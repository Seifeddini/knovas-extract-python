"""Page-level rendering tests: structured-page test, heading policy, section records,
lists, letterhead, document pass, invariants and the PyMuPDF adapters."""

from __future__ import annotations

import io
import re
from collections import Counter
from typing import Any

import pytest

from knovas_extract._layout import (
    DocStats,
    DocumentLayoutPass,
    LayoutOptions,
    Rule,
    Word,
    build_page_text,
    check_invariants,
    is_structured,
    render_page,
    render_page_detailed,
)
from knovas_extract._layout.headings import classify_heading, heading_levels
from knovas_extract._layout.lint import split_long_row
from knovas_extract._layout.render import Part, assemble, passthrough
from knovas_extract._layout.segments import Segment
from knovas_extract._layout.tables import TableGrid, TableRow, detect_tables, render_table_parts

pytestmark = [pytest.mark.unit]

CW = 5.0


def words_line(
    x: float, y: float, text: str, size: float = 10.0, bold: bool = False, **kw: Any
) -> list[Word]:
    out: list[Word] = []
    cx = x
    for i, t in enumerate(text.split(" ")):
        if i:
            cx += 0.3 * size
        out.append(Word(cx, y, cx + CW * len(t), y + size * 1.2, t, size, bold, **kw))
        cx += CW * len(t)
    return out


def right(x1: float, y: float, t: str, size: float = 10.0, bold: bool = False) -> Word:
    return Word(x1 - CW * len(t), y, x1, y + size * 1.2, t, size, bold)


def table(y0: float = 300, n: int = 4, size: float = 10.0) -> list[Word]:
    ws = [
        *words_line(50, y0, "Posten", size, bold=True),
        right(430, y0, "2023", size, True),
        right(530, y0, "2022", size, True),
    ]
    for i in range(n):
        y = y0 + 18 * (i + 1)
        ws += [
            *words_line(50, y, f"Position {i} Aufwand", size),
            right(430, y, f"{i + 1}'200.00", size),
            right(530, y, f"{i + 1}'100.00", size),
        ]
    return ws


def prose(
    y0: float, nlines: int = 4, size: float = 10.0, x0: float = 50, first: str = "Der"
) -> list[Word]:
    ws: list[Word] = []
    for i in range(nlines):
        ws += words_line(
            x0,
            y0 + 12 * i,
            f"{first if i == 0 else 'und'} Vorsitzende eroeffnet die Versammlung und stellt fest",
            size,
        )
    return ws


def bag(text: str) -> Counter[str]:
    return Counter(
        t for t in re.findall(r"\S+", text) if t not in ("|", "-") and not t.startswith("#")
    )


# ── structured-page test / passthrough ──────────────────────────────────────


def test_prose_only_page_is_unstructured_and_passes_through() -> None:
    ws = words_line(50, 60, "Grosser Titel", 16, True) + prose(100, 8)
    lay = build_page_text(ws, 595, 842, ocr=False)
    assert not is_structured(lay)
    plain = "Grosser Titel\nDer Vorsitzende …\n\n  verbatim  "
    assert render_page(lay, plain_text=plain) == plain
    assert render_page_detailed(lay, plain_text="").text == ""


def test_table_page_is_structured_and_title_becomes_heading() -> None:
    ws = words_line(50, 60, "Grosser Titel", 16, True) + table(300)
    lay = build_page_text(ws, 595, 842, ocr=False)
    assert is_structured(lay)
    rp = render_page_detailed(lay, plain_text="ignored")
    assert rp.text.startswith("# Grosser Titel\n\n")
    assert rp.sections and rp.sections[0].heading == "Grosser Titel" and rp.sections[0].level == 1
    assert rp.line_kinds[0] == "heading" and rp.line_kinds[1] == "blank"


def test_kv_block_of_three_lines_makes_page_structured() -> None:
    ws: list[Word] = []
    for i, (k, v) in enumerate(
        [("Datum:", "22.05.2024"), ("Ort:", "Zuerich"), ("Vorsitz:", "Peter Mueller")]
    ):
        ws += words_line(60, 100 + 13 * i, k) + words_line(130, 100 + 13 * i, v)
    assert is_structured(build_page_text(ws, 595, 842, ocr=False))
    two = [w for w in ws if w.y0 < 120]
    assert not is_structured(build_page_text(two, 595, 842, ocr=False))


def test_ocr_gutter_makes_page_structured_but_digital_gutter_does_not() -> None:
    ws: list[Word] = []
    for i in range(8):
        ws += words_line(50, 100 + 12 * i, "links stehender Vertragstext mit Inhalt")
        ws += words_line(320, 100 + 12 * i, "rechts stehender Vertragstext mit Inhalt")
    assert is_structured(build_page_text(ws, 595, 842, ocr=True))
    assert not is_structured(build_page_text(ws, 595, 842, ocr=False))


def test_empty_page() -> None:
    lay = build_page_text([], 595, 842, ocr=False)
    assert not is_structured(lay) and render_page(lay, plain_text="") == ""
    assert passthrough("").parts == []


# ── headings ───────────────────────────────────────────────────────────────


def test_headings_only_on_structured_pages_and_policy_none() -> None:
    ws = (
        words_line(50, 60, "Grosser Titel", 16, True)
        + words_line(50, 90, "1. Begruessung", 10.5, True)
        + prose(110, 3)
    )
    assert "#" not in render_page(
        build_page_text(ws, 595, 842, ocr=False), plain_text="Grosser Titel"
    )
    structured = ws + table(300)
    md = render_page(build_page_text(structured, 595, 842, ocr=False), plain_text="")
    assert "# Grosser Titel" in md and re.search(r"^#{2,4} 1\. Begruessung$", md, re.M)
    none = render_page(
        build_page_text(structured, 595, 842, ocr=False, opts=LayoutOptions(headings="none")),
        plain_text="",
    )
    assert "#" not in none and "Grosser Titel" in none and "1. Begruessung" in none


def test_ocr_heading_policy_needs_size_plus_one_more_cue() -> None:
    body = prose(140, 4, size=10) + table(300)
    tall_only = words_line(
        50, 100, "Irgendeine Zeile hier", 14
    )  # 1.4 × body, no other cue, glued to prose
    tall_numbered = words_line(50, 60, "Art. 1 Zweck", 14)
    md = render_page(
        build_page_text(tall_only + tall_numbered + body, 595, 842, ocr=True), plain_text=""
    )
    assert re.search(r"^#{1,4} Art\. 1 Zweck$", md, re.M)
    md2 = render_page(
        build_page_text(
            words_line(50, 100, "Zeile ohne weitere Hinweise", 12) + body, 595, 842, ocr=True
        ),
        plain_text="",
    )
    assert "# Zeile ohne" not in md2


def test_classify_heading_rules() -> None:
    def seg(text: str, size: float = 10, bold: bool = False) -> Segment:
        return Segment(words_line(50, 100, text, size, bold))

    levels = {16.0: 1, 14.0: 2, 12.0: 3}
    kw = {"body_em": 10.0, "body_bold_ratio": 0.1, "levels": levels, "ocr": False}
    assert classify_heading([seg("Titel gross", 16)], **kw) == 1
    assert classify_heading([seg("Titel mittel", 12)], **kw) == 3
    assert classify_heading([seg("Fett gesetzt", 10, True)], **kw) == 4
    assert classify_heading([seg("KAPITEL EINS", 10)], **kw) == 4
    assert classify_heading([seg("Satz endet mit Punkt.", 16)], **kw) == 0
    assert classify_heading([seg("Doppelpunkt:", 16)], **kw) == 0
    assert classify_heading([seg("12345 678", 16)], **kw) == 0
    assert classify_heading([seg("1. Nummeriert", 10)], standalone=True, **kw) == 4
    assert classify_heading([seg("1. Nummeriert", 10)], **kw) == 0
    assert classify_heading([seg(" ".join(["w"] * 13), 16)], **kw) == 0
    assert (
        classify_heading(
            [seg("OCR gross", 14)], body_em=10.0, body_bold_ratio=0.0, levels=levels, ocr=True
        )
        == 0
    )
    assert (
        classify_heading(
            [seg("OCR GROSS", 14)], body_em=10.0, body_bold_ratio=0.0, levels=levels, ocr=True
        )
        == 2
    )
    assert heading_levels({16.0: 50, 14.0: 20, 12.0: 10, 11.0: 1, 10.0: 500}, 10.0, ocr=False) == {
        16.0: 1,
        14.0: 2,
        12.0: 3,
    }
    assert heading_levels({13.0: 50, 10.0: 500}, 10.0, ocr=True) == {13.0: 1}


def test_section_records_point_at_heading_lines() -> None:
    ws = (
        words_line(50, 60, "Jahresrechnung 2023", 16, True)
        + words_line(50, 100, "Bilanz per 31.12.2023", 12, True)
        + table(140)
    )
    ws += words_line(50, 300, "Erfolgsrechnung 2023", 12, True) + table(330)
    rp = render_page_detailed(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    lines = rp.text.split("\n")
    assert [s.heading for s in rp.sections] == [
        "Jahresrechnung 2023",
        "Bilanz per 31.12.2023",
        "Erfolgsrechnung 2023",
    ]
    for s in rp.sections:
        assert lines[s.line_start] == "#" * s.level + " " + s.heading
        assert lines[s.line_start + 1] == ""
        assert s.line_end >= s.line_start and lines[s.line_end] != ""
    first, second, third = rp.sections
    assert first.line_end == len(lines) - 1  # level 1 spans the page
    assert (
        second.line_end == third.line_start - 1 - 1
    )  # ends before the blank line preceding the next level-3 heading


def test_first_page_letterhead_becomes_first_heading_and_is_kept_by_furniture() -> None:
    pages = []
    for n in range(3):
        ws = (
            words_line(60, 30, "Mueller Treuhand AG", 9)
            + words_line(280, 820, f"Seite {n + 1} von 3", 8)
            + table(200 + 10 * n)
        )
        pages.append(build_page_text(ws, 595, 842, ocr=False, page_index=n))
    dp = DocumentLayoutPass()
    stats = dp.fit(pages)
    assert stats.n_pages == 3 and stats.furniture_marked == 2 + 3
    texts = [render_page(p, plain_text="") for p in pages]
    assert texts[0].startswith("# Mueller Treuhand AG\n\n")
    assert "Mueller Treuhand AG" not in texts[1] and "Mueller Treuhand AG" not in texts[2]
    assert all("Seite" not in t for t in texts)
    rendered = dp.render(pages, ["", "", ""])
    assert [r.text for r in rendered] == texts


# ── lists, kv, rendering details ───────────────────────────────────────────


def test_bullet_list_rendering_with_nesting() -> None:
    ws = table(80)
    y = 200.0
    ws += words_line(50, y, "• Erster Punkt der Liste")
    ws += words_line(50, y + 12, "• Zweiter Punkt der Liste")
    ws += words_line(68, y + 24, "• Unterpunkt mit Einzug")
    ws += words_line(50, y + 36, "a) Enumerator bleibt stehen")
    md = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert (
        "- Erster Punkt der Liste\n- Zweiter Punkt der Liste\n  - Unterpunkt mit Einzug\n- a) Enumerator bleibt stehen"
        in md
    )


def test_rotated_margin_words_are_kept_as_trailing_paragraph() -> None:
    ws = [*table(80), Word(10, 300, 20, 400, "Randtext", 8, rotated=True)]
    rp = render_page_detailed(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert rp.parts[-1].kind == "margin" and rp.text.endswith("Randtext")


def test_table_section_rows_and_placeholders() -> None:
    grid = TableGrid(
        {},
        [
            TableRow({0: "Umlaufvermoegen"}),
            TableRow({0: "Fluessige Mittel", 2: "100.00"}),
            TableRow({0: "Vorraete", 1: "", 2: "50.00"}),
            TableRow({0: "Notiz ohne Werte"}),
        ],
        3,
    )
    parts = render_table_parts(grid, budget=150)
    assert parts[0].kind == "heading" and parts[0].text == "#### Umlaufvermoegen"
    assert "Fluessige Mittel | - | 100.00" in parts[1].text and "Notiz ohne Werte" in parts[1].text
    assert render_table_parts(grid, budget=150, section_rows=False)[0].kind == "table"
    assert split_long_row("", ["a", "b"]) == ["a | b"] and split_long_row("", []) == []


def test_assemble_cleans_blank_lines_and_builds_kinds() -> None:
    rp = assemble(
        [
            Part("para", "  erster  \n\n  "),
            Part("heading", "## Titel", 2),
            Part("table", "a | b\nc | d"),
        ],
        structured=True,
    )
    assert rp.text == "erster\n\n## Titel\n\na | b\nc | d"
    assert rp.line_kinds == ["para", "blank", "heading", "blank", "table", "table"]
    assert check_invariants(rp.text) == []


def test_check_invariants_reports_violations() -> None:
    bad = "# Titel\nkein Leerzeichen\n\n\n   \n\f\n\ttab\n##### tief"
    msgs = check_invariants(bad, plain_len=1)
    assert (
        any("three" in m for m in msgs)
        and any("form feed" in m for m in msgs)
        and any("tab" in m for m in msgs)
    )
    assert (
        any("not followed" in m for m in msgs)
        and any("level > 4" in m for m in msgs)
        and any("whitespace-only" in m for m in msgs)
    )
    assert "expansion" not in " ".join(msgs)
    assert any("expansion" in m for m in check_invariants("x" * 5000, plain_len=10))
    assert "malformed" in " ".join(check_invariants("#kein Leerzeichen"))


def test_invariants_hold_and_bag_of_words_equals_input_minus_furniture() -> None:
    ws = (
        words_line(60, 30, "Kopfzeile", 9)
        + words_line(280, 820, "Seite 2 von 3", 8)
        + table(100)
        + prose(260, 5)
    )
    ws += words_line(50, 340, "Hinweis: Zahlen sind provisorisch. Siehe Anhang.")
    pages = [
        build_page_text(
            table(100) + words_line(60, 30, "Kopfzeile", 9), 595, 842, ocr=False, page_index=0
        ),
        build_page_text(ws, 595, 842, ocr=False, page_index=1),
        build_page_text(
            table(100) + words_line(60, 30, "Kopfzeile", 9), 595, 842, ocr=False, page_index=2
        ),
    ]
    DocumentLayoutPass().fit(pages)
    rp = render_page_detailed(pages[1], plain_text="")
    assert check_invariants(rp.text) == []
    plain = "\n".join(w.text for w in ws)
    assert len(rp.text) <= 1.5 * len(plain) + 4096
    inp = Counter(w.text for w in ws)
    inp -= Counter(["Kopfzeile", "Seite", "2", "von", "3"])  # furniture
    out = bag(rp.text)
    header_extra: Counter[str] = Counter()
    for _ in range(rp.text.count("Posten | 2023 | 2022") - 1):
        header_extra.update(["Posten", "2023", "2022"])
    fold_keys = Counter({t: out[t] for t in out if t in ("2023:", "2022:")})
    assert out - header_extra - fold_keys == inp


def test_document_stats_mixed_modalities_and_lazy_fit() -> None:
    dig = build_page_text(table(100), 595, 842, ocr=False)
    ocr = build_page_text(table(100, size=11), 595, 842, ocr=True)
    stats = DocStats.from_layouts([dig, ocr])
    assert stats.digital.body_em == 10.0 and stats.ocr.body_em == 11.0
    assert stats.mode(True) is stats.ocr and stats.mode(False) is stats.digital
    assert build_page_text(table(100), 595, 842, ocr=False).doc is None  # lazy: fit on first render
    render_page(dig, plain_text="")
    fitted = dig.doc
    assert isinstance(fitted, DocStats) and dig.sections == []
    assert dig.hrules == [] and dig.vrules == []


def statement_table(
    y0: float = 300, n: int = 4, *, label: str = "Ertrag und Aufwand"
) -> list[Word]:
    """A statement table with a multi-word label header over the label column, the
    stacked ``CHF | CHF`` line and ``n`` amount rows (18 pt pitch)."""
    ws = [
        *words_line(50, y0, label, 10, bold=True),
        right(430, y0, "31.12.2023", 10, True),
        right(530, y0, "31.12.2022", 10, True),
        right(430, y0 + 18, "CHF", 10, True),
        right(530, y0 + 18, "CHF", 10, True),
    ]
    for i in range(n):
        y = y0 + 18 * (i + 2)
        ws += [
            *words_line(50, y, f"Position {i} Aufwand", 10),
            right(430, y, f"{i + 1}'200.00", 10),
            right(530, y, f"{i + 1}'100.00", 10),
        ]
    return ws


@pytest.mark.parametrize("ocr", [False, True])
def test_caption_over_table_with_own_header_keeps_multiword_label_header(ocr: bool) -> None:
    """R3 caption guard: a two-word 11 pt subtitle (under the heading-size guard) right
    above a header row whose label cell is ``Ertrag und Aufwand`` is not taken as a
    header line; the real header keeps its label, folds the year keys and is repeated
    on top of every pack, and the subtitle is not a ``####`` section row."""
    ws = (
        words_line(50, 60, "Grosser Titel", 16, True)
        + words_line(50, 270, "Erfolgsrechnung 2023", 11, True)
        + statement_table(300)
    )
    lay = build_page_text(ws, 595, 842, ocr=ocr, opts=LayoutOptions(pack_budget_tokens=70))
    md = render_page(lay, plain_text="")
    header = "Ertrag und Aufwand | 31.12.2023 CHF | 31.12.2022 CHF"
    packs = [b for b in md.split("\n\n") if " | " in b]
    assert len(packs) >= 2, md
    assert all(p.split("\n")[0] == header for p in packs), md
    assert "Position 0 Aufwand | 2023: 1'200.00 | 2022: 1'100.00" in md
    assert "#### Erfolgsrechnung 2023" not in md and "CHF | CHF\n" not in md
    assert re.search(r"^(?:#{1,3} )?Erfolgsrechnung 2023$", md, re.M)
    assert check_invariants(md) == []
    run = detect_tables(lay.vlines, lay.u, lay.page_w, ocr=ocr)[0]
    assert [s.text for s in run[0]] == ["Ertrag und Aufwand", "31.12.2023", "31.12.2022"]


def test_lone_label_over_headerless_run_is_still_taken() -> None:
    """Negative control for the caption guard: a run that opens with an amount row has
    no header of its own, so the lone label above it still joins the run (section row)."""
    ws = words_line(50, 60, "Grosser Titel", 16, True) + words_line(50, 282, "Umlaufvermoegen")
    for i in range(4):
        y = 300 + 18 * i
        ws += [
            *words_line(50, y, f"Position {i} Aufwand", 10),
            right(430, y, f"{i + 1}'200.00", 10),
            right(530, y, f"{i + 1}'100.00", 10),
        ]
    lay = build_page_text(ws, 595, 842, ocr=False)
    run = detect_tables(lay.vlines, lay.u, lay.page_w)[0]
    assert [s.text for s in run[0]] == ["Umlaufvermoegen"]
    assert "#### Umlaufvermoegen\n\nPosition 0 Aufwand | 1'200.00 | 1'100.00" in render_page(
        lay, plain_text=""
    )


def _title() -> list[Word]:
    return words_line(50, 60, "Grosser Titel", 16, True)


def _first_run(lay: Any, *, ocr: bool = False) -> list[list[str]]:
    return [[s.text for s in ln] for ln in detect_tables(lay.vlines, lay.u, lay.page_w, ocr=ocr)[0]]


def _header_on_every_pack(md: str, header: str) -> None:
    packs = [b for b in md.split("\n\n") if " | " in b]
    assert len(packs) >= 2, md
    assert all(p.split("\n")[0] == header and p.count(header) == 1 for p in packs), md
    assert check_invariants(md) == []


SMALL = LayoutOptions(pack_budget_tokens=70)
HEADER = "Ertrag und Aufwand | 31.12.2023 CHF | 31.12.2022 CHF"


@pytest.mark.parametrize("ocr", [False, True])
def test_wrapped_label_fragment_is_never_freed_into_a_heading(ocr: bool) -> None:
    """Wrapped-label fall-through: a bold 10 pt ``Ertrag und`` one row above
    ``Aufwand | 31.12.2023 | 31.12.2022`` (same em, same weight, within 1.3 × pitch,
    digit-free) is not a caption. The scan continues and the pre-guard rules take it
    as a header line — the pre-existing rendering of that shape (the grid opens
    label-only, so the header is lost) — but it is never freed into a ``### Ertrag und``
    heading at the subtitle's level: the 11 pt subtitle keeps its place on the heading
    stack of every chunk below (``#{1,3}``) and no ``#{1,3} Ertrag und`` line exists."""
    ws = (
        _title()
        + words_line(50, 270, "Erfolgsrechnung 2023", 11, True)
        + words_line(50, 286, "Ertrag und", 10, True)
        + statement_table(300, label="Aufwand")
    )
    lay = build_page_text(ws, 595, 842, ocr=ocr, opts=SMALL)
    run = _first_run(lay, ocr=ocr)
    assert run[0] == ["Ertrag und"] and run[1] == ["Aufwand", "31.12.2023", "31.12.2022"], run
    assert "Erfolgsrechnung 2023" not in [ln[0] for ln in run]  # the subtitle stays a caption
    md = render_page(lay, plain_text="")
    assert check_invariants(md) == []
    assert not re.search(r"^#{1,3} .*Ertrag und", md, re.M), md
    assert "Ertrag und" in md and md.count("Ertrag und") == 1, md
    if not ocr:
        assert re.search(r"^#{1,3} Erfolgsrechnung 2023$", md, re.M), md


def test_bare_label_over_label_less_date_header_is_not_freed_into_a_heading() -> None:
    """``Aktiven`` (bold 10 pt) one row above a label-less ``31.12.2023 | 31.12.2022``
    header: the same fall-through — never a ``### Aktiven`` heading that pops the real
    subtitle out of the heading stack."""
    ws = (
        _title()
        + words_line(50, 270, "Bilanz per 31. Dezember 2023", 11, True)
        + words_line(50, 286, "Aktiven", 10, True)
        + [right(430, 300, "31.12.2023", 10, True), right(530, 300, "31.12.2022", 10, True)]
    )
    for i in range(4):
        y = 318 + 18 * i
        ws += [
            *words_line(50, y, f"Position {i} Aktiven", 10),
            right(430, y, f"{i + 1}'200.00", 10),
            right(530, y, f"{i + 1}'100.00", 10),
        ]
    lay = build_page_text(ws, 595, 842, ocr=False, opts=SMALL)
    assert _first_run(lay)[0] == ["Aktiven"]
    md = render_page(lay, plain_text="")
    assert check_invariants(md) == []
    assert not re.search(r"^#{1,3} Aktiven$", md, re.M), md
    assert re.search(r"^#{1,3} Bilanz per 31\. Dezember 2023$", md, re.M), md


def test_lone_numeric_caption_over_header_opening_run_is_not_taken() -> None:
    """The caption guard needs no letters: a lone period ``2023`` over the label column
    (date-like, so it passes the no-amount header filter) is a caption too; taking it
    had put a label-only line on top of the grid and lost the real header."""
    ws = _title() + words_line(50, 270, "2023", 11, True) + statement_table(300)
    lay = build_page_text(ws, 595, 842, ocr=False, opts=SMALL)
    assert _first_run(lay)[0] == ["Ertrag und Aufwand", "31.12.2023", "31.12.2022"]
    md = render_page(lay, plain_text="")
    _header_on_every_pack(md, HEADER)
    assert "\n2023\n\n" in md  # the period stays a paragraph line of its own


def test_lone_stacked_word_over_a_value_column_is_a_header_line() -> None:
    """The guard is bound to the label column: a lone ``Vorjahr`` right-aligned over the
    second amount column above a header-opening run is a stacked column header and is
    still taken (merged into that column's header cell on every pack)."""
    ws = [*_title(), right(530, 282, "Vorjahr", 11, True), *statement_table(300)]
    lay = build_page_text(ws, 595, 842, ocr=False, opts=SMALL)
    assert _first_run(lay)[0] == ["Vorjahr"]
    md = render_page(lay, plain_text="")
    _header_on_every_pack(md, "Ertrag und Aufwand | 31.12.2023 CHF | Vorjahr 31.12.2022 CHF")
    assert "Vorjahr\n\n" not in md and not re.search(r"^#+ Vorjahr", md, re.M)


def test_two_cell_line_over_header_opening_run_is_taken_and_scan_stops_at_a_caption() -> None:
    """Multi-cell lines are not captions: ``Soll | Haben`` over the amount columns right
    above the header is taken as a stacked header line. Above a caption, the same line
    is out of reach: the guard ends the upward scan instead of skipping the caption."""
    soll_haben = [right(430, 282, "Soll", 10, True), right(530, 282, "Haben", 10, True)]
    lay = build_page_text(_title() + soll_haben + statement_table(300), 595, 842, ocr=False)
    assert _first_run(lay)[0] == ["Soll", "Haben"]
    assert "Ertrag und Aufwand | Soll 31.12.2023 CHF | Haben 31.12.2022 CHF" in render_page(
        lay, plain_text=""
    )
    konto = [*words_line(50, 282, "Konto"), right(530, 282, "Vorjahr")]  # regular weight
    lay = build_page_text(_title() + konto + statement_table(300), 595, 842, ocr=False)
    assert _first_run(lay)[0] == ["Konto", "Vorjahr"]  # first cell over the label column
    above_caption = [right(430, 254, "Soll", 10, True), right(530, 254, "Haben", 10, True)]
    ws = _title() + above_caption + words_line(50, 270, "Erfolgsrechnung 2023", 11, True)
    lay = build_page_text(ws + statement_table(300), 595, 842, ocr=False)
    assert _first_run(lay)[0] == ["Ertrag und Aufwand", "31.12.2023", "31.12.2022"]
    assert "Soll" in render_page(lay, plain_text="").split("Ertrag und Aufwand")[0]


def test_lone_key_column_word_over_a_kv_run_is_a_caption() -> None:
    """The fall-through needs a header *row*: over a run that opens with a
    ``Key: | value`` row, a lone same-size, digit-free word in the key column
    (``Kontoangaben`` over ``Datum: | 05.04.2024``) is a caption and is never glued into
    the first key."""
    ws = _title() + words_line(50, 282, "Kontoangaben")
    for i, (k, v) in enumerate(
        [("Datum:", "05.04.2024"), ("Zahlbar bis:", "05.05.2024"), ("Kunden-Nr.:", "10482")]
    ):
        ws += words_line(50, 300 + 18 * i, k) + words_line(150, 300 + 18 * i, v)
    lay = build_page_text(ws, 595, 842, ocr=False)
    run = _first_run(lay)
    assert run[0] == ["Datum:", "05.04.2024"] and "Kontoangaben" not in str(run)
    md = render_page(lay, plain_text="")
    assert md.count("Kontoangaben") == 1 and "Kontoangaben Datum" not in md, md


@pytest.mark.parametrize(
    ("fragment", "why"),
    [
        (words_line(50, 286, "Ertrag und", 11, True), "em differs by 10 %"),
        (words_line(50, 286, "Ertrag und", 10, False), "bold signature differs"),
        (words_line(50, 270, "Ertrag und", 10, True), "1.7 x pitch above the header"),
        (words_line(50, 286, "Ertrag und:", 10, True), "trailing colon (a kv key)"),
        (words_line(50, 286, "Ertrag 2023", 10, True), "carries a digit"),
        (words_line(50, 286, "%", 10, True), "no letters (a lone unit sign)"),
    ],
    ids=["em", "bold", "distance", "colon", "digit", "letters"],
)
def test_label_fragment_fall_through_needs_every_cue(fragment: list[Word], why: str) -> None:
    """Each cue of the wrapped-label fall-through is load-bearing: a lone label-column
    cell that misses one is a caption (never a header line), the run opens with the
    header row itself and the caption appears exactly once, outside the table."""
    ws = _title() + fragment + statement_table(300, label="Aufwand")
    lay = build_page_text(ws, 595, 842, ocr=False, opts=SMALL)
    assert _first_run(lay)[0] == ["Aufwand", "31.12.2023", "31.12.2022"], why
    md = render_page(lay, plain_text="")
    _header_on_every_pack(md, "Aufwand | 31.12.2023 CHF | 31.12.2022 CHF")
    caption = " ".join(w.text for w in fragment)
    assert md.count(caption) == 1 and " | " not in md.split("\n\n")[1], (why, md)


def test_label_fragment_is_only_asked_of_the_line_directly_above_the_header() -> None:
    """A same-size, same-weight, digit-free line two rows above the header (a caption
    between them) is never a fragment: the guard stops at the caption, neither line
    enters the run and the header is kept on every pack. What R1 then makes of the two
    bold short lines outside the table (here its pre-existing two-segment join) is the
    heading rule's business, not the guard's."""
    ws = (
        _title()
        + words_line(50, 254, "Ertrag und", 10, True)
        + words_line(50, 270, "Erfolgsrechnung 2023", 11, True)
        + statement_table(300, label="Aufwand")
    )
    lay = build_page_text(ws, 595, 842, ocr=False, opts=SMALL)
    run = _first_run(lay)
    assert run[0] == ["Aufwand", "31.12.2023", "31.12.2022"], run
    assert "Ertrag und" not in str(run) and "Erfolgsrechnung 2023" not in str(run)
    md = render_page(lay, plain_text="")
    _header_on_every_pack(md, "Aufwand | 31.12.2023 CHF | 31.12.2022 CHF")
    assert md.count("Ertrag und") == 1 and md.count("Erfolgsrechnung 2023") == 1, md


def test_rules_split_cells_and_hrules_are_exposed() -> None:
    ws = table(100)
    lay = build_page_text(ws, 595, 842, ocr=False, rules=[Rule("v", 300, 90, 300, 200), 120.0])
    assert len(lay.vrules) == 1 and len(lay.hrules) == 1
    assert "Position 0 Aufwand | 2023: 1'200.00 | 2022: 1'100.00" in render_page(lay, plain_text="")


# ── PyMuPDF adapters (skipped without the [pdf] extra) ─────────────────────


def test_fitz_adapters_words_rules_and_plain_text_passthrough() -> None:
    fitz = pytest.importorskip("fitz")
    from knovas_extract._layout import vector_rules_from_fitz_page, words_from_fitz_page

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Vertrag zwischen den Parteien", fontsize=12, fontname="helv")
    page.insert_text((72, 100), "Fett gesetzt", fontsize=12, fontname="hebo")
    for i in range(6):
        page.insert_text(
            (72, 140 + 14 * i), f"Zeile {i} des Fliesstextes ohne Struktur", fontsize=10
        )
    page.draw_line((72, 130), (400, 130))
    page.draw_rect(fitz.Rect(72, 300, 400, 340))
    buf = io.BytesIO()
    doc.save(buf)
    doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
    page = doc[0]
    words = words_from_fitz_page(page)
    assert [w.text for w in words][:4] == ["Vertrag", "zwischen", "den", "Parteien"]
    assert any(w.bold for w in words if w.text == "Fett") and not any(
        w.bold for w in words if w.text == "Vertrag"
    )
    assert {round(w.size) for w in words} == {12, 10}
    rules = vector_rules_from_fitz_page(page)
    assert [r.kind for r in rules].count("h") >= 3 and any(abs(r.y - 130) < 1 for r in rules)
    plain = page.get_text("text")
    lay = build_page_text(words, page.rect.width, page.rect.height, ocr=False, rules=rules)
    assert render_page(lay, plain_text=plain) == plain  # GI-EXTRACT-03 on a real text layer
    assert sum(len(w.text.split()) for w in words) == len(plain.split())
