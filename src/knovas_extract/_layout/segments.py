"""Segments: runs of words on a visual line that belong to one cell / column.

A gap wider than ``seg_gap_u × u`` (≈ 1 em) becomes a cell or column break only
when one of these holds (random wide gaps of justified prose are re-joined):

* a dot leader sat in the gap,
* the gap is very wide (> ``wide_gap_u × u``, ≈ 3 em),
* a *whitespace stream*: ≥ ``support_k`` of the ±3 neighbouring lines have a
  candidate gap overlapping it by ≥ 1 u,
* a vertical ruling crosses the gap,
* (OCR pages) the words left and right belong to different engine blocks —
  Tesseract already decided they are separate blocks, which replaces the
  neighbour-support requirement (second column cue, plan R2),
* a prose gutter lies inside the gap — the gap must *cover* most of the gutter
  (a word gap that merely touches the gutter's x-range on a full-width line is
  not a column break; that was the party-block interleave of the prototype).
"""

from __future__ import annotations

import statistics as st
from collections.abc import Sequence

from .lines import VisualLine, build_visual_lines
from .words import CURRENCY_TOKENS, Rule, Word, is_amount, is_dateyear


class Segment:
    """A cell/column-local run of words on one visual line."""

    __slots__ = ("col", "furniture", "leader_before", "words", "x0", "x1", "y0", "y1")

    def __init__(self, words: list[Word]) -> None:
        self.words = words
        self.leader_before = False
        self.col = 0
        self.furniture = False
        self.x0 = min(w.x0 for w in words)
        self.x1 = max(w.x1 for w in words)
        self.y0 = min(w.y0 for w in words)
        self.y1 = max(w.y1 for w in words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def n_words(self) -> int:
        return len(self.words)

    def em(self) -> float:
        return st.median([w.size for w in self.words])

    def bold_ratio(self) -> float:
        return sum(1 for w in self.words if w.bold) / len(self.words)

    def numeric(self) -> bool:
        """Amount-like cell (dates/years excluded)."""
        return all(is_amount(w.text) or w.text in CURRENCY_TOKENS for w in self.words) and any(
            is_amount(w.text) for w in self.words
        )

    def datelike(self) -> bool:
        return all(is_dateyear(w.text) or w.text in CURRENCY_TOKENS for w in self.words)

    def engine_blocks(self) -> set[int]:
        return {w.block for w in self.words}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Seg({self.text!r} @ {self.x0:.0f},{self.y0:.0f})"


def _line_unit(toks: Sequence[Word], u: float) -> float:
    cws = [w.cw() for w in toks if len(w.text) >= 2]
    ul = st.median(cws) if cws else u
    return min(max(ul, 0.6 * u), 1.6 * u)


def _vrule_between(xa: float, xb: float, y0: float, y1: float, vrules: Sequence[Rule]) -> bool:
    return any(xa < r.x < xb and min(r.y1, y1) - max(r.y0, y0) >= 0.5 * (y1 - y0) for r in vrules)


def _column_of(s: Segment, gutters: Sequence[tuple[float, float]]) -> int:
    """Prose column index by the gutter *centres* (OCR boxes may poke into a gutter);
    -1 when the segment straddles a gutter centre by more than a quarter of its width."""
    xc = (s.x0 + s.x1) / 2
    col = 0
    for g0, g1 in gutters:
        gc, tol = (g0 + g1) / 2, 0.25 * (g1 - g0)
        if s.x0 < gc - tol and s.x1 > gc + tol:
            return -1
        if xc > gc:
            col += 1
    return col


def build_segments(
    words: Sequence[Word],
    u: float,
    *,
    seg_gap_u: float = 2.0,
    support_k: int = 2,
    wide_gap_u: float = 6.0,
    gutters: Sequence[tuple[float, float]] = (),
    ocr: bool = False,
    vrules: Sequence[Rule] = (),
) -> tuple[list[Segment], list[list[Segment]]]:
    """Return ``(all segments, segments per visual line)``."""
    vlines: list[VisualLine] = build_visual_lines(words)
    cands: list[list[tuple[int, float, float, float]]] = []
    for vl in vlines:
        toks = vl.words
        ul = _line_unit(toks, u)
        cands.append(
            [
                (i, toks[i - 1].x1, toks[i].x0, ul)
                for i in range(1, len(toks))
                if toks[i].x0 - toks[i - 1].x1 > seg_gap_u * ul
            ]
        )
    segs: list[Segment] = []
    per_line: list[list[Segment]] = []
    for r, vl in enumerate(vlines):
        toks = vl.words
        breaks = set(vl.forced)
        for i in range(1, len(toks)):
            a, b = toks[i - 1], toks[i]
            gap = b.x0 - a.x1
            if gap <= 0:
                continue
            if vrules and _vrule_between(a.x1, b.x0, vl.y0, vl.y1, vrules):
                breaks.add(i)
                continue
            for g0, g1 in gutters:
                gw = max(g1 - g0, 1e-6)
                covered = (min(b.x0, g1) - max(a.x1, g0)) / gw
                centred = a.x1 <= (g0 + g1) / 2 <= b.x0
                if ocr and a.line_id == b.line_id and a.block >= 0:
                    ok = centred and gap >= 1.5 * u
                elif ocr and a.block != b.block and a.block >= 0:
                    ok = (centred or covered >= 0.5) and gap >= 0.8 * u
                else:
                    ok = (centred or covered >= 0.6) and gap >= 0.8 * u
                if ok:
                    breaks.add(i)
                    break
        for i, xa, xb, ul in cands[r]:
            if i in breaks:
                continue
            if xb - xa >= wide_gap_u * ul:
                breaks.add(i)
                continue
            if ocr and toks[i - 1].block != toks[i].block and toks[i].block >= 0:
                breaks.add(i)
                continue
            sup = sum(
                1
                for r2 in range(max(0, r - 3), min(len(vlines), r + 4))
                if r2 != r
                and any(min(xb, hb) - max(xa, ha) >= 1.0 * ul for _, ha, hb, _ in cands[r2])
            )
            if sup >= support_k:
                breaks.add(i)
        line_segs: list[Segment] = []
        cur: list[Word] = [toks[0]]
        for i in range(1, len(toks)):
            if i in breaks:
                line_segs.append(Segment(cur))
                cur = []
            cur.append(toks[i])
        line_segs.append(Segment(cur))
        pos = 0
        for k, s in enumerate(line_segs):
            if k > 0 and pos in vl.forced:
                s.leader_before = True
            pos += len(s.words)
            if gutters:
                s.col = _column_of(s, gutters)
        segs += line_segs
        per_line.append(line_segs)
    return segs, per_line
