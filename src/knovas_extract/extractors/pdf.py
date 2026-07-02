"""PDF extractor — PyMuPDF backend.

PyMuPDF wraps MuPDF (C). It's by far the fastest mature Python PDF library
(20-100 pages/sec text-only on a single thread) but it is **AGPL-licensed** -
downstream users must comply with the AGPL's network-distribution clauses if
they embed knovas-extract in a closed-source product that loads PyMuPDF at
runtime. See NOTICE; install permissive-only stack via `pip install
knovas-extract[minimal]`.

Security posture (see SECURITY.md):
- Embedded JavaScript: never executed; we don't even enumerate JS streams.
  A warning is emitted when present so callers can audit.
- Encrypted PDFs: raise EncryptedDocumentError (we never attempt blank-password
  bypass; some valid PDFs are intentionally locked).
- Page-count cap: enforced via Limits.max_pages → ResourceExhaustedError when
  the source exceeds it (default 10 000).
- Hostile inputs (malformed object streams etc.): PyMuPDF's `fitz.open()` and
  page-level operations raise `fitz.FileDataError` / `RuntimeError`, which we
  re-raise as `CorruptDocumentError`.

The extractor is intentionally text-focused. Images, forms, annotations, and
embedded files are NOT extracted in v1; per-format metadata of interest is
surfaced under `metadata.extra` with `pdf:` namespace.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING, ClassVar, cast

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import (
    CorruptDocumentError,
    DependencyMissingError,
    EncryptedDocumentError,
    ResourceExhaustedError,
)
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Page

# PyMuPDF `doc.permissions` bitmask flags. See pdfmark spec + PyMuPDF docs.
_PDF_PERM_TOKENS = (
    (1 << 2, "print"),
    (1 << 3, "modify"),
    (1 << 4, "copy"),
    (1 << 5, "annotate"),
    (1 << 8, "form"),
    (1 << 9, "accessibility"),
    (1 << 10, "assemble"),
    (1 << 11, "print_high_res"),
)


def _perm_tokens(bits: int | None) -> str | None:
    if not bits:
        return None
    tokens = [name for mask, name in _PDF_PERM_TOKENS if bits & mask]
    return ",".join(tokens) if tokens else None


def _parse_xmp(xmp: str, limits: Limits, warnings: list[str]) -> dict[str, str]:
    """Parse PDF XMP metadata (XML) via defusedxml, size-capped.

    Returns a dict with any of: title, author, description, language,
    creator_tool, pdfa_part. Empty dict on error / oversize / absence.
    """
    if not xmp:
        return {}
    if len(xmp) > limits.max_xmp_bytes:
        warnings.append("pdf: xmp metadata exceeded max_xmp_bytes; skipped")
        return {}

    try:
        from defusedxml import ElementTree as ET

        root = ET.fromstring(xmp)
    except Exception:
        warnings.append("pdf: xmp metadata unparseable; skipped")
        return {}

    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "xmp": "http://ns.adobe.com/xap/1.0/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "pdfaid": "http://www.aiim.org/pdfa/ns/id/",
    }

    def _text_deep(tag: str, prefix: str) -> str | None:
        # dc: / xmp: fields are usually wrapped in rdf:Alt / rdf:Seq / rdf:li.
        for el in root.iter(f"{{{ns[prefix]}}}{tag}"):
            if el.text and el.text.strip():
                return el.text.strip()
            # Nested rdf:li.
            for child in el.iter(f"{{{ns['rdf']}}}li"):
                if child.text and child.text.strip():
                    return child.text.strip()
        return None

    def _attr_deep(tag: str, prefix: str, attr_prefix: str, attr: str) -> str | None:
        for el in root.iter(f"{{{ns[prefix]}}}{tag}"):
            v = el.attrib.get(f"{{{ns[attr_prefix]}}}{attr}") or el.attrib.get(attr)
            if v:
                return v.strip() or None
        return None

    out: dict[str, str] = {}
    for k, tag, pfx in (
        ("title", "title", "dc"),
        ("author", "creator", "dc"),
        ("description", "description", "dc"),
        ("language", "language", "dc"),
        ("creator_tool", "CreatorTool", "xmp"),
    ):
        v = _text_deep(tag, pfx)
        if v:
            out[k] = v

    part = _attr_deep("part", "pdfaid", "pdfaid", "part")
    if part:
        out["pdfa_part"] = part
    return out


if TYPE_CHECKING:
    import fitz


def _parse_pdf_date(s: str | None) -> str | None:
    """Convert a PDF /D:YYYYMMDDHHMMSS+ZZ'zz' date string to ISO 8601.

    PyMuPDF returns metadata dates in the raw PDF format; the schema requires
    ISO 8601. Best-effort: returns None if parsing fails (the field is
    optional in the schema).
    """
    if not s:
        return None
    s = s.strip()
    if s.startswith("D:"):
        s = s[2:]
    # Format: YYYYMMDDHHMMSSOHH'mm' (O = + or - or Z)
    if len(s) < 4:
        return None
    try:
        year = int(s[0:4])
        month = int(s[4:6]) if len(s) >= 6 else 1
        day = int(s[6:8]) if len(s) >= 8 else 1
        hour = int(s[8:10]) if len(s) >= 10 else 0
        minute = int(s[10:12]) if len(s) >= 12 else 0
        second = int(s[12:14]) if len(s) >= 14 else 0
    except ValueError:
        return None
    # Timezone — best-effort. Common forms: Z, +0200, +02'00'.
    tz = "Z"
    if len(s) >= 15:
        rest = s[14:].replace("'", "")
        if rest.startswith(("+", "-")) and len(rest) >= 3:
            sign = rest[0]
            tzhour = rest[1:3]
            tzmin = rest[3:5] if len(rest) >= 5 else "00"
            tz = f"{sign}{tzhour}:{tzmin}"
        elif rest.startswith("Z"):
            tz = "Z"
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}{tz}"


def _pdf_to_markdown(
    doc: fitz.Document,
    plain_text: str,
    limits: Limits,
    warnings: list[str],
) -> str | None:
    """Emit whole-doc markdown via pymupdf4llm, with URL allowlist + size guards.

    Returns None (and appends a warning) on backend failure — never
    silently substitutes plain text as if it were markdown.
    """
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise DependencyMissingError("pdf", "pymupdf4llm") from exc

    from knovas_extract._markdown import apply_url_allowlist, check_expansion

    try:
        raw_md = pymupdf4llm.to_markdown(doc)
    except Exception:
        warnings.append("pdf: pymupdf4llm conversion failed; content.markdown left null")
        return None

    md = canonicalize_text(raw_md or "")

    if len(md.encode("utf-8")) > limits.max_text_bytes:
        raise ResourceExhaustedError("markdown size", limits.max_text_bytes, observed=len(md))
    check_expansion(md, len(plain_text), limits)

    # PDF annotation URLs are the primary risk: /URI actions with
    # javascript: / file: schemes come through pymupdf4llm as clickable
    # markdown links. Scrub via the shared allowlist.
    return apply_url_allowlist(md, warnings=warnings)


def _open_doc(data: bytes) -> fitz.Document:
    """Open the PDF, mapping every PyMuPDF failure mode to a typed ExtractError."""
    import fitz  # local import — keeps top-level startup cost flat

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        # fitz.FileDataError, RuntimeError, ValueError — all signal an
        # unparseable input. Don't leak the underlying exception type to
        # callers; the contract is "ExtractError or success".
        raise CorruptDocumentError(f"could not parse PDF: {exc}") from exc

    # Encryption check. We refuse password-protected PDFs predictably; the
    # blank-password attempt covers PDFs that claim is_encrypted but accept
    # an empty owner password (some scanner-generated PDFs do this).
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise EncryptedDocumentError(
            "PDF is encrypted; provide an unlocked copy or a password "
            "(passwords are not exposed via the public knovas-extract API yet)."
        )
    return doc


class PdfExtractor(IExtractor):
    """PDF text + metadata extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/pdf"})
    name: ClassVar[str] = "pdf"

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
        doc = _open_doc(data)

        try:
            page_count = doc.page_count
            if page_count > limits.max_pages:
                raise ResourceExhaustedError("page_count", limits.max_pages, observed=page_count)

            # Per-page text. Caps text size in aggregate; raises if exceeded.
            pages: list[Page] = []
            text_chunks: list[str] = []
            total_chars = 0
            had_js = False
            for i in range(page_count):
                try:
                    page = doc.load_page(i)
                except Exception as exc:
                    warnings.append(f"page {i}: could not load ({exc})")
                    continue
                page_text = cast("str", page.get_text("text") or "")
                if not page_text and i == 0:
                    warnings.append("first page produced no text (consider OCR for scanned PDFs)")
                pages.append(Page(index=i, text=canonicalize_text(page_text)))
                text_chunks.append(page_text)
                total_chars += len(page_text)
                if total_chars > limits.max_text_bytes:
                    raise ResourceExhaustedError(
                        "text size", limits.max_text_bytes, observed=total_chars
                    )

            # Doc-level JS check (cheap; runs once). Heuristic only; the
            # only thing we ever do with detected JS is emit a warning.
            with contextlib.suppress(Exception):
                if doc.has_links() or any(
                    "/JS" in str(doc.xref_object(xref))
                    for xref in range(1, min(50, doc.xref_length()))
                ):
                    had_js = True
            if had_js:
                warnings.append("PDF embedded JavaScript ignored (never executed)")

            full_text = canonicalize_text("\n\n".join(text_chunks))

            # Attach 1-based line coordinates to each Page by locating each
            # page.text in full_text (they are identical after
            # canonicalize_text, up to the "\n\n" join). The cursor keeps
            # searches linear.
            cursor = 0
            for p in pages:
                if not p.text:
                    p.line_start = None
                    p.line_end = None
                    continue
                loc = full_text.find(p.text, cursor)
                if loc < 0:
                    p.line_start = None
                    p.line_end = None
                    continue
                p.line_start = 1 + full_text.count("\n", 0, loc)
                p.line_end = 1 + full_text.count("\n", 0, loc + len(p.text) - 1)
                cursor = loc + len(p.text)

            # Markdown path — whole-doc via pymupdf4llm.
            markdown: str | None = None
            if emit_markdown:
                markdown = _pdf_to_markdown(doc, full_text, limits, warnings)

            raw_meta = doc.metadata or {}

            # XMP metadata — parsed via defusedxml with a size cap.
            xmp_dict: dict[str, str] = {}
            with contextlib.suppress(Exception):
                xmp_raw = doc.get_xml_metadata() or ""
                xmp_dict = _parse_xmp(xmp_raw, limits, warnings)

            # Merge policy: XMP wins over the older doc.metadata dict when
            # both are present and non-empty for the same first-class field.
            title = xmp_dict.get("title") or (raw_meta.get("title") or "").strip() or None
            author = xmp_dict.get("author") or (raw_meta.get("author") or "").strip() or None
            language = xmp_dict.get("language") or None

            extra: dict[str, str | int | float | bool | None] = {}
            for k, v in {
                "pdf:producer": (raw_meta.get("producer") or "").strip() or None,
                "pdf:creator": (raw_meta.get("creator") or "").strip() or None,
                "pdf:subject": (raw_meta.get("subject") or "").strip() or None,
                "pdf:keywords": (raw_meta.get("keywords") or "").strip() or None,
                "pdf:format": raw_meta.get("format"),
                "pdf:xmp_description": xmp_dict.get("description"),
                "pdf:xmp_creator_tool": xmp_dict.get("creator_tool"),
                "pdf:pdfa_part": xmp_dict.get("pdfa_part"),
            }.items():
                if v is None:
                    continue
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[k] = clean

            # PDF version, permissions, outline count, forms/annotations.
            with contextlib.suppress(Exception):
                pdf_version = getattr(doc, "pdf_version", None)
                if callable(pdf_version):
                    pv = pdf_version()
                    if pv is not None:
                        extra["pdf:pdf_version"] = str(pv)
            with contextlib.suppress(Exception):
                perms = _perm_tokens(getattr(doc, "permissions", 0))
                if perms:
                    extra["pdf:permissions"] = perms
            with contextlib.suppress(Exception):
                extra["pdf:outline_count"] = len(doc.get_toc())
            with contextlib.suppress(Exception):
                extra["pdf:is_form_pdf"] = bool(doc.is_form_pdf)

            finalize_warnings(counts, warnings)

            metadata = Metadata(
                title=title,
                author=author,
                language=language,
                created=_parse_pdf_date(raw_meta.get("creationDate")),
                modified=_parse_pdf_date(raw_meta.get("modDate")),
                page_count=page_count,
                word_count=word_count(full_text),
                extra=extra,
            )

            # Sentences — per-page tokenization stitched into
            # document-global coords via split_sentences_for_pages.
            sentences = None
            if emit_sentences:
                from knovas_extract._sentences import split_sentences_for_pages

                sentences = split_sentences_for_pages(
                    pages, full_text, limits, warnings=warnings, language=language
                )

            return make_result(
                text=full_text,
                mime="application/pdf",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                filename=filename,
                metadata=metadata,
                pages=pages or None,
                warnings=warnings,
                markdown=markdown,
                sentences=sentences,
            )
        finally:
            doc.close()


MIME_REGISTRY["application/pdf"] = PdfExtractor()
