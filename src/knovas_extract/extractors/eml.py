"""EML extractor — Python stdlib `email` backend.

RFC 5322 (Internet Message Format) parsed via stdlib `email.message_from_bytes`
+ `email.policy.default` (modern policy with proper header decoding). No
network access; no external parsers.

Security posture (see SECURITY.md):
- **Header injection** (`Subject: foo\\r\\nBcc: attacker@x`): stdlib's parser
  decodes individual header lines; we surface the parsed `subject`/`from`/`to`
  fields but never re-serialize. A CRLF in a header doesn't extend our output.
- **HTML body content**: when a multipart message offers both text/plain and
  text/html, we prefer text/plain (per RFC 2046). If only HTML is present, we
  strip tags with a deliberately-tiny regex — no external HTML parser to feed
  hostile markup.
- **Attachment payloads**: never retained in the result — only name,
  content_type, and size are surfaced under `metadata.extra`. Measuring the
  decoded size does transiently decode the part, but the decoded bytes are
  counted and discarded, never stored; this is bounded by `max_input_bytes`
  because the whole message is already resident from `message_from_bytes`.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import hashlib
import re
from email.message import EmailMessage
from typing import ClassVar, cast

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """Very small HTML→text — for emails where only text/html is available."""
    s = _TAG.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _safe_header(msg: EmailMessage, name: str) -> str | None:
    """Decoded header string, or None."""
    v = msg.get(name)
    if v is None:
        return None
    try:
        s = str(v).strip()
    except Exception:
        return None
    return s or None


def _extract_body(msg: EmailMessage) -> tuple[str, str]:
    """Return (text, source) where source is 'text/plain' or 'text/html' or ''."""
    if msg.is_multipart():
        # Prefer text/plain.
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            payload = plain.get_content()
            return str(payload), "text/plain"
        html = msg.get_body(preferencelist=("html",))
        if html is not None:
            return _strip_html(str(html.get_content())), "text/html"
        return "", ""
    # Singlepart.
    content_type = msg.get_content_type()
    payload = msg.get_content() if hasattr(msg, "get_content") else msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        payload = payload.decode(msg.get_content_charset("utf-8"), errors="replace")
    if content_type == "text/html":
        return _strip_html(str(payload)), "text/html"
    return str(payload), content_type


def _extract_html_alternative(msg: EmailMessage) -> str | None:
    """Return the raw HTML body if the message offers one, else None.

    Used only on the `emit_markdown=True` path. `cid:` / `mid:` inline
    references are refused by the shared sanitizer via the URL scheme
    allowlist — no special handling required here.
    """
    if msg.is_multipart():
        html = msg.get_body(preferencelist=("html",))
        if html is not None:
            return str(html.get_content())
        return None
    # Singlepart.
    if msg.get_content_type() == "text/html":
        payload = msg.get_content() if hasattr(msg, "get_content") else msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            payload = payload.decode(msg.get_content_charset("utf-8"), errors="replace")
        return str(payload)
    return None


def _collect_attachments(msg: EmailMessage) -> list[dict[str, str | int | None]]:
    out: list[dict[str, str | int | None]] = []
    if not msg.is_multipart():
        return out
    for part in msg.iter_attachments():
        # Best-effort metadata only. `size` requires decoding the part to
        # measure it; the decoded bytes are counted and immediately discarded
        # — never stored in the result. Per-attachment failures are non-fatal.
        name = ctype = None
        size: int | None = None
        with contextlib.suppress(Exception):
            name = part.get_filename()
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True) if hasattr(part, "get_payload") else None
            size = len(payload) if isinstance(payload, bytes) else None
        out.append({"name": name, "content_type": ctype, "size_bytes": size})
    return out


class EmlExtractor(IExtractor):
    """RFC 5322 email extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"message/rfc822"})
    name: ClassVar[str] = "eml"

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

        try:
            # email.policy.default returns an EmailMessage at runtime; the
            # stdlib stubs declare the older Message[str, str] return type,
            # which doesn't expose .get_body / .iter_attachments. Cast once.
            msg = cast(
                "EmailMessage",
                # email.policy.default has narrowed typing in recent stubs;
                # message_from_bytes still expects the wider Policy[Message].
                email.message_from_bytes(data, policy=email.policy.default),  # pyright: ignore[reportArgumentType]
            )
        except Exception as exc:
            raise CorruptDocumentError(f"EML parse failed: {exc}") from exc

        # A truly hostile EML may yield an empty headers/body — the stdlib
        # parser is famously permissive. Refuse the obvious case: not a
        # mapping AND no headers.
        if not msg.items() and not msg.get_payload():
            raise CorruptDocumentError("EML has no headers and no body")

        body_raw, body_source = _extract_body(msg)
        text = canonicalize_text(body_raw)
        if len(text.encode("utf-8")) > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

        from collections import Counter

        from knovas_extract._metadata import finalize_warnings, sanitize_scalar

        # Top-level metadata.
        subject = _safe_header(msg, "Subject")
        from_addr = _safe_header(msg, "From")
        date_hdr = _safe_header(msg, "Date")

        # Extended header surface — see plan / SECURITY.md. All values
        # routed through sanitize_scalar; CRLF-poisoned values drop out.
        _EXT_HEADERS = {
            "Reply-To": "reply_to",
            "Sender": "sender",
            "In-Reply-To": "in_reply_to",
            "References": "references",
            "Delivered-To": "delivered_to",
            "Return-Path": "return_path",
            "List-Id": "list_id",
            "List-Unsubscribe": "list_unsubscribe",
            "X-Priority": "priority",
            "Importance": "importance",
            "Authentication-Results": "auth_results",
            "Content-Language": "content_language",
        }

        attachments = _collect_attachments(msg)

        warnings: list[str] = []
        counts: Counter[str] = Counter()

        extra: dict[str, str | int | float | bool | None] = {}
        for hdr_name, tag in {
            **{"From": "from", "To": "to", "Cc": "cc", "Message-ID": "message_id"},
            **_EXT_HEADERS,
        }.items():
            v = _safe_header(msg, hdr_name)
            if v is None:
                continue
            clean = sanitize_scalar(v, limits=limits, counts=counts)
            if clean is not None:
                extra[f"eml:{tag}"] = clean

        if body_source:
            extra["eml:body_source"] = body_source
        if attachments:
            extra["eml:has_attachments"] = True
            extra["eml:attachment_count"] = len(attachments)
            names = ",".join(str(a.get("name") or "<unnamed>") for a in attachments)
            clean_names = sanitize_scalar(names, limits=limits, counts=counts)
            if clean_names is not None:
                extra["eml:attachment_names"] = clean_names
        finalize_warnings(counts, warnings)

        # Header-injection sentinel: stdlib decodes individual headers, but
        # if a user-supplied Subject was crafted to contain CRLF, flag it.
        for hdr in ("Subject", "From", "To", "Cc"):
            raw = msg.get(hdr)
            if raw and ("\r" in str(raw) or "\n" in str(raw)):
                warnings.append(
                    f"{hdr} header contains embedded newline (header-injection attempt)"
                )
                break

        metadata = Metadata(
            title=subject,
            author=from_addr,
            created=_normalize_date(date_hdr),
            word_count=word_count(text),
            extra=extra,
        )

        # Markdown path: prefer HTML alternative → sanitized markdown; else
        # the plain body is markdown-by-identity. `cid:` / `mid:` inline
        # references land in the sanitizer's URL-scheme allowlist and are
        # unwrapped / stripped there — no per-extractor handling needed.
        markdown: str | None = None
        if emit_markdown:
            html_alt = _extract_html_alternative(msg)
            if html_alt:
                from knovas_extract._markdown import check_expansion, html_to_markdown

                markdown = html_to_markdown(html_alt, limits, warnings=warnings)
                check_expansion(markdown, len(text), limits)
            else:
                markdown = text

        sentences = None
        if emit_sentences:
            from knovas_extract._sentences import split_sentences

            sentences = split_sentences(text, limits, warnings=warnings)

        return make_result(
            text=text,
            mime="message/rfc822",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            warnings=warnings,
            markdown=markdown,
            sentences=sentences,
        )


def _normalize_date(s: str | None) -> str | None:
    """RFC 2822 date string → ISO 8601, best-effort. None on parse failure."""
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    return dt.isoformat()


MIME_REGISTRY["message/rfc822"] = EmlExtractor()
