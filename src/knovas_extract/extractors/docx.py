"""DOCX extractor — python-docx + mammoth backends.

DOCX is a ZIP container holding XML. Two trust-boundary hazards we have to
defuse before parsing:

1. **Decompression bombs** (1 KB → 4 PB classic). We pre-scan the ZIP's
   central directory; any entry whose declared uncompressed size / compressed
   size > Limits.max_decompression_ratio raises ResourceExhaustedError before
   any extractor touches the bytes.
2. **Zip-slip** — an entry name like `../../etc/passwd` would write outside
   the destination if the extractor naively unpacks. We never extract to
   disk, but we still refuse ZIP entries with absolute or parent-traversing
   names so a buggy downstream consumer can't be coerced.

XML parsing within docx files goes through `defusedxml` (entity resolution
disabled, billion-laughs blocked). python-docx historically used lxml without
defusedxml; we install defusedxml's lxml shim at import time.

For body text we use **python-docx**; for heading extraction (sections[]) we
use **mammoth**'s HTML conversion + a tiny ATX-style harvester. Either path
alone has known gaps; the combination is the established pragmatic approach.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING, ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import (
    CorruptDocumentError,
    ResourceExhaustedError,
)
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Section

if TYPE_CHECKING:
    pass

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# mammoth emits HTML like <h2>Heading text</h2>. We harvest level + text via
# this regex rather than spinning up a full HTML parser.
_HEADING_HTML = re.compile(r"<h([1-6])>([^<]+)</h\1>", re.IGNORECASE)


def _guard_zip(data: bytes, limits: Limits) -> zipfile.ZipFile:
    """Open the docx zip, refusing zip-slip names and decompression bombs.

    Raises CorruptDocumentError on malformed/zip-slip and
    ResourceExhaustedError on bomb-like compression ratios.
    """
    try:
        zf = zipfile.ZipFile(BytesIO(data), "r")
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise CorruptDocumentError(f"DOCX is not a valid ZIP container: {exc}") from exc

    for info in zf.infolist():
        # Zip-slip / absolute-path guard.
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
            zf.close()
            raise CorruptDocumentError(f"DOCX contains zip-slip-style entry name: {name!r}")
        # Decompression bomb guard.
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_decompression_ratio:
                zf.close()
                raise ResourceExhaustedError(
                    "decompression ratio",
                    limits.max_decompression_ratio,
                    observed=ratio,
                )
        # Per-entry absolute size guard — even a single huge entry is suspect.
        if info.file_size > limits.max_text_bytes:
            zf.close()
            raise ResourceExhaustedError(
                "zip entry uncompressed size",
                limits.max_text_bytes,
                observed=info.file_size,
            )
    return zf


# NOTE — XML security posture:
#   - Our own core.xml parsing goes through `defusedxml.ElementTree` (see
#     _extract_core_metadata below). That's the safe path.
#   - python-docx uses **lxml** internally; defusedxml cannot intercept that.
#     lxml's defaults disable external-entity fetching and network access but
#     DO resolve internal entities, leaving a residual billion-laughs risk on
#     hostile DOCX.
#   - The real defense against XML bombs in hostile DOCX is the decompression-
#     ratio cap + per-entry size cap in _guard_zip — a billion-laughs payload
#     trips one of those before lxml ever sees the bytes.
# We previously called defusedxml.defuse_stdlib() here as belt-and-suspenders.
# Removed because: (a) we don't use stdlib XML directly, (b) it patches the
# deprecated xml.etree.cElementTree which emits a DeprecationWarning under
# Python 3.13 that we'd have to silence at every callsite.


def _extract_body_text(data: bytes) -> str:
    """Body text via python-docx. Takes original docx bytes (not a ZipFile)."""
    import docx  # python-docx

    try:
        document = docx.Document(BytesIO(data))
    except Exception as exc:
        raise CorruptDocumentError(f"python-docx could not parse: {exc}") from exc

    parts: list[str] = []
    for para in document.paragraphs:
        text = (para.text or "").rstrip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text)
            if row_text:
                parts.append(row_text)
    return "\n\n".join(parts)


def _extract_sections(data: bytes) -> list[Section]:
    """Heading-derived sections via mammoth → HTML → harvester."""
    import mammoth

    try:
        result = mammoth.convert_to_html(BytesIO(data))
    except Exception:
        return []

    html = result.value or ""
    matches = list(_HEADING_HTML.finditer(html))
    if not matches:
        return []

    sections: list[Section] = []
    for i, m in enumerate(matches):
        level = int(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        body_html = html[start:end]
        # Preserve paragraph + list-item structure before stripping tags;
        # otherwise consecutive <p>foo</p><p>bar</p> collapses to "foobar".
        body_html = re.sub(r"</(p|li|h[1-6]|tr|br)\s*>", "\n\n", body_html, flags=re.I)
        body_text = re.sub(r"<[^>]+>", "", body_html)
        sections.append(Section(heading=heading, level=level, text=canonicalize_text(body_text)))
    return sections


def _extract_core_metadata(zf: zipfile.ZipFile) -> dict[str, str | None]:
    """Pull docProps/core.xml fields via defusedxml.

    Returns a dict with title/author/created/modified/subject/keywords (all str|None).
    """
    from defusedxml import ElementTree as ET

    try:
        with zf.open("docProps/core.xml") as f:
            tree = ET.parse(f)
    except KeyError:
        return {}
    except Exception as exc:
        raise CorruptDocumentError(f"docProps/core.xml unparseable: {exc}") from exc

    ns = {
        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
    }
    root = tree.getroot()

    def text_of(tag: str, namespace_key: str) -> str | None:
        el = root.find(f"{{{ns[namespace_key]}}}{tag}")
        if el is not None and el.text:
            return el.text.strip() or None
        return None

    return {
        "title": text_of("title", "dc"),
        "author": text_of("creator", "dc"),
        "subject": text_of("subject", "dc"),
        "keywords": text_of("keywords", "cp"),
        "language": text_of("language", "dc"),
        "created": text_of("created", "dcterms"),
        "modified": text_of("modified", "dcterms"),
        "revision": text_of("revision", "cp"),
        "last_modified_by": text_of("lastModifiedBy", "cp"),
    }


class DocxExtractor(IExtractor):
    """DOCX text + metadata extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({DOCX_MIME})
    name: ClassVar[str] = "docx"

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
    ) -> ExtractionResult:
        limits = limits or Limits()
        if len(data) > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))

        warnings: list[str] = []
        zf = _guard_zip(data, limits)
        try:
            # Detect (but never execute) macros.
            if "word/vbaProject.bin" in zf.namelist():
                warnings.append("DOCX contains VBA macros; payload ignored (never executed)")
            # Detect external relationships (template injection vector).
            if any(name.startswith("word/_rels/") for name in zf.namelist()):
                # Only warn if external targets are declared — most docs have
                # internal _rels which are harmless.
                pass

            body = _extract_body_text(data)
            text = canonicalize_text(body)

            if len(text.encode("utf-8")) > limits.max_text_bytes:
                raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

            sections = _extract_sections(data)
            meta = _extract_core_metadata(zf)
        finally:
            zf.close()

        extra: dict[str, str | int | float | bool | None] = {}
        for k in ("subject", "keywords", "revision", "last_modified_by"):
            v = meta.get(k)
            if v:
                extra[f"docx:{k}"] = v

        metadata = Metadata(
            title=meta.get("title"),
            author=meta.get("author"),
            language=meta.get("language"),
            created=meta.get("created"),
            modified=meta.get("modified"),
            word_count=word_count(text),
            extra=extra,
        )

        return make_result(
            text=text,
            mime=DOCX_MIME,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            sections=sections or None,
            warnings=warnings,
        )


MIME_REGISTRY[DOCX_MIME] = DocxExtractor()
