"""Plain-text extractor.

Handles `text/plain`. Detects encoding via chardet (BOM stripping handled
implicitly by the codec). Returns text canonicalized by `normalize`.
"""
from __future__ import annotations

import hashlib
from typing import ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata


def _decode(data: bytes) -> tuple[str, str | None]:
    """Return (text, declared_charset). Best-effort encoding detection."""
    # Try UTF-8/16 BOMs first — cheap and unambiguous.
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be"), "utf-16-be"

    # Try strict UTF-8 (the vast majority case).
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # chardet fallback. Always optional dep handled gracefully.
    try:
        import chardet  # type: ignore[import-not-found]

        guess = chardet.detect(data)
        enc = (guess.get("encoding") or "").lower() or "utf-8"
        try:
            return data.decode(enc, errors="replace"), enc
        except (LookupError, UnicodeDecodeError) as exc:
            raise CorruptDocumentError(
                f"could not decode text with detected encoding {enc!r}: {exc}"
            ) from exc
    except ImportError:
        # chardet missing → final fallback with replacement chars.
        return data.decode("utf-8", errors="replace"), "utf-8"


class TxtExtractor(IExtractor):
    """Plain-text extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/plain"})
    name: ClassVar[str] = "txt"

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
    ) -> ExtractionResult:
        limits = limits or Limits()
        if len(data) > limits.max_text_bytes:
            raise ResourceExhaustedError(
                "text size", limits.max_text_bytes, observed=len(data)
            )

        raw_text, charset = _decode(data)
        text = canonicalize_text(raw_text)

        return make_result(
            text=text,
            mime="text/plain",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=Metadata(
                word_count=word_count(text),
                extra={"txt:charset_detected": charset} if charset else {},
            ),
        )


# Register at import time. The dispatch layer lazy-imports this module.
MIME_REGISTRY["text/plain"] = TxtExtractor()
