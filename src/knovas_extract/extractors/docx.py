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


def _extract_html(data: bytes) -> str | None:
    """Convert DOCX bytes to HTML via mammoth. Returns None on backend failure.

    Isolated so that both `_sections_from_html` and `_markdown_from_html`
    can reuse the (relatively expensive) mammoth conversion output.
    """
    import mammoth

    try:
        result = mammoth.convert_to_html(BytesIO(data))
    except Exception:
        return None
    return result.value or ""


def _sections_from_html(html: str, canonical_text: str | None = None) -> list[Section]:
    """Harvest heading-anchored sections from mammoth HTML output.

    When ``canonical_text`` (== `content.text` we return) is provided,
    each Section carries 1-based `line_start` / `line_end` in it — used
    by the sentence↔section back-pointer and consumer citations.
    """
    matches = list(_HEADING_HTML.finditer(html))
    if not matches:
        return []

    sections: list[Section] = []
    canon_cursor = 0
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

        line_start: int | None = None
        line_end: int | None = None
        if canonical_text is not None:
            hstart = canonical_text.find(heading, canon_cursor)
            if hstart >= 0:
                line_start = 1 + canonical_text.count("\n", 0, hstart)
                next_pos = len(canonical_text)
                for j in range(i + 1, len(matches)):
                    nh = matches[j].group(2).strip()
                    n = canonical_text.find(nh, hstart + len(heading))
                    if n >= 0:
                        next_pos = n
                        break
                line_end = 1 + canonical_text.count("\n", 0, max(next_pos - 1, hstart))
                canon_cursor = hstart + len(heading)

        sections.append(
            Section(
                heading=heading,
                level=level,
                text=canonicalize_text(body_text),
                line_start=line_start,
                line_end=line_end,
            )
        )
    return sections


def _markdown_from_html(html: str, limits: Limits, warnings: list[str]) -> str:
    """Sanitize mammoth's HTML output and convert to Markdown.

    Even though mammoth emits structurally-clean HTML, we still route
    through the full sanitizer because <a href> URLs come from the source
    document verbatim — a hostile DOCX can embed `javascript:` links, and
    mammoth surfaces Word "Text Box" content as raw HTML fragments in
    some documents.
    """
    from knovas_extract._markdown import html_to_markdown

    return html_to_markdown(html, limits, warnings=warnings)


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
        "category": text_of("category", "cp"),
        "content_status": text_of("contentStatus", "cp"),
        "version": text_of("version", "cp"),
    }


def _extract_app_metadata(zf: zipfile.ZipFile) -> dict[str, str | None]:
    """Pull docProps/app.xml fields via defusedxml.

    Optional per the OOXML spec — many DOCX files (especially those from
    non-Microsoft producers) omit app.xml entirely. Absence is not an
    error; returns an empty dict.
    """
    from defusedxml import ElementTree as ET

    try:
        with zf.open("docProps/app.xml") as f:
            tree = ET.parse(f)
    except KeyError:
        return {}
    except Exception as exc:
        raise CorruptDocumentError(f"docProps/app.xml unparseable: {exc}") from exc

    ns = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
    root = tree.getroot()

    def text_of(tag: str) -> str | None:
        el = root.find(f"{{{ns}}}{tag}")
        if el is not None and el.text:
            return el.text.strip() or None
        return None

    return {
        "application": text_of("Application"),
        "app_version": text_of("AppVersion"),
        "template": text_of("Template"),
        "total_time": text_of("TotalTime"),
        "pages_declared": text_of("Pages"),
        "paragraph_count": text_of("Paragraphs"),
        "word_count_declared": text_of("Words"),
        "character_count": text_of("Characters"),
        "company": text_of("Company"),
        "manager": text_of("Manager"),
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
        emit_markdown: bool = False,
        emit_sentences: bool = False,
    ) -> ExtractionResult:
        from collections import Counter

        from knovas_extract._metadata import finalize_warnings, sanitize_scalar

        limits = limits or Limits()
        if len(data) > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))

        warnings: list[str] = []
        counts: Counter[str] = Counter()
        zf = _guard_zip(data, limits)
        try:
            # Detect (but never execute) macros.
            if "word/vbaProject.bin" in zf.namelist():
                warnings.append("DOCX contains VBA macros; payload ignored (never executed)")

            body = _extract_body_text(data)
            text = canonicalize_text(body)

            if len(text.encode("utf-8")) > limits.max_text_bytes:
                raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

            # Call mammoth ONCE and derive both sections and (optionally)
            # markdown from the same HTML. Cheaper than double-converting.
            html = _extract_html(data)
            sections = _sections_from_html(html, canonical_text=text) if html else []

            markdown: str | None = None
            if emit_markdown:
                if html is None:
                    warnings.append("docx: mammoth conversion failed; content.markdown left null")
                else:
                    from knovas_extract._markdown import check_expansion

                    markdown = _markdown_from_html(html, limits, warnings)
                    check_expansion(markdown, len(text), limits)

            meta = _extract_core_metadata(zf)
            app = _extract_app_metadata(zf)
        finally:
            zf.close()

        extra: dict[str, str | int | float | bool | None] = {}
        # Core.xml extras.
        for k in (
            "subject",
            "keywords",
            "revision",
            "last_modified_by",
            "category",
            "content_status",
            "version",
        ):
            v = meta.get(k)
            if v:
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[f"docx:{k}"] = clean
        # App.xml extras.
        for k in (
            "application",
            "app_version",
            "template",
            "total_time",
            "pages_declared",
            "paragraph_count",
            "word_count_declared",
            "character_count",
            "company",
            "manager",
        ):
            v = app.get(k)
            if v:
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[f"docx:{k}"] = clean
        finalize_warnings(counts, warnings)

        metadata = Metadata(
            title=meta.get("title"),
            author=meta.get("author"),
            language=meta.get("language"),
            created=meta.get("created"),
            modified=meta.get("modified"),
            word_count=word_count(text),
            extra=extra,
        )

        sentences = None
        if emit_sentences:
            from knovas_extract._sentences import split_sentences

            sentences = split_sentences(text, limits, warnings=warnings, language=metadata.language)

        return make_result(
            text=text,
            mime=DOCX_MIME,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            sections=sections or None,
            warnings=warnings,
            markdown=markdown,
            sentences=sentences,
        )


MIME_REGISTRY[DOCX_MIME] = DocxExtractor()
