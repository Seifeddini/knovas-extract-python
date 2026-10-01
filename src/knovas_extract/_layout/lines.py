"""Visual lines: words grouped by vertical overlap, dot leaders turned into forced breaks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise

from .words import LEADER_RE, TRAIL_LEADER_RE, Word


@dataclass(slots=True)
class VisualLine:
    """Words of one visual line (sorted by x0). ``forced`` holds the indices *i* such
    that a cell break is forced before ``words[i]`` (a dot leader was there)."""

    words: list[Word]
    forced: set[int] = field(default_factory=set)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)


@dataclass(slots=True)
class _Open:
    y0: float
    y1: float
    yc: float
    size: float
    words: list[Word]


def build_visual_lines(words: Sequence[Word]) -> list[VisualLine]:
    """Group words into visual lines.

    A word joins an open line when the vertical overlap is ≥ 50 % of the smaller
    box, the centre offset is < 0.6 × the larger height and the em differs by at
    most 45 %. Rotated words are skipped (the page keeps them separately).
    Leader words (``......``, ``____``) are removed and force a break; a trailing
    leader glued to a word (``Nettoeinkommen.....``) is stripped off a *copy* of
    the word and forces a break after it.
    """
    ws = sorted((w for w in words if not w.rotated), key=lambda w: (w.yc, w.x0))
    lines: list[_Open] = []
    for w in ws:
        placed = False
        for ln in reversed(lines[-6:]):
            ov = min(ln.y1, w.y1) - max(ln.y0, w.y0)
            if (
                ov >= 0.5 * min(ln.y1 - ln.y0, w.h)
                and abs(ln.yc - w.yc) < 0.6 * max(w.h, ln.y1 - ln.y0)
                and abs(w.size - ln.size) <= 0.45 * max(w.size, ln.size)
            ):
                ln.words.append(w)
                ln.y0 = min(ln.y0, w.y0)
                ln.y1 = max(ln.y1, w.y1)
                placed = True
                break
        if not placed:
            lines.append(_Open(w.y0, w.y1, w.yc, w.size, [w]))
    out: list[VisualLine] = []
    for ln in lines:
        toks: list[Word] = []
        forced: set[int] = set()
        for w in sorted(ln.words, key=lambda w: w.x0):
            if LEADER_RE.match(w.text):
                forced.add(len(toks))
                continue
            m = TRAIL_LEADER_RE.match(w.text)
            if m:
                toks.append(replace(w, text=m.group(1)))
                forced.add(len(toks))
                continue
            toks.append(w)
        if toks:
            out.append(VisualLine(toks, {i for i in forced if 0 < i < len(toks)}))
    return out


def median_pitch(vlines: Sequence[VisualLine], default: float = 12.0) -> float:
    """Median baseline distance between consecutive visual lines."""
    ds = [b.y0 - a.y0 for a, b in pairwise(vlines) if b.y0 > a.y0]
    if not ds:
        return default
    ds.sort()
    return ds[len(ds) // 2]
