"""Prose column gutters (1-D whitespace cover, Breuel-style), pure Python.

A gutter is an x-interval that is a word gap ≥ ``min_w_u × u`` on ≥ ``min_run``
*consecutive* visual lines, with multi-word text on both sides (median ≥ 3 words
and fewer than 30 % numeric lines on the right). Table column gaps therefore do
not qualify. Gutters are reading-order cues only: they are never cell
boundaries and never make a page a table.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Sequence
from itertools import pairwise

from .words import Word, is_numlike

_BIN = 2.0  # pt per histogram bin


def detect_gutters(
    vlines_words: Sequence[Sequence[Word]],
    u: float,
    page_w: float,
    *,
    min_run: int = 5,
    min_w_u: float = 1.5,
) -> list[tuple[float, float]]:
    n = len(vlines_words)
    if n < min_run or page_w <= 0:
        return []
    bins = max(1, int(page_w / _BIN))
    run = [0] * bins
    best = [0] * bins
    for toks in vlines_words:
        covered = bytearray(bins)
        srt = sorted(toks, key=lambda w: w.x0)
        for a, b in pairwise(srt):
            if b.x0 - a.x1 >= min_w_u * u:
                lo = max(0, min(bins, int(a.x1 / _BIN)))
                hi = max(lo + 1, min(bins, int(b.x0 / _BIN)))
                for x in range(lo, hi):
                    covered[x] = 1
        for x in range(bins):
            run[x] = run[x] + 1 if covered[x] else 0
            if run[x] > best[x]:
                best[x] = run[x]
    gutters: list[tuple[float, float]] = []
    start: int | None = None
    for x in range(bins + 1):
        on = x < bins and best[x] >= min_run
        if on and start is None:
            start = x
        if not on and start is not None:
            gx0, gx1 = start * _BIN, x * _BIN
            start = None
            if gx1 - gx0 < min_w_u * u or gx0 < 0.15 * page_w or gx1 > 0.85 * page_w:
                continue
            left_counts: list[int] = []
            right_counts: list[int] = []
            numeric_right = 0
            for toks in vlines_words:
                left = [w for w in toks if w.x1 <= gx0 + 1]
                right = [w for w in toks if w.x0 >= gx1 - 1]
                if left and right:
                    left_counts.append(len(left))
                    right_counts.append(len(right))
                    numeric_right += all(is_numlike(w.text) for w in right)
            if (
                len(left_counts) >= min_run
                and st.median(left_counts) >= 3
                and st.median(right_counts) >= 3
                and numeric_right < 0.3 * len(right_counts)
            ):
                gutters.append((gx0, gx1))
    return gutters
