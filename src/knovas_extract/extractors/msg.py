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
        emit_markdown: bool = False,
        emit_sentences: bool = False,
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
                # Capture raw htmlBody separately so the emit_markdown path can
                # feed sanitized HTML through the shared markdown helper.
                body = (msg.body or "").strip()
                source = "text/plain" if body else ""
                raw_html_body: str = ""
                raw_html_candidate = getattr(msg, "htmlBody", None) or ""
                if raw_html_candidate:
                    raw_html_body = (
                        raw_html_candidate.decode("utf-8", errors="replace")
                        if isinstance(raw_html_candidate, bytes)
                        else raw_html_candidate
                    )
                if not body and raw_html_body:
                    body = _strip_html(raw_html_body)
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

                from collections import Counter

                from knovas_extract._metadata import finalize_warnings, sanitize_scalar

                warnings: list[str] = []
                counts: Counter[str] = Counter()

                extra: dict[str, str | int | float | bool | None] = {}
                for key, raw_val in (
                    ("msg:from", sender),
                    ("msg:to", recipients),
                    ("msg:message_id", (msg.messageId or "").strip() if msg.messageId else None),
                    ("msg:cc", getattr(msg, "cc", None)),
                    ("msg:bcc", getattr(msg, "bcc", None)),
                    ("msg:reply_to", getattr(msg, "replyTo", None)),
                    ("msg:in_reply_to", getattr(msg, "inReplyTo", None)),
                    ("msg:conversation_topic", getattr(msg, "conversationTopic", None)),
                    ("msg:conversation_index", getattr(msg, "conversationIndex", None)),
                    ("msg:categories", getattr(msg, "categories", None)),
                    (
                        "msg:sent_representing",
                        getattr(msg, "sentRepresentingName", None)
                        or getattr(msg, "sentRepresentingEmail", None),
                    ),
                ):
                    if raw_val is None:
                        continue
                    clean = sanitize_scalar(raw_val, limits=limits, counts=counts)
                    if clean is not None:
                        extra[key] = clean

                # Enum-ish fields — surface as ints when present.
                importance = getattr(msg, "importance", None)
                if isinstance(importance, int):
                    extra["msg:importance"] = importance
                sensitivity = getattr(msg, "sensitivity", None)
                if isinstance(sensitivity, int):
                    extra["msg:sensitivity"] = sensitivity

                if source:
                    extra["msg:body_source"] = source
                if attachments:
                    extra["msg:has_attachments"] = True
                    extra["msg:attachment_count"] = len(attachments)
                    clean_names = sanitize_scalar(attachment_names, limits=limits, counts=counts)
                    if clean_names is not None:
                        extra["msg:attachment_names"] = clean_names

                finalize_warnings(counts, warnings)

                metadata = Metadata(
                    title=subject,
                    author=sender,
                    created=created,
                    word_count=word_count(text),
                    extra=extra,
                )

                # Markdown path: prefer HTML body → sanitized markdown,
                # else plain body as identity. RTF-only bodies leave
                # markdown=None + warning (matches the RTF extractor's
                # position — striprtf preserves no structure).
                markdown: str | None = None
                if emit_markdown:
                    if raw_html_body:
                        from knovas_extract._markdown import (
                            check_expansion,
                            html_to_markdown,
                        )

                        markdown = html_to_markdown(raw_html_body, limits, warnings=warnings)
                        check_expansion(markdown, len(text), limits)
                    elif source == "text/plain" and text:
                        markdown = text
                    else:
                        warnings.append("msg: only RTF body available; content.markdown left null")

                sentences = None
                if emit_sentences:
                    from knovas_extract._sentences import split_sentences

                    sentences = split_sentences(text, limits, warnings=warnings)

                return make_result(
                    text=text,
                    mime=MSG_MIME,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    filename=filename,
                    metadata=metadata,
                    warnings=warnings or None,
                    markdown=markdown,
                    sentences=sentences,
                )
            finally:
                msg.close()
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


MIME_REGISTRY[MSG_MIME] = MsgExtractor()
