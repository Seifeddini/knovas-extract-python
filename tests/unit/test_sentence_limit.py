"""`Limits.max_sentences` caps the output instead of failing the document."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pysbd")

from knovas_extract import Limits, Page, extract  # noqa: E402
from knovas_extract._sentences import split_sentences, split_sentences_for_pages  # noqa: E402
from knovas_extract.result import Sentence  # noqa: E402

pytestmark = [pytest.mark.unit]


def test_single_text_keeps_the_first_sentences_and_warns() -> None:
    warnings: list[str] = []
    out = split_sentences(
        "Eins ist hier. Zwei ist da. Drei folgt. Vier endet.",
        Limits(max_sentences=2),
        warnings=warnings,
    )
    assert [s.text for s in out] == ["Eins ist hier.", "Zwei ist da."]
    assert "sentences: 2 beyond max_sentences (2) omitted" in warnings


def test_pages_stop_at_the_cap_and_count_the_pages_not_split() -> None:
    texts = ["Erste Seite eins. Erste Seite zwei.", "Zweite Seite eins.", "Dritte Seite eins."]
    doc = "\n\n".join(texts)
    pages = []
    line = 1
    for i, t in enumerate(texts):
        pages.append(Page(index=i, text=t, line_start=line, line_end=line))
        line += 2
    warnings: list[str] = []
    out = split_sentences_for_pages(pages, doc, Limits(max_sentences=2), warnings=warnings)
    assert len(out) == 2
    assert all(s.page_index == 0 for s in out)
    assert "sentences: 2 pages after max_sentences (2) not split" in warnings


def test_extract_no_longer_raises_and_contracts_hold() -> None:
    data = "Satz eins. Satz zwei. Satz drei. Satz vier. Satz fünf.".encode()
    r = extract(data, mime="text/plain", emit_sentences=True, limits=Limits(max_sentences=3))
    assert r.content.sentences is not None
    assert len(r.content.sentences) == 3
    assert [s.index for s in r.content.sentences] == [0, 1, 2]
    for s in r.content.sentences:
        assert r.content.text[s.char_start : s.char_end] == s.text
    assert any(w.startswith("sentences: 2 beyond max_sentences (3)") for w in r.warnings)


def test_pages_after_the_cap_are_not_segmented(monkeypatch: pytest.MonkeyPatch) -> None:
    """For paged input the cap also stops the work, not only the output."""
    segmented: list[int | None] = []

    def spy(text: str, limits: Limits, **kwargs: Any) -> list[Sentence]:
        segmented.append(kwargs.get("page_index"))
        return split_sentences(text, limits, **kwargs)

    monkeypatch.setattr("knovas_extract._sentences.split_sentences", spy)
    texts = ["Erste Seite eins. Erste Seite zwei.", "Zweite Seite eins.", "Dritte Seite eins."]
    pages = [
        Page(index=i, text=t, line_start=1 + 2 * i, line_end=1 + 2 * i) for i, t in enumerate(texts)
    ]
    split_sentences_for_pages(pages, "\n\n".join(texts), Limits(max_sentences=2), warnings=[])
    assert segmented == [0]


def _pdf(sentences_per_page: list[int]) -> bytes:
    """A born-digital PDF with one short sentence per text line."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for p, n in enumerate(sentences_per_page):
        page = doc.new_page()
        for i in range(n):
            page.insert_text((72, 72 + 14 * i), f"Seite {p + 1} Satz {i + 1} endet hier.")
    data: bytes = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (0, ["sentences: 3 pages after max_sentences (0) not split"]),
        # Inside page 2: that page keeps its prefix and page 3 is not split.
        (
            4,
            [
                "sentences: 2 beyond max_sentences (4) omitted",
                "sentences: 1 pages after max_sentences (4) not split",
            ],
        ),
        # Exactly on the page 2/3 boundary: only the pages are counted.
        (6, ["sentences: 1 pages after max_sentences (6) not split"]),
        (9, []),
    ],
)
def test_pdf_cap_is_document_wide(cap: int, expected: list[str]) -> None:
    """Until 0.4.0a1 the cap was checked per page, so a PDF was never capped."""
    data = _pdf([3, 3, 3])
    full = extract(data, mime="application/pdf", emit_sentences=True, use_ocr=False)
    r = extract(
        data,
        mime="application/pdf",
        emit_sentences=True,
        use_ocr=False,
        limits=Limits(max_sentences=cap),
    )
    assert full.content.sentences is not None
    assert len(full.content.sentences) == 9
    assert r.content.sentences is not None
    # The first `cap` sentences of the uncapped run, page coordinates included.
    assert r.content.sentences == full.content.sentences[:cap]
    assert r.content.text == full.content.text
    assert [w for w in r.warnings if "max_sentences" in w] == expected
    # The documented rule a consumer uses to tell that the list was cut short.
    cut = any(w.startswith("sentences:") and "max_sentences (" in w for w in r.warnings)
    assert cut == (cap < len(full.content.sentences))
