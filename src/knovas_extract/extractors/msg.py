"""MSG extractor — extract-msg backend (Outlook .msg / OLE Compound File).

extract-msg is GPL-3.0; downstream commercial users must comply. See NOTICE.
Install via `pip install 'knovas-extract[msg]'`.

Security posture (see SECURITY.md):
- **CFB / OLE compound file** — extract-msg parses the underlying CFB
  container via olefile. olefile has had CVEs historically; we pin recent
  versions and treat parse failures as CorruptDocumentError.
- **HTML body fallback**: when only HTML is present in the MSG, we strip
  tags via the same small regex used in the EML extractor. No external
  HTML parser. No image/CSS/script loading.
- **Attachments**: metadata only (name, content_type, size) — never read
  payload bytes into the result. extract-msg lets us iterate attachments
  cheaply via the attachment list without materializing each payload.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata

MSG_MIME = "application/vnd.ms-outlook"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    s = _TAG.sub(" ", s)
    return _WS.sub(" ", s).strip()


class MsgExtractor(IExtractor):
    """Outlook .msg / OLE compound file extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({MSG_MIME})
    name: ClassVar[str] = "msg"

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

        # extract-msg wants a path, not a bytes-like. Use a tmpfile inside the
        # configured tmpdir; the file is unlinked on close.
        import extract_msg

        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        try:
            try:
                # extract-msg has no type stubs; openMsg returns a polymorphic
                # MSGFile subclass whose body/subject/sender/etc. attributes
                # are not declared on the base class. Typing as Any keeps the
                # rest of this function readable.
                msg: Any = extract_msg.openMsg(str(tmp_path))
            except Exception as exc:
                raise CorruptDocumentError(f"MSG parse failed: {exc}") from exc

            try:
                # Body preference: text/plain → HTML → RTF (which we surface as text).
                body = (msg.body or "").strip()
                source = "text/plain" if body else ""
                if not body:
                    html_body = getattr(msg, "htmlBody", None) or ""
                    if html_body:
                        body = _strip_html(
                            html_body.decode("utf-8", errors="replace")
                            if isinstance(html_body, bytes)
                            else html_body
                        )
                        source = "text/html"
                if not body:
                    rtf_body = getattr(msg, "rtfBody", None) or b""
                    if rtf_body:
                        try:
                            from striprtf.striprtf import rtf_to_text

                            body = rtf_to_text(  # type: ignore[no-untyped-call]
                                rtf_body.decode("latin-1", errors="replace")
                                if isinstance(rtf_body, bytes)
                                else rtf_body,
                                errors="ignore",
                            )
                            source = "application/rtf"
                        except ImportError:
                            pass

                text = canonicalize_text(body)
                if len(text.encode("utf-8")) > limits.max_text_bytes:
                    raise ResourceExhaustedError(
                        "text size", limits.max_text_bytes, observed=len(text)
                    )

                subject = (msg.subject or "").strip() or None
                sender = (msg.sender or "").strip() or None
                recipients = (
                    ", ".join(
                        (r.email or r.name or "").strip()
                        for r in (msg.recipients or [])
                        if (r.email or r.name)
                    )
                    or None
                )

                attachments = msg.attachments or []
                attachment_names = ",".join(
                    getattr(a, "longFilename", None)
                    or getattr(a, "shortFilename", None)
                    or "<unnamed>"
                    for a in attachments
                )

                date = msg.date
                created = (
                    date.isoformat()
                    if hasattr(date, "isoformat")
                    else (str(date) if date else None)
                )

                extra: dict[str, str | int | float | bool | None] = {}
                if sender:
                    extra["msg:from"] = sender
                if recipients:
                    extra["msg:to"] = recipients
                if msg.messageId:
                    extra["msg:message_id"] = msg.messageId.strip()
                if source:
                    extra["msg:body_source"] = source
                if attachments:
                    extra["msg:has_attachments"] = True
                    extra["msg:attachment_count"] = len(attachments)
                    extra["msg:attachment_names"] = attachment_names

                metadata = Metadata(
                    title=subject,
                    author=sender,
                    created=created,
                    word_count=word_count(text),
                    extra=extra,
                )

                return make_result(
                    text=text,
                    mime=MSG_MIME,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    filename=filename,
                    metadata=metadata,
                )
            finally:
                msg.close()
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


MIME_REGISTRY[MSG_MIME] = MsgExtractor()
