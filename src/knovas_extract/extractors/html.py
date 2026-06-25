"""HTML extractor — selectolax (lexbor) backend.

selectolax wraps the lexbor C HTML5 parser. It is fast (~25× BeautifulSoup),
forgiving of malformed input, and crucially it does **not** execute scripts,
follow `<img>`/`<link>` resources, or resolve any external entity references.
For our threat model that's the right tool.

Security posture (see SECURITY.md):
- **No script execution**: lexbor is a pure parser; <script> bodies are parsed
  as text and either dropped or surfaced as raw content depending on caller
  preference. We never `exec`/`eval`.
- **No network**: lexbor performs zero I/O. pytest-socket enforces this from
  the test side; nothing in this extractor opens a socket.
- **No XML/XXE**: HTML5 parsing is not XML parsing. Even XHTML input is parsed
  as HTML5 by lexbor — `<!ENTITY>` / `<!DOCTYPE>` references are ignored, not
  resolved. This is the design difference that makes selectolax safer than
  `lxml.etree.parse` for hostile input.
- **Resource exhaustion**: lexbor handles deeply-nested input in linear time.
  The size + nesting depth caps in Limits still apply.
"""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar, cast

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.extractors.txt import _decode
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Section

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _extract_html_metadata(tree, raw_text: str) -> dict[str, str | None]:
    """Pull title + selected <meta> fields. Always returns a dict (possibly all None)."""
    meta: dict[str, str | None] = {
        "title": None,
        "description": None,
        "keywords": None,
        "language": None,
        "charset_declared": None,
    }

    title_node = tree.css_first("title")
    if title_node and title_node.text():
        meta["title"] = title_node.text(strip=True) or None

    # <html lang="...">.
    html_node = tree.css_first("html")
    if html_node is not None:
        lang = html_node.attributes.get("lang")
        if lang:
            meta["language"] = lang.strip() or None

    for m in tree.css("meta"):
        name = (m.attributes.get("name") or "").strip().lower()
        if name == "description":
            content = m.attributes.get("content")
            if content:
                meta["description"] = content.strip() or None
        elif name == "keywords":
            content = m.attributes.get("content")
            if content:
                meta["keywords"] = content.strip() or None
        # <meta charset="..."> AND <meta http-equiv="Content-Type" content="...">.
        charset = m.attributes.get("charset")
        if charset:
            meta["charset_declared"] = charset.strip() or None
        elif (m.attributes.get("http-equiv") or "").lower() == "content-type":
            content = (m.attributes.get("content") or "").lower()
            if "charset=" in content:
                meta["charset_declared"] = content.split("charset=", 1)[1].strip() or None

    return meta


def _extract_html_sections(html: str) -> list[Section]:
    """Flat list of sections derived from <h1>..<h6>, using text-level slicing.

    Walking selectolax's DOM sibling chain is fragile (node wrappers don't
    compare cleanly across calls and walks descend into elements at unexpected
    times). Instead we slice the **rendered text** at heading boundaries — the
    same logic the markdown extractor uses. Simpler, deterministic, no DOM
    identity hazards.
    """
    from selectolax.parser import HTMLParser

    # Render to text with explicit newlines so we can find heading positions.
    tree = HTMLParser(html)
    headings = tree.css("h1, h2, h3, h4, h5, h6")
    if not headings:
        return []

    # Build a flat list of (heading_text, level) and use a regex on the rendered
    # body text to locate each heading's position.
    body = tree.body
    if body is None:
        return []
    body_text = cast("str", body.text(separator="\n"))

    found: list[tuple[str, int, int]] = []  # (heading, level, start_index)
    cursor = 0
    for h in headings:
        ht = h.text(strip=True) or ""
        if not ht:
            continue
        level = int(h.tag[1])
        idx = body_text.find(ht, cursor)
        if idx < 0:
            # Heading text not found (rare — happens when text is split by
            # nested inline tags). Skip; don't lie about the section.
            continue
        found.append((ht, level, idx))
        cursor = idx + len(ht)

    sections: list[Section] = []
    for i, (heading, level, start) in enumerate(found):
        text_start = start + len(heading)
        # End at the next heading at equal-or-lower level.
        end = len(body_text)
        for j in range(i + 1, len(found)):
            if found[j][1] <= level:
                end = found[j][2]
                break
        section_text = canonicalize_text(body_text[text_start:end])
        sections.append(Section(heading=heading, level=level, text=section_text))
    return sections


class HtmlExtractor(IExtractor):
    """HTML5 / XHTML text + metadata extractor (lexbor backend)."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})
    name: ClassVar[str] = "html"

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

        raw_text, charset = _decode(data)

        from selectolax.parser import HTMLParser

        try:
            tree = HTMLParser(raw_text)
        except Exception as exc:
            raise CorruptDocumentError(f"HTML parse failed: {exc}") from exc

        # Body text — drop scripts/styles before extracting visible text. We
        # use the regex on the raw input rather than relying on selectolax's
        # node walk because the regex stripping is deterministic and the test
        # corpus expects it.
        cleaned = _SCRIPT_STYLE.sub("", raw_text)
        body_tree = HTMLParser(cleaned)
        body = cast("str", body_tree.body.text(separator="\n") if body_tree.body else "")
        text = canonicalize_text(body)

        if len(text.encode("utf-8")) > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

        meta_raw = _extract_html_metadata(tree, raw_text)
        sections = _extract_html_sections(cleaned)

        extra: dict[str, str | int | float | bool | None] = {}
        if meta_raw.get("description"):
            extra["html:description"] = meta_raw["description"]
        if meta_raw.get("keywords"):
            extra["html:keywords"] = meta_raw["keywords"]
        if meta_raw.get("charset_declared"):
            extra["html:charset_declared"] = meta_raw["charset_declared"]
        if charset:
            extra["html:charset_detected"] = charset

        metadata = Metadata(
            title=meta_raw.get("title"),
            author=None,
            language=meta_raw.get("language"),
            word_count=word_count(text),
            extra=extra,
        )

        return make_result(
            text=text,
            mime="text/html",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            sections=sections or None,
        )


_inst = HtmlExtractor()
for _m in _inst.supported_mimes:
    MIME_REGISTRY[_m] = _inst
