"""RTF extractor — striprtf backend.

striprtf is a small pure-Python library that strips RTF control words and
returns plain text. It does NOT support `\\object` linking, embedded OLE
objects, or any kind of code execution — by deliberate design.

Security posture (see SECURITY.md):
- **CVE-2017-0199 (RTF OLE auto-load)** and its descendants attack callers
  that follow `\\object`/`\\objdata` references to fetch external payloads.
  striprtf has no opener for any of these; the bytes are silently dropped.
  An `obj` warning is added when control words are detected so callers can
  audit hostile inputs.
- **Stack overflow / recursion** on deeply-nested group braces: striprtf is
  iterative, not recursive. The recursion-depth Limit isn't relevant here;
  the input-size cap is the only relevant guard.
- **Non-RTF bytes**: striprtf returns "" silently for inputs that don't open
  with `{\\rtf`. We surface that as CorruptDocumentError to keep the contract
  ("anything but a valid document raises a typed error").
"""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.extractors.txt import _decode
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata

# Detect any OLE-linking control word so we can warn callers.
_OBJ_PATTERNS = re.compile(r"\\(object|objdata|objemb|objautlink|objupdate)\b", re.IGNORECASE)


class RtfExtractor(IExtractor):
    """RTF text extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/rtf", "text/rtf"})
    name: ClassVar[str] = "rtf"

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

        raw_text, _ = _decode(data)
        if not raw_text.lstrip().startswith("{\\rtf"):
            raise CorruptDocumentError("input does not begin with '{\\rtf' RTF header")

        from striprtf.striprtf import rtf_to_text

        try:
            stripped = rtf_to_text(raw_text, errors="ignore")  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise CorruptDocumentError(f"RTF parse failed: {exc}") from exc

        text = canonicalize_text(stripped or "")
        if len(text.encode("utf-8")) > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

        warnings: list[str] = []
        if _OBJ_PATTERNS.search(raw_text):
            warnings.append(
                "RTF contains OLE object-linking control words; payload ignored "
                "(never executed, never fetched — see SECURITY.md CVE-2017-0199 mitigation)"
            )

        return make_result(
            text=text,
            mime="application/rtf",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=Metadata(word_count=word_count(text)),
            warnings=warnings,
        )


_inst = RtfExtractor()
for _m in _inst.supported_mimes:
    MIME_REGISTRY[_m] = _inst
