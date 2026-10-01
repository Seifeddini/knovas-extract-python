"""Text blocks: union-find linking of column-local segments, side-heading attachment."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

from .segments import Segment
from .words import has_letters


class Block:
    """A reading-order unit: a text block, a table or a key-value block."""

    __slots__ = ("col", "head_prefix", "kind", "level_hint", "segs", "text", "x0", "x1", "y0", "y1")

    def __init__(
        self,
        kind: str,
        segs: Sequence[Segment] | None = None,
        text: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> None:
        self.kind = kind
        self.segs: list[Segment] = list(segs or [])
        self.text = text
        self.head_prefix: list[str] = []
        self.level_hint = 0
        if bbox:
            self.x0, self.y0, self.x1, self.y1 = bbox
        else:
            self.fit()
        cols = Counter(s.col for s in self.segs)
        self.col = (
            -1
            if (kind in ("table", "kv") or -1 in cols)
            else (max(cols, key=lambda c: (cols[c], -c)) if cols else 0)
        )

    def fit(self) -> None:
        self.x0 = min(s.x0 for s in self.segs)
        self.x1 = max(s.x1 for s in self.segs)
        self.y0 = min(s.y0 for s in self.segs)
        self.y1 = max(s.y1 for s in self.segs)

    @property
    def n_lines(self) -> int:
        return len(self.segs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block({self.kind}, {len(self.segs)} segs @ {self.x0:.0f},{self.y0:.0f})"


def link_blocks(segs: Sequence[Segment], u: float, size_tol: float = 0.2) -> list[Block]:
    """Union segments into column-local blocks: B directly below A (gap ≤ 1 line
    height), x-overlap ≥ 50 % of the narrower, same column, compatible em, and no
    smaller-font line in between (pdfminer ``line_margin`` style)."""
    ordered = sorted(segs, key=lambda s: (s.y0, s.x0))
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(ordered):
        best: tuple[int, Segment] | None = None
        a_em = a.em()
        for j in range(i + 1, min(len(ordered), i + 40)):
            b = ordered[j]
            if b.y0 - a.y1 > 1.0 * max(a.h, b.h):
                break
            if b.y0 < a.yc:
                continue
            ov = min(a.x1, b.x1) - max(a.x0, b.x0)
            if ov < 0.5 * min(a.x1 - a.x0, b.x1 - b.x0):
                continue
            b_em = b.em()
            if abs(a_em - b_em) > size_tol * max(a_em, b_em) or a.col != b.col:
                continue
            if any(
                a.yc < c.yc < b.yc
                and c.em() < 0.8 * min(a_em, b_em)
                and min(a.x1, c.x1) - max(a.x0, c.x0) > 0
                for c in ordered[i + 1 : j]
            ):
                continue
            if best is None or b.y0 < best[1].y0:
                best = (j, b)
        if best:
            parent[find(best[0])] = find(i)
    groups: dict[int, list[Segment]] = defaultdict(list)
    for i, s in enumerate(ordered):
        groups[find(i)].append(s)
    return [Block("text", sorted(g, key=lambda s: (s.y0, s.x0))) for g in groups.values()]


def attach_side_headings(blocks: Sequence[Block], u: float) -> list[Block]:
    """Short label blocks (≤ 2 lines, ≤ 5 words/line, letters) whose top aligns with a
    prose block to their right become that block's heading (prevents the
    'all side headings first' failure of column ordering)."""
    keep: list[Block] = []
    prose = [b for b in blocks if b.kind == "text" and len(b.segs) >= 2]
    for b in blocks:
        if (
            b.kind == "text"
            and len(b.segs) <= 2
            and all(s.n_words <= 5 for s in b.segs)
            and has_letters(" ".join(s.text for s in b.segs))
            and not any(s.numeric() or s.datelike() for s in b.segs)
        ):
            lh = b.segs[0].h
            tgt = [
                p
                for p in prose
                if p is not b and p.x0 >= b.x1 + 2 * u and abs(p.y0 - b.y0) < 0.6 * lh
            ]
            below = [
                c
                for c in blocks
                if c is not b
                and min(c.x1, b.x1) - max(c.x0, b.x0) > 0
                and 0 <= c.y0 - b.y1 < 2.0 * lh
            ]
            if tgt and not below:  # a column heading has its own text right below it
                tgt[0].head_prefix.append(" ".join(s.text for s in b.segs))
                continue
        keep.append(b)
    return keep
