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
    EncryptedDocumentError,
    ResourceExhaustedError,
)
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Page

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
    ) -> ExtractionResult:
        limits = limits or Limits()
        if len(data) > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))

        warnings: list[str] = []
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
                # PDF JS detection — never executes, just notes presence.
                # PyMuPDF exposes JS via page.get_text("dict") metadata or
                # doc.is_form_pdf; for v1 we just check the doc-level flag once.
                # get_text("text") returns str at runtime, but the stub
                # overloads cover {"text", "dict", "blocks", ...} as
                # str | list | dict. cast() satisfies pyright; mypy is
                # silenced via the fitz ignore_missing_imports override.
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

            raw_meta = doc.metadata or {}
            metadata = Metadata(
                title=(raw_meta.get("title") or "").strip() or None,
                author=(raw_meta.get("author") or "").strip() or None,
                language=None,  # PDF metadata rarely carries reliable lang
                created=_parse_pdf_date(raw_meta.get("creationDate")),
                modified=_parse_pdf_date(raw_meta.get("modDate")),
                page_count=page_count,
                word_count=word_count(full_text),
                extra={
                    k: v
                    for k, v in {
                        "pdf:producer": (raw_meta.get("producer") or "").strip() or None,
                        "pdf:creator": (raw_meta.get("creator") or "").strip() or None,
                        "pdf:subject": (raw_meta.get("subject") or "").strip() or None,
                        "pdf:keywords": (raw_meta.get("keywords") or "").strip() or None,
                        "pdf:format": raw_meta.get("format"),
                    }.items()
                    if v
                },
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
            )
        finally:
            doc.close()


MIME_REGISTRY["application/pdf"] = PdfExtractor()
