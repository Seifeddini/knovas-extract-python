"""Positive assertions of every consumer contract on `content.sentences`.

Each contract from the plan's "Must-have consumer guarantees" section
gets one test. Failures here are producer bugs.
"""

from __future__ import annotations

import pytest

from knovas_extract import extract

pytest.importorskip("pysbd")


_TXT = b"First sentence. Second one on line one.\nSecond line here. Third."
_HTML = (
    b"<html><body>"
    b"<h1>Report</h1><p>Body of report. Two sentences here.</p>"
    b"<h2>Sub</h2><p>Sub body. Sub body two.</p>"
    b"</body></html>"
)
_MD = b"# H\n\nOne. Two.\n\n## Sub\n\nThree. Four.\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("text/plain", _TXT),
        ("text/html", _HTML),
        ("text/markdown", _MD),
    ],
)
def test_exact_retrieval(mime: str, data: bytes) -> None:
    """content.text[char_start:char_end] == sentence.text for every sentence."""
    r = extract(data, mime=mime, emit_sentences=True)
    assert r.content.sentences is not None
    for s in r.content.sentences:
        assert r.content.text[s.char_start : s.char_end] == s.text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("text/plain", _TXT),
        ("text/html", _HTML),
        ("text/markdown", _MD),
    ],
)
def test_line_window_retrieval(mime: str, data: bytes) -> None:
    """The [line_start, line_end] window contains sentence.text as substring."""
    r = extract(data, mime=mime, emit_sentences=True)
    assert r.content.sentences is not None
    lines = r.content.text.split("\n")
    for s in r.content.sentences:
        window = "\n".join(lines[s.line_start - 1 : s.line_end])
        assert s.text in window


@pytest.mark.unit
def test_ordering_non_overlapping() -> None:
    r = extract(_TXT, mime="text/plain", emit_sentences=True)
    assert r.content.sentences is not None
    for i in range(1, len(r.content.sentences)):
        assert (
            r.content.sentences[i].char_start >= r.content.sentences[i - 1].char_end
        ), f"overlap at {i}"


@pytest.mark.unit
def test_index_monotonic_zero_based() -> None:
    r = extract(_TXT, mime="text/plain", emit_sentences=True)
    assert r.content.sentences is not None
    assert [s.index for s in r.content.sentences] == list(range(len(r.content.sentences)))


@pytest.mark.unit
def test_page_coords_null_for_non_paginated_formats() -> None:
    for mime, data in (("text/plain", _TXT), ("text/html", _HTML), ("text/markdown", _MD)):
        r = extract(data, mime=mime, emit_sentences=True)
        assert r.content.sentences is not None
        for s in r.content.sentences:
            assert s.page_index is None
            assert s.page_number is None


@pytest.mark.unit
def test_section_backpointer_html_nested_picks_innermost() -> None:
    r = extract(_HTML, mime="text/html", emit_sentences=True)
    assert r.content.sentences is not None
    assert r.content.sections is not None
    # Sub-section sentences (h2 "Sub") should point to that section, not h1.
    sub_idx = next(i for i, sec in enumerate(r.content.sections) if sec.heading == "Sub")
    sub_sentences = [s for s in r.content.sentences if s.text == "Sub body."]
    assert sub_sentences, "expected 'Sub body.' as a sentence"
    for s in sub_sentences:
        assert s.section_index == sub_idx


@pytest.mark.unit
def test_determinism_byte_identical() -> None:
    r1 = extract(_TXT, mime="text/plain", emit_sentences=True)
    r2 = extract(_TXT, mime="text/plain", emit_sentences=True)
    assert r1.to_dict() == r2.to_dict()


@pytest.mark.unit
def test_sentence_text_matches_content_text_slice_for_multi_line() -> None:
    """Multi-line sentences: line_start != line_end, char slice still exact."""
    data = b"First one on line 1. Second\nspanning\nthree lines here. Third."
    r = extract(data, mime="text/plain", emit_sentences=True)
    assert r.content.sentences is not None
    for s in r.content.sentences:
        assert r.content.text[s.char_start : s.char_end] == s.text
