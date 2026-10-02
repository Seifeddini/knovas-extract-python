"""Golden-layout case for a statement page whose table header starts with a multi-word
label (``Ertrag und Aufwand | 31.12.2023 | 31.12.2022`` under a two-word subtitle
``Erfolgsrechnung 2023``), rendered from the words of the deterministic synthetic
PDF (`tests.synth.pdf_docs.three_statements_pdf`, page 1) — once as the born-digital
text layer and once as the same words through the OCR adapter.

The committed renderings live in ``tests/fixtures/synth/``; ``KNOVAS_LAYOUT_UPDATE_GOLDEN=1``
(or ``--update-golden``) rewrites them. Pinned behaviour (R3, caption guard in
``tables._header_above``): the subtitle stays a heading of its own, the header row
keeps its multi-word label, folds the year keys into every amount cell and is
repeated on top of every pack; it is not a ``####`` section row and no header cell
is taken from the caption.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from knovas_extract import _layout as L
from knovas_extract._layout import check_invariants

pytest.importorskip("fitz")
import fitz  # noqa: E402

from tests.synth.pdf_docs import digital_words_as_ocr, three_statements_pdf  # noqa: E402

pytestmark = [pytest.mark.golden]

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "synth"
PAGE = 1  # Erfolgsrechnung 2023 — label header "Ertrag und Aufwand"
HEADER = "Ertrag und Aufwand | 31.12.2023 CHF | 31.12.2022 CHF"


def _update_mode(config: pytest.Config) -> bool:
    if os.environ.get("KNOVAS_LAYOUT_UPDATE_GOLDEN") == "1":
        return True
    try:
        return bool(config.getoption("--update-golden"))
    except ValueError:
        return False


def _render(source: str) -> tuple[str, str]:
    src = three_statements_pdf()
    doc = fitz.open(stream=src, filetype="pdf")
    page = doc[PAGE]
    plain = page.get_text("text")
    if source == "digital":
        words = L.words_from_fitz_page(page)
        rules = L.vector_rules_from_fitz_page(page)
        lay = L.build_page_text(
            words, page.rect.width, page.rect.height, ocr=False, rules=rules, page_index=PAGE
        )
    else:
        rows = [
            {
                "text": w.text,
                "bbox": [w.x0, w.y0, w.x1, w.y1],
                "conf": w.conf,
                "block": w.block,
                "par": w.par,
                "line": w.line,
            }
            for w in digital_words_as_ocr(src, PAGE)
        ]
        lay = L.build_page_text(
            L.words_from_ocr_rows(rows),
            page.rect.width,
            page.rect.height,
            ocr=True,
            page_index=PAGE,
        )
    doc.close()
    L.DocumentLayoutPass().fit([lay])
    return L.render_page(lay, plain_text=plain), plain


@pytest.mark.parametrize("source", ["digital", "ocr"])
def test_multiword_label_header_expected_output(
    request: pytest.FixtureRequest, source: str
) -> None:
    """Byte-identical to the committed rendering (determinism + regression guard)."""
    text, _plain = _render(source)
    exp = FIX / f"erfolgsrechnung_p{PAGE}.{source}.md"
    if _update_mode(request.config) or not exp.exists():
        exp.parent.mkdir(parents=True, exist_ok=True)
        exp.write_text(text, encoding="utf-8")
        if not _update_mode(request.config):
            pytest.fail(f"expected output was missing and has been written: {exp.name}; re-run")
    assert text == exp.read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["digital", "ocr"])
def test_multiword_label_header_is_folded_and_repeated(source: str) -> None:
    text, plain = _render(source)
    assert check_invariants(text, plain_len=len(plain)) == []
    packs = [b for b in text.split("\n\n") if " | " in b]
    assert len(packs) >= 2, text
    for pack in packs:
        assert pack.split("\n")[0] == HEADER, pack  # header on top of EVERY pack
        assert pack.count(HEADER) == 1
    assert (
        "Nettoerlös aus Lieferungen und Leistungen | 2023: 3'456'000.00 | 2022: 3'120'500.00"
        in text
    )
    assert "Jahresgewinn | 2023: 773'900.00 | 2022: 659'050.00" in text
    assert "#### Erfolgsrechnung 2023" not in text  # the subtitle is not a section row
    assert "CHF | CHF\n" not in text  # the stacked header line is folded, not a data row
    if source == "digital":
        assert re.search(r"^### Erfolgsrechnung 2023$", text, re.M)
    else:  # conservative OCR heading policy (D4): the subtitle stays a paragraph line
        assert "Erfolgsrechnung 2023" in text.split("\n\n")[0]
