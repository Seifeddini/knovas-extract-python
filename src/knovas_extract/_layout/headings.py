"""Heading classification and document-level level ranking (plan R1, decisions D4).

Digital pages: size ≥ 1.15 × body em → level by size rank; bold (when the
document is not mostly bold), ALL CAPS, a numbering prefix with bold or standing
alone → one level below the ranked sizes. OCR pages (conservative policy): line
height ≥ 1.3 × the body line height **plus** one more cue (numbering prefix,
standalone before a paragraph, isolated block, caps). Headings are emitted on
structured pages only (the page renderer passes through unstructured pages).
Every level is capped at 4 — the server parses ``^(#{1,4})\\s+(.+)``.
"""

from __future__ import annotations

import re
import statistics as st
from collections import Counter
from collections.abc import Mapping, Sequence

from .segments import Segment
from .words import has_letters

HEAD_NUM_RE = re.compile(
    r"^(?:§\s?\d+[a-z]?|Art\.?\s?\d+|Ziff(?:er|\.)?\s?\d+|[A-H]\.|[IVX]{1,4}\.|\d{1,2}(?:\.\d{1,2}){0,3}\.?)$"
)
MAX_LEVEL = 4
_TRAIL_PUNCT = re.compile(r"[,;:.]$")


def heading_levels(sizes: Mapping[float, int], body_em: float, *, ocr: bool) -> dict[float, int]:
    """Map the (rounded) em sizes that are headings-by-size to levels 1..3."""
    factor = 1.3 if ocr else 1.15
    big = sorted((s for s, n in sizes.items() if s >= factor * body_em and n >= 3), reverse=True)
    return {s: min(i + 1, 3) for i, s in enumerate(big)}


def body_stats(words_sizes: Counter[float], bold_chars: int) -> tuple[float, float]:
    """``(body_em, body_bold_ratio)`` from a char-weighted size histogram."""
    total = sum(words_sizes.values())
    body_em = max(words_sizes, key=lambda s: (words_sizes[s], -s)) if words_sizes else 10.0
    return body_em, (bold_chars / total if total else 0.0)


def classify_heading(
    segs: Sequence[Segment],
    *,
    body_em: float,
    body_bold_ratio: float,
    levels: Mapping[float, int],
    ocr: bool,
    standalone: bool = False,
    isolated: bool = False,
    max_level: int = MAX_LEVEL,
) -> int:
    """Heading level (1–4) for a paragraph, or 0."""
    text = " ".join(s.text for s in segs)
    words = [w for s in segs for w in s.words]
    if not words or len(words) > 12 or not 1 <= len(text) <= 120 or len(segs) > 2:
        return 0
    if _TRAIL_PUNCT.search(text) or not has_letters(text):
        return 0
    em = st.median([w.size for w in words])
    bold = sum(1 for w in words if w.bold) / len(words) >= 0.8
    numbered = bool(HEAD_NUM_RE.match(words[0].text)) and len(words) >= 2
    caps = text.isupper() and len(text) >= 4 and len(words) <= 8
    ratio = em / body_em if body_em else 1.0
    sub = min(max_level, max(levels.values(), default=1) + 1)
    if ocr:
        if ratio >= 1.3 and (numbered or standalone or isolated or caps):
            return min(max_level, levels.get(round(em * 2) / 2, 3))
        return 0
    if ratio >= 1.15:
        return min(max_level, levels.get(round(em * 2) / 2, 3))
    if (bold and body_bold_ratio < 0.5) or caps or (numbered and (bold or standalone)):
        return sub
    return 0
