"""Hypothesis properties of the layout renderer (plan §7 "Property, determinism").

Random word-box pages go through `build_page_text` + `render_page`:

- every input word appears exactly as often in the output as in the input
  (no furniture is generated: a single page, words inside the text band, no
  page-number / stamp patterns; fold keys and pack-header repetition are
  switched off because they copy header words by design),
- deterministic (two renders are byte-identical),
- no exception, no 3+ consecutive newlines, no ``\\f``, headings <= 4 levels
  and followed by a blank line (`check_invariants`),
- every renderer-classified row / kv line passes the row lint unchanged.
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from knovas_extract._layout import (
    LayoutOptions,
    Word,
    build_page_text,
    check_invariants,
    render_page,
    render_page_detailed,
)
from knovas_extract._layout.lint import lint_row_line

pytestmark = pytest.mark.property

PAGE_W, PAGE_H = 595.0, 842.0
CW = 5.2  # mean character width at 10 pt
OPTS = LayoutOptions(fold=False, pack_budget_tokens=10**6)

_WORD = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZäöüÄÖÜ"),
    min_size=1,
    max_size=12,
)
_AMOUNT = st.builds(
    lambda a, b, c: f"{a}'{b:03d}.{c:02d}" if a else f"{b}.{c:02d}",
    st.integers(0, 999),
    st.integers(0, 999),
    st.integers(0, 99),
)
_LABEL = st.lists(_WORD, min_size=1, max_size=5)
_LINE = st.tuples(
    _LABEL,
    st.lists(_AMOUNT, min_size=0, max_size=2),
    st.floats(-0.3, 0.3),  # vertical jitter in units of the em
    st.sampled_from([10.0, 10.0, 10.0, 12.0, 14.0]),
    st.booleans(),  # bold
)
_PAGE = st.lists(_LINE, min_size=1, max_size=12)


def _page_words(lines: list[tuple[list[str], list[str], float, float, bool]]) -> list[Word]:
    words: list[Word] = []
    y = 120.0
    for li, (label, amounts, jitter, size, bold) in enumerate(lines):
        y0 = y + jitter * size
        x = 50.0
        for t in label:
            words.append(Word(x, y0, x + CW * len(t), y0 + 1.2 * size, t, size, bold, line=li))
            x += CW * len(t) + 0.3 * size
        for k, a in enumerate(amounts):
            x1 = 420.0 + 90.0 * k
            words.append(Word(x1 - CW * len(a), y0, x1, y0 + 1.2 * size, a, size, bold, line=li))
        y += 1.6 * size
    return words


def _plain(words: list[Word]) -> str:
    by_line: dict[int, list[Word]] = {}
    for w in words:
        by_line.setdefault(w.line, []).append(w)
    return "\n".join(
        " ".join(w.text for w in sorted(ws, key=lambda w: w.x0)) for ws in by_line.values()
    )


def _render(words: list[Word]) -> tuple[str, list[str]]:
    layout = build_page_text(words, PAGE_W, PAGE_H, ocr=False, opts=OPTS)
    rp = render_page_detailed(layout, plain_text=_plain(words))
    return rp.text, rp.line_kinds


@given(lines=_PAGE)
@settings(
    max_examples=150,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_every_input_word_survives_exactly_once(lines: list) -> None:
    words = _page_words(lines)
    text, _kinds = _render(words)
    want = Counter(w.text for w in words)
    have = Counter(text.split())
    for tok, n in want.items():
        assert have[tok] == n, (tok, n, have[tok], text)


@given(lines=_PAGE)
@settings(
    max_examples=150,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_invariants_and_row_lint(lines: list) -> None:
    words = _page_words(lines)
    plain = _plain(words)
    text, kinds = _render(words)
    assert check_invariants(text, plain_len=len(plain)) == [], text
    assert "\f" not in text and "\n\n\n" not in text
    out_lines = text.split("\n")
    assert len(out_lines) == len(kinds)
    for ln, kind in zip(out_lines, kinds, strict=True):
        if kind in ("table", "kv"):
            assert lint_row_line(ln) == ln
            assert "\t" not in ln and " |  | " not in ln
            assert not ln.startswith("| ") and not ln.endswith(" |")
            assert all(c.strip() for c in ln.split(" | "))
        if ln.startswith("#"):
            assert len(ln) - len(ln.lstrip("#")) <= 4


@given(lines=_PAGE)
@settings(
    max_examples=60,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_render_is_deterministic(lines: list) -> None:
    words = _page_words(lines)
    plain = _plain(words)
    a = render_page(build_page_text(words, PAGE_W, PAGE_H, ocr=False, opts=OPTS), plain_text=plain)
    b = render_page(build_page_text(words, PAGE_W, PAGE_H, ocr=False, opts=OPTS), plain_text=plain)
    assert a == b


@given(lines=_PAGE)
@settings(
    max_examples=60,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_default_options_keep_invariants(lines: list) -> None:
    """With the production options (fold on, 150-token packs) the R10 invariants hold
    and the bag of input words is a subset of the output (header copies only add)."""
    words = _page_words(lines)
    plain = _plain(words)
    rp = render_page_detailed(build_page_text(words, PAGE_W, PAGE_H, ocr=False), plain_text=plain)
    assert check_invariants(rp.text, plain_len=len(plain)) == []
    have = Counter(rp.text.replace(" | ", " ").split())
    for w in words:
        assert have[w.text] >= 1 or w.text + ":" in rp.text, (w.text, rp.text)
