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


def _read_input(input: InputT, limits: Limits) -> tuple[bytes, str | None]:
    """Return (bytes, filename-or-None). Enforces max_input_bytes."""
    if isinstance(input, bytes | bytearray | memoryview):
        data = bytes(input)
        filename = None
    else:
        path = Path(os.fspath(input))
        size = path.stat().st_size
        if size > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=size)
        with path.open("rb") as f:
            data = f.read()
        filename = path.name

    if len(data) > limits.max_input_bytes:
        raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))
    return data, filename


def _detect_mime(data: bytes, filename: str | None) -> str:
    """Header-first MIME detection with extension fallback.

    libmagic is optional — if it's unavailable, fall back to a small
    header-byte sniff and the extension table.
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
            return ext_mime
    if libmagic_mime is not None:
        return libmagic_mime

    # 2. Tiny built-in header sniff for common formats (covers test paths
    # where python-magic isn't installed).
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04") and filename:
        # ZIP — could be DOCX, XLSX, ePub, ...; use the extension to pick.
        ext = Path(filename).suffix.lower()
        if ext in EXT_REGISTRY:
            return EXT_REGISTRY[ext]
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
                "docx": "docx",
                "mammoth": "docx",
                "extract_msg": "msg",
                "selectolax": "html",
                "striprtf": "rtf",
                "frontmatter": "md",
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
) -> ExtractionResult:
    """Extract text + metadata from a document.

    Args:
        input:  Path-like (str / os.PathLike) OR raw bytes / bytearray / memoryview.
        mime:   Optional explicit MIME type. If None, detected from content.
        limits: Optional Limits override. Defaults are conservative; see Limits.

    Returns:
        ExtractionResult — guaranteed to validate against spec/schema.json.

    Raises:
        UnsupportedFormatError, CorruptDocumentError, EncryptedDocumentError,
        ResourceExhaustedError, DependencyMissingError.
    """
    limits = limits or Limits()
    data, filename = _read_input(input, limits)

    detected_mime = mime or _detect_mime(data, filename)
    extractor = _get_extractor(detected_mime)

    # Extractors lazy-import their parser backends INSIDE their extract() method
    # (so importing knovas_extract is cheap even with no extras installed). If
    # the backend isn't installed at call time, an ImportError escapes the
    # extractor and would break the contract — re-frame as DependencyMissingError.
    try:
        result = extractor.extract(data, filename=filename, limits=limits)
    except ImportError as exc:
        missing = getattr(exc, "name", "unknown")
        extra_map = {
            "fitz": "pdf",
            "pymupdf": "pdf",
            "docx": "docx",
            "mammoth": "docx",
            "extract_msg": "msg",
            "selectolax": "html",
            "striprtf": "rtf",
            "frontmatter": "md",
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
    return result


# Re-export the canonical "empty" result builder so extractors can start from a
# known-good baseline.
def make_result(
    *,
    text: str,
    mime: str,
    sha256: str,
    size_bytes: int,
    filename: str | None = None,
    metadata: Metadata | None = None,
    pages: list[Page] | None = None,
    sections: list[Section] | None = None,
    warnings: list[str] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        spec_version=_SPEC_VERSION,
        source=Source(mime_type=mime, sha256=sha256, size_bytes=size_bytes, filename=filename),
        metadata=metadata or Metadata(),
        content=Content(text=text, pages=pages, sections=sections),
        warnings=warnings or [],
        extractor=Extractor(name="knovas-extract-python", version=_PACKAGE_VERSION),
    )
