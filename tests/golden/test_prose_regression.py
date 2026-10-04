"""Law-firm no-regression gate at text level (plan §7, GI-EXTRACT-03).

Twenty synthetic born-digital contracts (ten one-column, ten two-column — the
two-column layout is rendered with ``page.insert_textbox`` into two frames)
from the committed deterministic generator `tests.synth.pdf_docs.lawfirm_pdf`:
numbered clauses ``Art. N …``, a ``Parteien:`` block of three lines, a running
header and footer, footnotes. No Tesseract, no external corpus.

Gate: in layout mode every page is byte-identical to plain mode (structured-page
passthrough), zero ``#`` lines, zero `` | `` rows, and the bag of words is
identical. The whole suite stays under 20 s.
"""

from __future__ import annotations

import time

import pytest

from knovas_extract import extract

pytest.importorskip("fitz")
pytest.importorskip("rapidfuzz")
from tests.eval.metrics import bow  # noqa: E402
from tests.synth.pdf_docs import LawFirmDoc, lawfirm_corpus  # noqa: E402

pytestmark = [pytest.mark.golden]

PDF = "application/pdf"
CORPUS: list[LawFirmDoc] = lawfirm_corpus(20, pages=2)
IDS = [f"seed{d.seed}-{d.columns}col" for d in CORPUS]


@pytest.fixture(scope="module")
def clock() -> dict[str, float]:
    return {"start": time.perf_counter()}


@pytest.mark.parametrize("doc", CORPUS, ids=IDS)
def test_prose_document_is_byte_identical_in_layout_mode(doc: LawFirmDoc) -> None:
    plain = extract(doc.data, mime=PDF, emit_sentences=True)
    layout = extract(doc.data, mime=PDF, emit_sentences=True, text_mode="layout")
    assert plain.content.pages is not None and len(plain.content.pages) == doc.pages
    assert layout.content.pages is not None
    for p, lp in zip(plain.content.pages, layout.content.pages, strict=True):
        assert lp.text == p.text, f"page {p.index} differs ({doc.columns}-column)"
        assert (lp.line_start, lp.line_end) == (p.line_start, p.line_end)
    assert layout.content.text == plain.content.text
    assert layout.content.sentences == plain.content.sentences
    assert layout.content.sections is None
    assert layout.metadata.word_count == plain.metadata.word_count
    assert layout.metadata.extra["pdf:structured_pages"] == 0
    assert layout.metadata.extra["pdf:layout_tables"] == 0
    lines = layout.content.text.split("\n")
    assert not [ln for ln in lines if ln.startswith("#")]
    assert not [ln for ln in lines if " | " in ln]
    assert "\f" not in layout.content.text
    assert bow(layout.content.text) == bow(plain.content.text)


def test_corpus_shape() -> None:
    assert sum(1 for d in CORPUS if d.columns == 1) == 10
    assert sum(1 for d in CORPUS if d.columns == 2) == 10
    text = extract(CORPUS[1].data, mime=PDF).content.text
    assert "Parteien:" in text and "Art. 1 " in text and "Seite 1 von 2" in text


def test_generator_is_deterministic() -> None:
    a, b = lawfirm_corpus(2), lawfirm_corpus(2)
    texts = [[extract(d.data, mime=PDF).content.text for d in c] for c in (a, b)]
    assert texts[0] == texts[1]  # the PDF trailer /ID varies; the content does not


def test_suite_budget(clock: dict[str, float]) -> None:
    """Runs last in the module: the whole gate must stay cheap enough for every leg."""
    assert time.perf_counter() - clock["start"] < 20.0
