"""`text_mode="layout"` wiring in the PDF extractor (plan §4 carrier, GI-EXTRACT-03).

Byte-identity guarantees pinned here:

(a) the default `extract()` is unchanged (no `text_mode`, no new metadata keys)
    on born-digital and image PDFs;
(b) an unstructured digital prose document in layout mode is byte-identical
    to plain mode — `content.text`, `pages`, `sentences`, `word_count`;
(c) a synthetic Bilanz page yields `#` headings and ` | ` rows with the header
    repeated per pack, `content.sections` with level <= 4 and line ranges
    that index into `content.text`;
(d) the consumer contracts hold with `emit_sentences=True`;
(e) determinism: two runs and 1 vs 4 OCR workers are byte-identical;
(f) a mixed document OCRs the raster page and renders its rows (fake backend);
(g) `len(layout) <= 1.5 * len(plain) + 4096` on every fixture;
(h) `text_mode="layout"` on a DOCX without tables emits the plain text, no
    warning and no `pdf:*` keys (DOCX layout: test_extractors_docx_layout.py).

The OCR engine is the fake `FakeWordsOcrBackend` (canned word rows), so the
OCR → layout path runs on every CI leg without Tesseract.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from knovas_extract import OcrOptions, extract
from knovas_extract._layout import check_invariants
from knovas_extract._layout.lint import lint_row_line
from knovas_extract._ocr.tsv import OcrWord
from knovas_extract._pdf_layout import (
    ocr_words_to_rows,
    strip_layout_markup,
    validate_text_mode,
)
from knovas_extract.dispatch import _assert_consumer_contracts
from knovas_extract.result import ExtractionResult

pytest.importorskip("fitz")
from tests.synth.pdf_docs import (  # noqa: E402
    bilanz_pdf,
    fake_backend_for,
    prose_pdf,
    scanned_pdf,
    stamped_scan_pdf,
    three_statements_pdf,
)

pytestmark = [pytest.mark.unit]

HEADING_RE = re.compile(r"^(#{1,4}) (.+)$")
PDF = "application/pdf"
LAYOUT_KEYS = ("pdf:text_mode", "pdf:structured_pages", "pdf:layout_tables")


@pytest.fixture(scope="module")
def prose() -> bytes:
    return prose_pdf(pages=3)


@pytest.fixture(scope="module")
def bilanz() -> bytes:
    return bilanz_pdf()


@pytest.fixture(scope="module")
def statements() -> bytes:
    return three_statements_pdf()


@pytest.fixture(scope="module")
def mixed(bilanz: bytes, prose: bytes) -> tuple[bytes, bytes]:
    """(digital source, mixed PDF): prose cover page + raster Bilanz page."""
    import fitz  # type: ignore[import-untyped]

    src = fitz.open()
    src.insert_pdf(fitz.open(stream=prose, filetype="pdf"), from_page=0, to_page=0)
    src.insert_pdf(fitz.open(stream=bilanz, filetype="pdf"))
    buf = io.BytesIO()
    src.save(buf)
    src.close()
    source = buf.getvalue()
    return source, scanned_pdf(source, cover_pages=1)


def _layout(data: bytes, **kw: object) -> ExtractionResult:
    return extract(data, mime=PDF, text_mode="layout", **kw)  # type: ignore[arg-type]


def _plain(data: bytes, **kw: object) -> ExtractionResult:
    return extract(data, mime=PDF, **kw)  # type: ignore[arg-type]


def _same_output(a: ExtractionResult, b: ExtractionResult) -> None:
    assert a.content.text == b.content.text
    assert a.content.pages == b.content.pages
    assert a.content.sentences == b.content.sentences
    assert a.content.sections == b.content.sections
    assert a.metadata.word_count == b.metadata.word_count


# ── (a) default output unchanged ───────────────────────────────────────────


def test_default_extract_has_no_layout_keys_on_digital_pdf(bilanz: bytes) -> None:
    r = _plain(bilanz)
    assert not any(k in r.metadata.extra for k in LAYOUT_KEYS)
    assert r.content.sections is None
    assert " | " not in r.content.text
    assert "#" not in r.content.text
    assert r.content.text == _plain(bilanz, text_mode="plain").content.text


def test_default_extract_has_no_layout_keys_on_image_pdf(bilanz: bytes) -> None:
    scan = scanned_pdf(bilanz)
    backend = fake_backend_for(bilanz)
    r = _plain(scan, ocr=OcrOptions(backend=backend, workers=1))
    assert r.metadata.extra["pdf:ocr_pages"] == 1
    assert not any(k in r.metadata.extra for k in LAYOUT_KEYS)
    assert r.content.sections is None
    assert " | " not in r.content.text


# ── (b) unstructured prose: layout == plain ────────────────────────────────


def test_unstructured_prose_is_byte_identical_to_plain(prose: bytes) -> None:
    plain = _plain(prose, emit_sentences=True)
    layout = _layout(prose, emit_sentences=True)
    _same_output(plain, layout)
    assert layout.metadata.extra["pdf:text_mode"] == "layout"
    assert layout.metadata.extra["pdf:structured_pages"] == 0
    assert layout.metadata.extra["pdf:layout_tables"] == 0
    assert layout.metadata.word_count == plain.metadata.word_count
    assert layout.warnings == plain.warnings


# ── (c) Bilanz grammar ─────────────────────────────────────────────────────


def test_bilanz_renders_headings_and_rows(bilanz: bytes) -> None:
    r = _layout(bilanz, emit_sentences=True)
    text = r.content.text
    lines = text.split("\n")
    headings = [ln for ln in lines if ln.startswith("#")]
    rows = [ln for ln in lines if " | " in ln]
    assert headings, text
    assert all(HEADING_RE.match(h) for h in headings), headings
    assert all(len(HEADING_RE.match(h).group(1)) <= 4 for h in headings)  # type: ignore[union-attr]
    for i, ln in enumerate(lines):
        if ln.startswith("#"):
            assert i + 1 < len(lines) and lines[i + 1] == "", f"heading not followed by blank: {ln}"
    assert "\n\n\n" not in text
    assert "\f" not in text
    assert check_invariants(text) == []
    # Every amount of every row sits on one physical line with its label.
    assert "Flüssige Mittel | 2023: 1'234'567.80 | 2022: 987'654.30" in rows
    assert "Total Aktiven | 2023: 2'150'106.95 | 2022: 1'858'394.30" in rows
    header = "Aktiven | 31.12.2023 CHF | 31.12.2022 CHF"
    packs = [p for p in text.split("\n\n") if " | " in p]
    assert len(packs) >= 2, "expected several packs under the 150-token budget"
    assert all(p.split("\n")[0] == header for p in packs), packs
    assert all(lint_row_line(ln) == ln for ln in rows)
    assert r.metadata.extra["pdf:structured_pages"] == 1
    assert r.metadata.extra["pdf:layout_tables"] == 1
    # Sections: one per '#' line, level <= 4, windows index into content.text.
    sections = r.content.sections
    assert sections is not None and len(sections) == len(headings)
    for sec, h in zip(sections, headings, strict=True):
        assert 1 <= sec.level <= 4
        assert sec.level == len(HEADING_RE.match(h).group(1))  # type: ignore[union-attr]
        assert sec.line_start is not None and sec.line_end is not None
        assert 1 <= sec.line_start <= sec.line_end <= len(lines)
        assert lines[sec.line_start - 1] == h
        assert sec.heading == HEADING_RE.match(h).group(2)  # type: ignore[union-attr]
        body = "\n".join(lines[sec.line_start : sec.line_end])
        assert sec.text == body.strip("\n")
    # Sentences inside the table point at the innermost section.
    assert r.content.sentences
    row_sentences = [s for s in r.content.sentences if " | " in s.text]
    assert row_sentences and all(s.section_index is not None for s in row_sentences)


def test_bilanz_word_count_matches_plain(bilanz: bytes) -> None:
    """Markup-stripped count: fold keys and repeated pack headers do not inflate it."""
    assert _layout(bilanz).metadata.word_count == _plain(bilanz).metadata.word_count


# ── (d) consumer contracts with sentences ──────────────────────────────────


def test_consumer_contracts_hold_in_layout_mode(mixed: tuple[bytes, bytes]) -> None:
    source, data = mixed
    r = _layout(data, emit_sentences=True, ocr=OcrOptions(backend=fake_backend_for(source)))
    _assert_consumer_contracts(r)
    assert r.content.sentences
    for s in r.content.sentences:
        assert r.content.text[s.char_start : s.char_end] == s.text
        assert s.page_number is not None and s.page_index is not None
        assert s.page_number == s.page_index + 1
    assert {s.page_index for s in r.content.sentences} == {0, 1}


# ── (e) determinism ────────────────────────────────────────────────────────


def test_two_runs_are_byte_identical(statements: bytes) -> None:
    a, b = _layout(statements, emit_sentences=True), _layout(statements, emit_sentences=True)
    _same_output(a, b)
    assert a.to_dict() == b.to_dict()


def test_ocr_one_vs_four_workers_byte_identical(statements: bytes) -> None:
    scan = scanned_pdf(statements)
    outs = []
    for workers in (1, 4):
        backend = fake_backend_for(statements)
        r = _layout(scan, emit_sentences=True, ocr=OcrOptions(backend=backend, workers=workers))
        assert sorted(backend.calls) == [0, 1, 2]
        assert r.metadata.extra["pdf:ocr_pages"] == 3
        outs.append(r)
    _same_output(outs[0], outs[1])
    text = outs[0].content.text
    assert outs[0].metadata.extra["pdf:structured_pages"] == 3
    # Label and both amounts of the totals row on ONE physical line (OCR words).
    assert re.search(r"^Jahresgewinn \| .*773'900\.00 \| .*659'050\.00$", text, re.M), text
    assert "\f" not in text and check_invariants(text) == []


# ── (f) mixed document ─────────────────────────────────────────────────────


def test_mixed_document_ocrs_raster_page_and_renders_rows(mixed: tuple[bytes, bytes]) -> None:
    source, data = mixed
    backend = fake_backend_for(source)
    plain = _plain(source)  # the born-digital original
    r = _layout(data, ocr=OcrOptions(backend=backend, workers=2))
    assert backend.calls == [1]  # exactly the raster page
    assert r.metadata.extra["pdf:ocr_pages"] == 1
    assert r.metadata.extra["pdf:text_pages"] == 1
    assert r.metadata.extra["pdf:structured_pages"] == 1
    assert r.content.pages is not None and len(r.content.pages) == 2
    assert r.content.pages[0].text == plain.content.pages[0].text  # digital cover: passthrough
    page1 = r.content.pages[1].text
    assert "Flüssige Mittel | 2023: 1'234'567.80 | 2022: 987'654.30" in page1
    assert "Total Aktiven | 2023: 2'150'106.95 | 2022: 1'858'394.30" in page1
    # Headings on OCR pages follow the conservative policy (D4) and are not required.
    assert "\f" not in r.content.text and check_invariants(r.content.text) == []


def test_stamp_layer_is_kept_as_leading_paragraph_over_ocr_layout(bilanz: bytes) -> None:
    """Decision rule 4: a short scanner stamp over a full-page raster is kept and the
    OCR text appended; in layout mode the OCR words are the layout and the stamp
    text leads the page."""
    data = stamped_scan_pdf(bilanz)
    backend = fake_backend_for(bilanz)
    r = _layout(data, emit_sentences=True, ocr=OcrOptions(backend=backend))
    assert backend.calls == [0]
    page = r.content.pages[0].text  # type: ignore[index]
    assert page.startswith("Gescannt am 12.03.2024 14:33\n\n")
    assert "Total Aktiven | 2023: 2'150'106.95 | 2022: 1'858'394.30" in page
    lines = r.content.text.split("\n")
    for sec in r.content.sections or []:  # OCR headings are conservative (D4): may be none
        assert lines[sec.line_start - 1].lstrip("#").strip() == sec.heading  # type: ignore[operator]
    assert r.metadata.extra["pdf:structured_pages"] == 1
    _assert_consumer_contracts(r)


# ── (g) expansion bound ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["prose", "bilanz", "statements"])
def test_expansion_bound(request: pytest.FixtureRequest, name: str) -> None:
    data = request.getfixturevalue(name)
    plain, layout = _plain(data), _layout(data)
    assert len(layout.content.text) <= 1.5 * len(plain.content.text) + 4096
    for p, lp in zip(plain.content.pages or [], layout.content.pages or [], strict=True):
        assert len(lp.text) <= 1.5 * len(p.text) + 4096


def test_expansion_bound_mixed(mixed: tuple[bytes, bytes]) -> None:
    source, data = mixed
    plain = _plain(data, ocr=OcrOptions(backend=fake_backend_for(source)))
    layout = _layout(data, ocr=OcrOptions(backend=fake_backend_for(source)))
    assert len(layout.content.text) <= 1.5 * len(plain.content.text) + 4096


# ── (h) non-PDF input ──────────────────────────────────────────────────────


def test_docx_without_tables_in_layout_mode_matches_plain_without_warning() -> None:
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Vertrag", level=1)
    d.add_paragraph("Konto: Kontokorrent CHF. Die Parteien vereinbaren Folgendes.")
    buf = io.BytesIO()
    d.save(buf)
    data = buf.getvalue()
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    plain = extract(data, mime=mime)
    layout = extract(data, mime=mime, text_mode="layout")
    assert layout.content.text == plain.content.text
    assert layout.content.sections == plain.content.sections
    assert not any(k in layout.metadata.extra for k in LAYOUT_KEYS)
    extra = [w for w in layout.warnings if w not in plain.warnings]
    assert extra == []  # DOCX has a layout mode since 0.4.0a1; no table -> identical text


def test_invalid_text_mode_raises_value_error(bilanz: bytes) -> None:
    with pytest.raises(ValueError, match="text_mode"):
        extract(bilanz, mime=PDF, text_mode="rows")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="text_mode"):
        validate_text_mode(None)
    assert validate_text_mode("plain") == "plain" and validate_text_mode("layout") == "layout"


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_text_mode_flag(
    tmp_path: Path, bilanz: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    from knovas_extract.cli import main

    path = tmp_path / "bilanz.pdf"
    path.write_bytes(bilanz)
    assert main([str(path), "--text-mode", "layout"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["metadata"]["extra"]["pdf:text_mode"] == "layout"
    assert " | " in out["content"]["text"]
    assert main([str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "pdf:text_mode" not in out["metadata"]["extra"]


# ── helpers of _pdf_layout ─────────────────────────────────────────────────


def test_strip_layout_markup_restores_plain_word_count() -> None:
    text = (
        "# Müller AG\n\n"
        "Aktiven | 2023 CHF | 2022 CHF\n"
        "Flüssige Mittel | 2023: 1'234.00 | 2022: 987.30\n\n"
        "Aktiven | 2023 CHF | 2022 CHF\n"
        "Sachanlagen | 2023: 345'000.00 | 2022: 360'000.00\n\n"
        "- Erster Punkt\n  - Zweiter Punkt\n\n"
        "Konto: Kontokorrent CHF | IBAN: CH56 0483"
    )
    kinds = [
        "heading",
        "blank",
        "table",
        "table",
        "blank",
        "table",
        "table",
        "blank",
        "list",
        "list",
        "blank",
        "kv",
    ]
    stripped = strip_layout_markup(text, kinds)
    assert (
        stripped.split()
        == (
            "Müller AG Aktiven 2023 CHF 2022 CHF Flüssige Mittel 1'234.00 987.30 "
            "Sachanlagen 345'000.00 360'000.00 Erster Punkt Zweiter Punkt "
            "Konto: Kontokorrent CHF IBAN: CH56 0483"
        ).split()
    )
    # A non-numeric "key: value" cell in a row is not a fold and is kept.
    assert strip_layout_markup("Name: Meier | 2023: 1'200", ["table"]) == "Name: Meier 1'200"


def test_ocr_words_to_rows_treats_negative_conf_as_trusted() -> None:
    w = OcrWord("AG", 10.0, 20.0, 30.0, 32.0, -1.0, 1, 0, 2, 12.0)
    rows = ocr_words_to_rows([w])
    assert rows == [
        {
            "text": "AG",
            "bbox": [10.0, 20.0, 30.0, 32.0],
            "conf": 100.0,
            "block": 1,
            "par": 0,
            "line": 2,
        }
    ]
