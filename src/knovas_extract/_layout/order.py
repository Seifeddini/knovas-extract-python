"""Reading order: Breuel (2003) partial order with the column rule (plan R2).

``a < b`` when they x-overlap and *a* is above *b*, or *a* is left of *b* and no
block vertically between them x-overlaps both. Blocks in different prose columns
follow the column rule — the whole left column precedes the right column unless
a column-spanning block (``col == -1``: title, table, full-width paragraph)
separates them vertically. Tables are atomic. Ties resolve by ``(y0, x0)``.
"""

from __future__ import annotations

from collections.abc import Sequence

from .blocks import Block


def reading_order(blocks: Sequence[Block]) -> list[Block]:
    n = len(blocks)
    before: list[set[int]] = [set() for _ in range(n)]
    for i, a in enumerate(blocks):
        for j, b in enumerate(blocks):
            if i == j:
                continue
            xov = min(a.x1, b.x1) - max(a.x0, b.x0) > 0
            if a.col >= 0 and b.col >= 0 and a.col != b.col:
                lo, hi = (a, b) if a.col < b.col else (b, a)
                sep = any(
                    c.col == -1 and hi.y1 <= c.y0 + 0.5 and c.y1 <= lo.y0 + 0.5 for c in blocks
                )
                if (a is lo) != sep:
                    before[j].add(i)
                continue
            if xov:
                if a.y1 <= b.y0 + 0.5 or (a.y0 < b.y0 and (a.y0 + a.y1) / 2 < b.y0):
                    before[j].add(i)
            elif a.x1 <= b.x0:
                blocked = any(
                    c is not a
                    and c is not b
                    and c.y0 >= min(a.y1, b.y1) - 0.5
                    and c.y1 <= max(a.y0, b.y0) + 0.5
                    and min(c.x1, a.x1) - max(c.x0, a.x0) > 0
                    and min(c.x1, b.x1) - max(c.x0, b.x0) > 0
                    for c in blocks
                )
                if not blocked and a.y0 < b.y1:
                    before[j].add(i)
    order: list[Block] = []
    done: set[int] = set()
    while len(order) < n:
        ready = [k for k in range(n) if k not in done and not (before[k] - done)]
        if not ready:  # cycle: fall back to geometry
            ready = [
                min(
                    (k for k in range(n) if k not in done),
                    key=lambda k: (blocks[k].y0, blocks[k].x0),
                )
            ]
        k = min(ready, key=lambda k: (blocks[k].y0, blocks[k].x0))
        order.append(blocks[k])
        done.add(k)
    return order
