"""MIME detection + extractor dispatch.

Public entry point: `extract(input, *, mime=None, limits=None)`.

Detection priority:
1. Explicit `mime` argument (caller knows best — e.g. binary stream).
2. libmagic / python-magic header inspection of the first 8 KiB.
3. Fallback: filename extension if path-like input was given.
4. Raise UnsupportedFormatError.

We deliberately do NOT trust file extensions when content is available. Polyglot
files (a valid PDF that is also a valid ZIP) are real; they're handled by
header-first detection.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
from typing import Union

from knovas_extract._version import SPEC_VERSION as _SPEC_VERSION
from knovas_extract._version import __version__ as _PACKAGE_VERSION
from knovas_extract.errors import (
    DependencyMissingError,
    ResourceExhaustedError,
    UnsupportedFormatError,
)
from knovas_extract.interfaces import IExtractor
from knovas_extract.result import (
    Content,
    ExtractionResult,
    Extractor,
    Limits,
    Metadata,
    Page,
    Section,
    Sentence,
    Source,
)

# --- MIME registry ---------------------------------------------------------
# Each extractor module appends to this when imported. Lazy imports below keep
# startup cost flat regardless of which extras are installed.
MIME_REGISTRY: dict[str, IExtractor] = {}

# Filename-extension fallback (used only when content sniffing fails).
EXT_REGISTRY: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
    ".rtf": "application/rtf",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
}

_LAZY_LOADERS: dict[str, str] = {
    # MIME → module to import. The module's import side-effect registers it.
    "text/plain": "knovas_extract.extractors.txt",
    "text/markdown": "knovas_extract.extractors.md",
    "application/pdf": "knovas_extract.extractors.pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "knovas_extract.extractors.docx",
    "text/html": "knovas_extract.extractors.html",
    "application/xhtml+xml": "knovas_extract.extractors.html",
    "application/rtf": "knovas_extract.extractors.rtf",
    "text/rtf": "knovas_extract.extractors.rtf",
    "message/rfc822": "knovas_extract.extractors.eml",
    "application/vnd.ms-outlook": "knovas_extract.extractors.msg",
}

# Input types accepted by `extract`.
InputT = Union[str, "os.PathLike[str]", bytes, bytearray, memoryview]


def _read_input(input: InputT, limits: Limits) -> tuple[bytes, str | None, str | None]:
    """Return (bytes, filename-or-None, path-or-None). Enforces max_input_bytes.

    ``path`` preserves the caller's argv form (absolute stays absolute,
    relative stays relative). ``None`` when input was bytes.
    """
    if isinstance(input, bytes | bytearray | memoryview):
        data = bytes(input)
        filename = None
        argv_path: str | None = None
    else:
        raw = os.fspath(input)
        path = Path(raw)
        # Cheap early-out for regular files whose stat size already blows the
        # cap — avoids reading a known-oversized file. NOT authoritative:
        # special files (FIFO, /proc, /dev/*) stat as size 0 yet can stream
        # unbounded bytes, and a regular file can grow between stat and read
        # (TOCTOU). The bounded read below is the real guard.
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            if size > limits.max_input_bytes:
                raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=size)
        with path.open("rb") as f:
            # Read at most one byte past the cap so we can detect (but never
            # fully materialize) an oversized / unbounded source.
            data = f.read(limits.max_input_bytes + 1)
        filename = path.name
        argv_path = raw

    if len(data) > limits.max_input_bytes:
        raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))
    return data, filename, argv_path


# MIME canonicalization map — libmagic versions across Linux/macOS/Windows
# return DIFFERENT but equally-valid MIMEs for the same format. We always emit
# the canonical form (IANA-registered or de facto) so source.mime_type is
# byte-deterministic across platforms — otherwise the golden corpus would
# need per-platform expected.json files.
_MIME_CANONICAL = {
    # RTF: IANA-registered as application/rtf since 1998; older libmagic
    # (Windows bundled) still says text/rtf.
    "text/rtf": "application/rtf",
}


def _canonicalize_mime(mime: str) -> str:
    return _MIME_CANONICAL.get(mime, mime)


def _detect_mime(data: bytes, filename: str | None) -> str:
    """Header-first MIME detection with extension fallback.

    libmagic is optional — if it's unavailable, fall back to a small
    header-byte sniff and the extension table.

    Output is canonicalized via `_MIME_CANONICAL` so platform-version-
    skew across libmagic builds doesn't leak into `source.mime_type`.
    """
    # 1. libmagic (preferred). Catches both import errors and runtime errors
    # (libmagic system library missing on minimal containers, non-str under
    # unusual encoding conditions, etc.). All are non-fatal — we fall through
    # to the built-in header sniff in step 2.
    libmagic_mime: str | None = None
    with contextlib.suppress(Exception):
        import magic

        # mime=True forces RFC-style MIME string output.
        m = magic.from_buffer(data[: 1 << 13], mime=True)
        if m and m != "application/octet-stream":
            libmagic_mime = m

    # libmagic version-skew across platforms: macOS, Linux, and Windows ship
    # different libmagic versions, some of which classify our v1 formats with
    # less-specific MIMEs than the filename extension implies. When we have a
    # known filename extension AND libmagic's answer is in the generic /
    # container set, prefer the extension. Examples:
    #   - DOCX → libmagic often says application/zip (it IS a ZIP)
    #   - XHTML / HTML-with-<?xml?> → text/xml or application/xml
    #   - EML → some libmagic versions say text/plain (it's RFC 5322 text)
    #   - extension-tagged plain text → text/plain (no harm)
    # We never trust the filename when libmagic returned a content-specific,
    # non-container MIME that disagrees with extension.
    _GENERIC_MIMES = {
        "application/zip",
        "text/xml",
        "application/xml",
        "text/plain",
        "application/octet-stream",
    }
    if filename:
        ext = Path(filename).suffix.lower()
        ext_mime = EXT_REGISTRY.get(ext)
        if ext_mime and (libmagic_mime in _GENERIC_MIMES or libmagic_mime is None):
            return _canonicalize_mime(ext_mime)
    if libmagic_mime is not None:
        return _canonicalize_mime(libmagic_mime)

    # 2. Tiny built-in header sniff for common formats (covers test paths
    # where python-magic isn't installed).
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04") and filename:
        # ZIP — could be DOCX, XLSX, ePub, ...; use the extension to pick.
        ext = Path(filename).suffix.lower()
        if ext in EXT_REGISTRY:
            return _canonicalize_mime(EXT_REGISTRY[ext])
    if data.startswith(b"{\\rtf"):
        return "application/rtf"
    if data.startswith((b"<!DOCTYPE html", b"<html", b"<HTML")):
        return "text/html"

    # 3. Extension fallback.
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in EXT_REGISTRY:
            return EXT_REGISTRY[ext]

    # 4. Last resort: assume plain text if it looks UTF-decodable.
    try:
        data[:1024].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _get_extractor(mime: str) -> IExtractor:
    """Resolve a MIME type to a registered IExtractor, lazily importing if needed."""
    if mime in MIME_REGISTRY:
        return MIME_REGISTRY[mime]

    module_name = _LAZY_LOADERS.get(mime)
    if module_name:
        try:
            __import__(module_name)
        except DependencyMissingError:
            raise
        except ImportError as exc:
            # Re-frame as DependencyMissingError when the failure is the
            # extra's package, not our own code.
            missing = getattr(exc, "name", "unknown")
            extra_map = {
                "fitz": "pdf",
                "pymupdf": "pdf",
                "pymupdf4llm": "pdf",
                "docx": "docx",
                "mammoth": "docx",
                "extract_msg": "msg",
                "selectolax": "html",
                "striprtf": "rtf",
                "frontmatter": "md",
                "markdownify": "markdown",
            }
            extra = extra_map.get(missing, mime.split("/")[-1])
            raise DependencyMissingError(extra, missing) from exc
        if mime in MIME_REGISTRY:
            return MIME_REGISTRY[mime]

    raise UnsupportedFormatError(mime)


def extract(
    input: InputT,
    *,
    mime: str | None = None,
    limits: Limits | None = None,
    emit_markdown: bool = False,
    emit_sentences: bool = False,
    path: str | None = None,
) -> ExtractionResult:
    """Extract text + metadata from a document.

    Args:
        input:  Path-like (str / os.PathLike) OR raw bytes / bytearray / memoryview.
        mime:   Optional explicit MIME type. If None, detected from content.
        limits: Optional Limits override. Defaults are conservative; see Limits.
        emit_markdown: When True, populate `content.markdown` with a sanitized
            Markdown rendering of the document (whole-doc, not per-page).
            Sanitization is applied via `_markdown.html_to_markdown` — see
            SECURITY.md. Some formats have no structure to convert
            (e.g. RTF via striprtf) and will leave `content.markdown = None`
            with a warning explaining why. Defaults to False for zero-cost
            behavior parity with 1.0.0.
        emit_sentences: When True, populate `content.sentences` with a
            deterministic pysbd-based tokenization carrying exact char
            offsets + line coordinates + page/section back-pointers. See
            docs/citations.md for the full contract. Defaults to False for
            zero-cost behavior parity with 1.1.0.
        path: Optional caller-supplied source path. When ``input`` is
            path-like, dispatch auto-populates this from the argv form
            (absolute stays absolute); an explicit ``path=`` overrides.
            Validated by `_paths.validate_source_path` — rejects NUL, ASCII
            control chars, Unicode bidi-override chars (Trojan Source),
            and lengths over ``Limits.max_path_length``.

    Returns:
        ExtractionResult — guaranteed to validate against spec/schema.json.

    Raises:
        UnsupportedFormatError, CorruptDocumentError, EncryptedDocumentError,
        ResourceExhaustedError, DependencyMissingError, ValueError (path).
    """
    from knovas_extract._paths import validate_source_path

    limits = limits or Limits()
    data, filename, argv_path = _read_input(input, limits)
    resolved_path = validate_source_path(path if path is not None else argv_path, limits)

    detected_mime = mime or _detect_mime(data, filename)
    extractor = _get_extractor(detected_mime)

    # Extractors lazy-import their parser backends INSIDE their extract() method
    # (so importing knovas_extract is cheap even with no extras installed). If
    # the backend isn't installed at call time, an ImportError escapes the
    # extractor and would break the contract — re-frame as DependencyMissingError.
    try:
        result = extractor.extract(
            data,
            filename=filename,
            limits=limits,
            emit_markdown=emit_markdown,
            emit_sentences=emit_sentences,
        )
    except ImportError as exc:
        missing = getattr(exc, "name", "unknown")
        extra_map = {
            "fitz": "pdf",
            "pymupdf": "pdf",
            "pymupdf4llm": "pdf",
            "docx": "docx",
            "mammoth": "docx",
            "extract_msg": "msg",
            "selectolax": "html",
            "striprtf": "rtf",
            "frontmatter": "md",
            "markdownify": "markdown",
            "pysbd": "sentences",
        }
        extra = extra_map.get(missing, detected_mime.split("/")[-1])
        raise DependencyMissingError(extra, missing) from exc

    # Defense-in-depth: enforce the source block matches what dispatch actually saw.
    # An extractor that overrides source.sha256/size_bytes/mime_type is buggy;
    # we patch it here rather than trust per-extractor implementations.
    object.__setattr__(
        result,
        "source",
        Source(
            mime_type=detected_mime,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            path=resolved_path,
        ),
    )
    object.__setattr__(result, "spec_version", _SPEC_VERSION)
    object.__setattr__(
        result,
        "extractor",
        Extractor(
            name="knovas-extract-python",
            version=_PACKAGE_VERSION,
        ),
    )

    # Defense-in-depth: if an extractor populated `content.markdown` on the
    # emit_markdown path, run a final URL-scheme scrub on it. Catches
    # regressions in a per-extractor path (e.g. pymupdf4llm surfacing a
    # javascript: annotation URL, a future extractor that forgets the helper).
    if emit_markdown and result.content.markdown is not None:
        from knovas_extract._markdown import apply_url_allowlist

        cleaned = apply_url_allowlist(result.content.markdown, warnings=result.warnings)
        if cleaned is not result.content.markdown:
            result.content.markdown = cleaned

    # Sentence↔section back-pointer + post-condition invariants.
    if emit_sentences:
        from knovas_extract._sentences import attach_section_indices

        if result.content.sentences is None:
            # Extractor forgot to emit — either the format has no text
            # (RTF, etc.) or the extractor path is stale. Give consumers
            # the "opted-in and got nothing" signal, not None.
            result.content.sentences = []
        attach_section_indices(result.content.sentences, result.content.sections)
        _assert_consumer_contracts(result)

    return result


def _assert_consumer_contracts(result: ExtractionResult) -> None:
    """Producer-side post-conditions on the sentence array.

    Any violation is a producer bug (extractor wired sentences without
    honoring the contract). Raise RuntimeError so it's noisy in tests
    and impossible to silently ship.
    """
    sentences = result.content.sentences
    if not sentences:
        return

    text = result.content.text
    pages = result.content.pages
    sections = result.content.sections

    for i, s in enumerate(sentences):
        # 1. Exact retrieval.
        if text[s.char_start : s.char_end] != s.text:
            raise RuntimeError(f"sentence {s.index}: exact-retrieval invariant violated")

        # 2. Sentence↔page linkage.
        if pages is not None:
            if s.page_index is None or s.page_number is None:
                raise RuntimeError(
                    f"sentence {s.index}: pages present but page_index/number is None"
                )
            if s.page_number != s.page_index + 1:
                raise RuntimeError(
                    f"sentence {s.index}: page_number ({s.page_number}) != "
                    f"page_index+1 ({s.page_index + 1})"
                )
        else:
            if s.page_index is not None or s.page_number is not None:
                raise RuntimeError(f"sentence {s.index}: pages is None but page coords fabricated")

        # 3. Sentence↔section linkage bounds.
        if s.section_index is not None and (
            sections is None or not (0 <= s.section_index < len(sections))
        ):
            raise RuntimeError(f"sentence {s.index}: section_index {s.section_index} out of range")

        # 4. Ordering.
        if i > 0 and s.char_start < sentences[i - 1].char_end:
            raise RuntimeError(f"sentence {s.index}: char_start overlaps predecessor")

        # 5. Index monotonicity.
        if s.index != i:
            raise RuntimeError(f"sentence at position {i} has index {s.index}")


# Re-export the canonical "empty" result builder so extractors can start from a
# known-good baseline.
def make_result(
    *,
    text: str,
    mime: str,
    sha256: str,
    size_bytes: int,
    filename: str | None = None,
    path: str | None = None,
    metadata: Metadata | None = None,
    pages: list[Page] | None = None,
    sections: list[Section] | None = None,
    warnings: list[str] | None = None,
    markdown: str | None = None,
    sentences: list[Sentence] | None = None,
    tables=None,
) -> ExtractionResult:
    return ExtractionResult(
        spec_version=_SPEC_VERSION,
        source=Source(
            mime_type=mime,
            sha256=sha256,
            size_bytes=size_bytes,
            filename=filename,
            path=path,
        ),
        metadata=metadata or Metadata(),
        content=Content(
            text=text,
            pages=pages,
            sections=sections,
            markdown=markdown,
            sentences=sentences,
            tables=tables,
        ),
        warnings=warnings or [],
        extractor=Extractor(name="knovas-extract-python", version=_PACKAGE_VERSION),
    )
