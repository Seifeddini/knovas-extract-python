"""HTML-only e-mail bodies → plain text (EML and MSG).

A deliberately small converter without an HTML parser, so hostile markup is
never built into a tree (SECURITY.md, e-mail extractors). Comments and
script/style/head/title elements go with their content, block ends become line
breaks and table cells become `` | ``; any other tag leaves a space, except an
inline one (``<b>``, ``<span>``, …), which renders without a break and vanishes.
Entities are decoded, and C0 control characters (except tab and newline), DEL
and bidi overrides are dropped, also when a numeric character reference
smuggles them in (Trojan Source, SECURITY.md promise #7). Linear in the input.
"""

from __future__ import annotations

import html
import re

from knovas_extract._metadata import _BIDI_OVERRIDES

# Tag names compare ASCII-only, as in HTML: under plain re.I "<ſcript" (U+017F)
# would match too, and its name would be no key of _BLOCK_CLOSE.
_BLOCK_OPEN = re.compile(r"<(script|style|head|title)\b", re.I | re.A)
_BLOCK_CLOSE = {n: re.compile("</" + n, re.I | re.A) for n in ("script", "style", "head", "title")}
_PARA_END = re.compile(r"</(?:p|div|table|blockquote|ul|ol|h[1-6])\s*>", re.I)
_LINE_END = re.compile(r"<br\b[^<>]*>|</(?:tr|li)\s*>", re.I)
_CELL_END = re.compile(r"</t[dh]\s*>", re.I)
# `[^<>]*` (not `[^>]+`): an attempt stops at the next "<", so input full of
# unclosed brackets stays linear instead of rescanning to the end each time.
_TAG = re.compile(r"<[^<>]*>")
_INLINE_TAG = re.compile(
    r"</?(?:a|abbr|b|big|cite|code|em|font|i|kbd|mark|nobr|q|s|samp|small|span|strike|strong"
    r"|sub|sup|tt|u|var|wbr)[\s/>]",
    re.I | re.A,
)
_SPACES = re.compile(r"[^\S\n]+")  # every whitespace run except newlines, NBSP and U+2003 included
_BLANKS = re.compile(r"\n{3,}")
# html.unescape() turns a decimal reference into a number with int(), which refuses
# more than 4300 digits (a bare ValueError). References of eight or more digits are
# rewritten first: leading zeros dropped, and a number still longer than seven digits
# (above U+10FFFF, which unescape() maps to U+FFFD) becomes the first one out of range.
# ASCII digits only, as in unescape() itself.
_LONG_DEC_REF = re.compile(r"&#([0-9]{8,})")
# One regex pass: a per-character filter built a str object for every character
# outside Latin-1. C1 (0x80-0x9F) stays: a body labelled ISO-8859-1 but written in
# cp1252 decodes its dashes, quotes and "…" (U+0085, whitespace) there, and
# unescape() maps references in that range to cp1252 (&#x9b; is "›", not the CSI).
_DROP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f" + re.escape("".join(sorted(_BIDI_OVERRIDES))) + "]")


def _drop_comments(s: str) -> str:
    """Remove ``<!-- … -->``; an unterminated comment swallows the rest (linear)."""
    out: list[str] = []
    pos = 0
    while True:
        start = s.find("<!--", pos)
        if start < 0:
            out.append(s[pos:])
            return "".join(out)
        out.append(s[pos:start])
        end = s.find("-->", start + 4)
        if end < 0:
            return "".join(out)
        pos = end + 3


def _drop_blocks(s: str) -> str:
    """Remove script/style/head/title elements with their content; an unclosed
    one swallows the rest. One forward scan, no backtracking."""
    out: list[str] = []
    pos = 0
    while True:
        m = _BLOCK_OPEN.search(s, pos)
        if m is None:
            out.append(s[pos:])
            return "".join(out)
        out.append(s[pos : m.start()])
        # Searched in `s` itself: "İ".lower() is two code points, so an offset found
        # in s.lower() lands past the end tag and cuts the text that follows it.
        end = _BLOCK_CLOSE[m.group(1).lower()].search(s, m.end())
        if end is None:
            return "".join(out)
        close = s.find(">", end.end())
        pos = len(s) if close < 0 else close + 1


def _short_dec_ref(m: re.Match[str]) -> str:
    digits = m.group(1).lstrip("0") or "0"
    return "&#" + (digits if len(digits) <= 7 else "1114112")


def _tag_gap(m: re.Match[str]) -> str:
    # Inline tags render without a break, so they vanish (`CO<sub>2</sub>` stays one
    # word); any other tag leaves a space, so the words around a block start, an <hr>
    # or an omitted end tag never run together. Choosing per match keeps a single
    # pass: removing the inline tags first would join a stray "<" to a later ">".
    return "" if _INLINE_TAG.match(m.string, m.start(), m.end()) else " "


def _clean_line(line: str) -> str:
    # After the sub the only whitespace left is single spaces, so one strip trims the
    # cell separators and their spaces off both ends in one linear pass (slicing off
    # one "|" at a time was quadratic on a long run of them).
    return _SPACES.sub(" ", line).strip(" |")


def html_to_text(s: str) -> str:
    """Plain text of an HTML e-mail body: words, paragraphs, lines and cells kept."""
    s = _drop_blocks(_drop_comments(s))
    s = _CELL_END.sub(" | ", s)
    s = _PARA_END.sub("\n\n", s)
    s = _LINE_END.sub("\n", s)
    s = _TAG.sub(_tag_gap, s)
    s = html.unescape(_LONG_DEC_REF.sub(_short_dec_ref, s))
    s = _DROP.sub("", s)
    return _BLANKS.sub("\n\n", "\n".join(_clean_line(ln) for ln in s.split("\n"))).strip()
