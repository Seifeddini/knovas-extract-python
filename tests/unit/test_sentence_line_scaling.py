"""Line-coordinate computation must stay linear in text length.

`split_sentences` previously counted newlines from index 0 for every
sentence, which is O(n^2) and stalls on large, weakly-punctuated inputs
(tariff tables, log dumps). These tests pin both the correctness of the
running count and its scaling behaviour.
"""

from __future__ import annotations

import time

import pytest

from knovas_extract import extract

pytest.importorskip("pysbd")


def _naive_line(text: str, char_index: int) -> int:
    return 1 + text.count("\n", 0, char_index)


@pytest.mark.unit
def test_line_coords_match_naive_computation() -> None:
    raw = (
        "Alpha one. Alpha two.\n"
        "Beta one. Beta two.\n"
        "\n"
        "Gamma one.\nGamma two. Gamma three.\n"
    ).encode("utf-8")

    result = extract(raw, mime="text/plain", emit_sentences=True)
    text = result.content.text
    sentences = result.content.sentences
    assert sentences

    for s in sentences:
        assert s.line_start == _naive_line(text, s.char_start)
        assert s.line_end == _naive_line(text, s.char_end)


@pytest.mark.unit
def test_running_line_count_stays_correct_over_many_lines() -> None:
    """The incremental counter must not drift as the cursor advances."""
    raw = ("\n".join(f"Zeile {i} erste. Zeile {i} zweite." for i in range(500))).encode("utf-8")

    result = extract(raw, mime="text/plain", emit_sentences=True)
    text = result.content.text
    sentences = result.content.sentences
    assert sentences
    assert len(sentences) > 500

    for s in sentences:
        assert s.line_start == _naive_line(text, s.char_start)
        assert s.line_end == _naive_line(text, s.char_end)

    # Line numbers must be non-decreasing across the document.
    starts = [s.line_start for s in sentences]
    assert starts == sorted(starts)


@pytest.mark.unit
def test_large_weakly_punctuated_text_stays_linear() -> None:
    """Quadratic line counting turned this into minutes of CPU."""
    # Tariff-table shape: many short newline-separated records.
    raw = ("\n".join(f"{i:06d} Tarif Position. Betrag {i}." for i in range(20000))).encode("utf-8")

    start = time.perf_counter()
    result = extract(raw, mime="text/plain", emit_sentences=True)
    elapsed = time.perf_counter() - start

    assert result.content.sentences
    # Generous bound: the quadratic version took minutes on this input.
    assert elapsed < 30, f"sentence splitting took {elapsed:.1f}s"
