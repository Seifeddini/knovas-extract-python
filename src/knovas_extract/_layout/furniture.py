"""Running headers/footers, page numbers and scanner stamps (plan R7).

A segment in the top or bottom ``band`` of a page is furniture when

* it matches a page-number pattern (``Seite 3 von 12``, ``- 4 -``, ``page 2/9``),
* it looks like a scanner stamp (``Scanned 12.03.2024 09:41``, ``Eingegangen …``),
* its digit-masked text repeats in the same band on ≥ ``max(2, ceil(repeat_frac·n))``
  pages — then every occurrence except the *first* is dropped, so a first-page
  letterhead that later becomes a running header is never lost.

Furniture is removed before layout analysis; it still appears in plain mode
(passthrough pages are byte-identical to plain mode and keep it).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

from .segments import Segment

PAGENO_RE = re.compile(
    r"^(?:(?:seite|page|pag\.?|p\.|s\.)\s*)?[-–]?\s*\d{1,4}\s*[-–]?"  # noqa: RUF001
    r"(?:\s*(?:von|of|/|de|di|sur)\s*\d{1,4})?$",
    re.I,
)
STAMP_RE = re.compile(
    r"^(?:gescannt|scanned|scan\b|eingegangen|received|fax\s*(?:from|von))"
    r"|\d{1,2}[./]\d{1,2}[./]\d{2,4}\s+\d{1,2}:\d{2}",
    re.I,
)
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def _norm(t: str) -> str:
    return _DIGITS.sub("#", _WS.sub(" ", t.lower())).strip()


def is_page_number(text: str) -> bool:
    return bool(PAGENO_RE.match(text.strip()))


def is_scanner_stamp(text: str) -> bool:
    return bool(STAMP_RE.search(text.strip()))


def detect_furniture(
    pages_vlines: Sequence[Sequence[Sequence[Segment]]],
    page_heights: Sequence[float],
    *,
    band: float = 0.09,
    repeat_frac: float = 0.4,
) -> int:
    """Set ``Segment.furniture`` on furniture segments; return how many were marked."""
    n = len(pages_vlines)
    counts: Counter[tuple[str, bool]] = Counter()
    cands: list[tuple[int, Segment, tuple[str, bool]]] = []
    for pi, (vl, height) in enumerate(zip(pages_vlines, page_heights, strict=True)):
        seen: set[tuple[str, bool]] = set()
        for line in vl:
            for s in line:
                if s.y1 < band * height or s.y0 > (1 - band) * height:
                    key = (_norm(s.text), s.y0 < 0.5 * height)
                    cands.append((pi, s, key))
                    if key not in seen:
                        seen.add(key)
                        counts[key] += 1
    need = max(2, math.ceil(repeat_frac * n))
    first_seen: dict[tuple[str, bool], int] = {}
    marked = 0
    for pi, s, key in cands:
        text = s.text.strip()
        if is_page_number(text) or is_scanner_stamp(text):
            s.furniture = True
            marked += 1
        elif n >= 2 and counts[key] >= need:
            first_seen.setdefault(key, pi)
            if first_seen[key] != pi:
                s.furniture = True
                marked += 1
    return marked
