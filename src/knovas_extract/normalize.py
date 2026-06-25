"""Text canonicalization — applied to every extractor's output.

The same rules MUST be applied by every language implementation; they're the
reason cross-language golden tests can compare text at all. See
clients/extraction/spec/docs/tolerances.md::Text equality for the canonical
specification.
"""
from __future__ import annotations

import re
import unicodedata

_MULTI_BLANK = re.compile(r"\n{3,}")


def canonicalize_text(s: str) -> str:
    """Apply the cross-language text-equality rules.

    1. NFC-normalize.
    2. Collapse CRLF / CR to LF.
    3. Strip trailing whitespace per line.
    4. Collapse 3+ blank lines to 2 (i.e. at most one blank line between paragraphs).
    5. Strip leading/trailing whitespace on the whole string.

    Idempotent: canonicalize_text(canonicalize_text(s)) == canonicalize_text(s).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = _MULTI_BLANK.sub("\n\n", s)
    return s.strip()


def word_count(s: str) -> int:
    """Whitespace-separated token count. Used to fill `metadata.word_count`."""
    return len(s.split()) if s else 0
