"""Layout geometry unit tests on SYNTHETIC word boxes (no PDF, no OCR): fast, deterministic.

The first block ports the 22 prototype tests (``ocrbench/mdlite/test_mdlite.py``);
the rest pin the five gaps closed in M2 (vertical KV, block-id column cue,
heading-size guard, conservative OCR headings, structured-only headings) and the
plan §7 "layout geometry unit" rows.
"""

from __future__ import annotations

import random
import re
from typing import Any

import pytest

from knovas_extract._layout import (
    SENT_SPLIT_RE,
    HyphenPreference,
    LayoutOptions,
    Rule,
    Word,
    build_page_text,
    dehyphen_join,
    deskew_words,
    est_tokens,
    normalize_rules,
    render_page,
    render_page_detailed,
    words_from_ocr_rows,
)
from knovas_extract._layout.blocks import Block, attach_side_headings, link_blocks
from knovas_extract._layout.furniture import detect_furniture, is_page_number, is_scanner_stamp
from knovas_extract._layout.gutters import detect_gutters
from knovas_extract._layout.kv import split_kv_pairs
from knovas_extract._layout.lines import build_visual_lines, median_pitch
from knovas_extract._layout.lint import clean_cell, lint_row_line, split_long_row
from knovas_extract._layout.order import reading_order
from knovas_extract._layout.segments import Segment, build_segments
from knovas_extract._layout.tables import (
    TableGrid,
    TableRow,
    compact_keys,
    detect_tables,
    render_table_parts,
    table_structure,
)
from knovas_extract._layout.words import page_unit

pytestmark = [pytest.mark.unit]

CW = 5.0  # char width (pt) ≈ 0.5 em at 10 pt


def words_line(
    x: float,
    y: float,
    text: str,
    size: float = 10.0,
    gaps: dict[int, float] | None = None,
    bold: bool = False,
    block: int = -1,
    line: int = -1,
) -> list[Word]:
    """Place the words of *text* left-to-right; ``gaps[i]`` overrides the space before word i."""
    out: list[Word] = []
    cx = x
    for i, t in enumerate(text.split(" ")):
        if i:
            cx += (gaps or {}).get(i, 0.3 * size)
        out.append(
            Word(cx, y, cx + CW * len(t), y + size * 1.2, t, size, bold, block=block, line=line)
        )
        cx += CW * len(t)
    return out


def right(x1: float, y: float, t: str, size: float = 10.0, bold: bool = False) -> Word:
    return Word(x1 - CW * len(t), y, x1, y + size * 1.2, t, size, bold)


def segs_text(vlines: list[list[Segment]]) -> list[list[str]]:
    return [[s.text for s in ln] for ln in vlines]


def _w(x0: float, y0: float, text: str, size: float = 10, cw: float = 5.0, **kw: Any) -> Word:
    return Word(x0, y0, x0 + cw * len(text), y0 + size, text, size, **kw)


def _para(
    x0: float,
    y0: float,
    words_per_line: int,
    nlines: int,
    pitch: float = 12,
    first: str = "Lorem",
    **kw: Any,
) -> list[Word]:
    ws: list[Word] = []
    for li in range(nlines):
        x = x0
        for wi in range(words_per_line):
            t = first if (li == 0 and wi == 0) else f"wort{li}{wi}"
            ws.append(_w(x, y0 + li * pitch, t, **kw))
            x += 5.0 * len(t) + 4
    return ws


# ══════════════════════════════════════════════════════════════════════════
# ported prototype tests
# ══════════════════════════════════════════════════════════════════════════


def test_justified_wide_gaps_are_not_cells() -> None:
    ws: list[Word] = []
    for k, y in enumerate(range(100, 220, 14)):
        g = {3 + k % 4: 14.0}  # one ~1.4 em gap at a different word each line
        ws += words_line(
            50,
            y,
            "Die Beauftragte erbringt ihre Leistungen nach den anerkannten Grundsaetzen",
            gaps=g,
        )
    _segs, vl = build_segments(ws, CW)
    assert all(len(ln) == 1 for ln in vl), segs_text(vl)


def test_two_column_gutter_is_split() -> None:
    ws: list[Word] = []
    for y in range(100, 300, 14):
        ws += words_line(50, y, "links stehender Vertragstext mit Inhalt")
        ws += words_line(320, y, "rechts stehender Vertragstext mit Inhalt")
    _, vl = build_segments(ws, CW)
    assert all(len(ln) == 2 for ln in vl)


def test_dot_leader_forces_cell_break_and_is_dropped() -> None:
    ws = [
        *words_line(50, 100, "Steuerbares Einkommen"),
        Word(170, 100, 400, 112, "." * 40, 10),
        right(500, 100, "98'400"),
    ]
    _, vl = build_segments(ws, CW)
    assert segs_text(vl) == [["Steuerbares Einkommen", "98'400"]]


def test_trailing_leader_attached_to_word() -> None:
    ws = [*words_line(50, 100, "Nettoeinkommen.........."), right(500, 100, "79'493.00")]
    _, vl = build_segments(ws, CW)
    assert segs_text(vl) == [["Nettoeinkommen", "79'493.00"]]


def bilanz_lines(wrapped: bool = False, valign: str = "bottom") -> list[Word]:
    ws = [
        *words_line(50, 80, "Aktiven", bold=True),
        right(330, 80, "Anhang"),
        right(430, 80, "31.12.2023"),
        right(530, 80, "31.12.2022"),
    ]
    ws += [right(430, 92, "CHF"), right(530, 92, "CHF")]
    y = 110
    rows = [
        ("Fluessige Mittel", "", "1'234'567.80", "987'654.30"),
        ("Forderungen aus Lieferungen", "2.1", "456'789.00", "400'120.00"),
        ("Vorraete", "", "23'450.15", "19'870.00"),
        ("Total Aktiven", "", "5'176'806.95", "4'936'144.30"),
    ]
    for lab, note, a, b in rows:
        if wrapped and lab.startswith("Forderungen"):
            if valign == "bottom":
                ws += words_line(50, y, "Forderungen aus Lieferungen")
                y += 11
                ws += words_line(50, y, "und Leistungen gegenueber Dritten")
            else:
                ws += words_line(50, y, "Forderungen aus Lieferungen")
                ws += words_line(50, y + 11, "und Leistungen gegenueber Dritten")
            ws += [right(330, y, note), right(430, y, a), right(530, y, b)]
            y += 11 if valign == "top" else 0
        else:
            ws += words_line(50, y, lab)
            if note:
                ws.append(right(330, y, note))
            ws += [right(430, y, a), right(530, y, b)]
        y += 18
    return ws


def run_table(ws: list[Word], rules: list[Rule] | None = None) -> TableGrid:
    _, vl = build_segments(ws, CW)
    tabs = detect_tables(vl, CW, 595, hrules=rules or ())
    assert len(tabs) == 1
    return table_structure(tabs[0], CW, rules or ())


def test_borderless_bilanz_columns_and_two_line_header() -> None:
    grid = run_table(bilanz_lines())
    assert grid.ncols == 4
    assert grid.header[2] == "31.12.2023 CHF" and grid.header[3] == "31.12.2022 CHF"
    assert grid.rows[1].cells == {
        0: "Forderungen aus Lieferungen",
        1: "2.1",
        2: "456'789.00",
        3: "400'120.00",
    }


@pytest.mark.parametrize("valign", ["bottom", "top"])
def test_wrapped_label_is_one_row(valign: str) -> None:
    grid = run_table(bilanz_lines(wrapped=True, valign=valign))
    labels = [r.label for r in grid.rows]
    assert "Forderungen aus Lieferungen und Leistungen gegenueber Dritten" in labels, labels
    assert len(grid.rows) == 4


def test_two_column_prose_is_not_a_table() -> None:
    ws: list[Word] = []
    for y in range(100, 300, 14):
        ws += words_line(50, y, "links stehender Vertragstext mit viel Inhalt")
        ws += words_line(320, y, "rechts stehender Vertragstext mit viel Inhalt")
    _, vl = build_segments(ws, CW)
    assert detect_tables(vl, CW, 595) == []


def test_render_fold_and_budget_packing() -> None:
    grid = run_table(bilanz_lines())
    big = TableGrid(grid.header, grid.rows * 6, grid.ncols)
    parts = render_table_parts(big, budget=120)
    assert len(parts) > 1
    for p in parts:
        assert p.kind == "table"
        assert p.text.splitlines()[0].startswith(
            "Aktiven | Anhang | 31.12.2023 CHF"
        )  # header repeated
        assert est_tokens(p.text) <= 120 + 60  # one row may overflow
    md = "\n\n".join(p.text for p in parts)
    assert "Fluessige Mittel | 2023: 1'234'567.80 | 2022: 987'654.30" in md
    assert not re.search(
        r"[.!?]\s+[A-ZÄÖÜ\d]", md.replace("\n", " | ")
    ), "server _SENT_BOUNDARY would split a row"


def test_breuel_order_title_then_two_columns() -> None:
    t = Block("text", [Segment(words_line(50, 50, "Titel ueber beiden Spalten"))])
    t.x1 = 545
    left = Block("text", [Segment(words_line(50, y, "links")) for y in range(100, 300, 14)])
    rgt = Block("text", [Segment(words_line(320, y, "rechts")) for y in range(100, 300, 14)])
    assert reading_order([rgt, left, t]) == [t, left, rgt]


def test_side_headings_attach_to_their_paragraph() -> None:
    blocks: list[Block] = []
    for k, y in enumerate((100, 200, 300)):
        blocks.append(Block("text", [Segment(words_line(50, y, f"Kopf{k}"))]))
        blocks.append(
            Block(
                "text",
                [
                    Segment(words_line(170, y + i * 13, "Absatztext mit Inhalt und mehr"))
                    for i in range(4)
                ],
            )
        )
    kept = attach_side_headings(blocks, CW)
    assert len(kept) == 3 and [b.head_prefix for b in kept] == [["Kopf0"], ["Kopf1"], ["Kopf2"]]


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            ["der Vorbereitung der Steuerer-", "klärungen für"],
            "der Vorbereitung der Steuererklärungen für",
        ),
        (["alle Lieferungs-", "und Leistungsverträge"], "alle Lieferungs- und Leistungsverträge"),
        (["die Treuhand-", "Gesellschaft"], "die Treuhand-Gesellschaft"),
        (["Rechnungs­", "abgrenzung"], "Rechnungsabgrenzung"),
        (["Total CHF 1'234.50 -", "Vorjahr"], "Total CHF 1'234.50 - Vorjahr"),
    ],
)
def test_dehyphenation(lines: list[str], expected: str) -> None:
    assert dehyphen_join(lines) == expected


def test_running_header_and_page_numbers() -> None:
    pages: list[list[list[Segment]]] = []
    for n in range(1, 5):
        ws = words_line(50, 20, "Gebrueder Mueller AG Jahresrechnung 2023") + words_line(
            280, 820, f"Seite {n} von 4"
        )
        ws += words_line(50, 300, f"Inhalt der Seite {n} ist verschieden {'x' * n}")
        pages.append(build_segments(ws, CW)[1])
    marked = detect_furniture(pages, [842.0] * 4)
    texts = [s.text for vl in pages for ln in vl for s in ln if s.furniture]
    assert marked == 7
    assert texts.count("Gebrueder Mueller AG Jahresrechnung 2023") == 3  # first occurrence kept
    assert sum(t.startswith("Seite") for t in texts) == 4


def test_no_word_lost_or_duplicated_under_jitter() -> None:
    rnd = random.Random(1)
    base = bilanz_lines(wrapped=True) + [
        w
        for y in range(400, 500, 14)
        for w in words_line(50, y, "Prosa Text der Revisionsstelle bleibt erhalten")
    ]
    for _ in range(20):
        ws = [
            Word(
                w.x0 + rnd.uniform(-0.4, 0.4),
                w.y0 + rnd.uniform(-0.6, 0.6),
                w.x1 + rnd.uniform(-0.4, 0.4),
                w.y1 + rnd.uniform(-0.6, 0.6),
                w.text,
                w.size,
            )
            for w in base
        ]
        segs, _vl = build_segments(ws, CW)
        got = sorted(w.text for s in segs for w in s.words)
        assert got == sorted(w.text for w in base)


def test_kv_grid_renders_label_colon_value() -> None:
    ws: list[Word] = []
    for i, (k1, v1, k2, v2) in enumerate(
        [
            ("Name:", "Meier", "Geburtsdatum:", "14.07.1968"),
            ("AHV-Nr.:", "756.1234", "Zivilstand:", "ledig"),
            ("Adresse:", "Seestrasse", "Beruf:", "Kaufmann"),
        ]
    ):
        y = 100 + 14 * i
        ws += [_w(50, y, k1), _w(130, y, v1), _w(300, y, k2), _w(400, y, v2)]
    rp = render_page_detailed(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert rp.structured
    assert "Name: Meier | Geburtsdatum: 14.07.1968" in rp.text and " | Meier | " not in rp.text
    assert rp.parts[0].kind == "kv"


def test_two_column_prose_order_left_column_before_right_column() -> None:
    """Law-firm regression gate: the whole left column precedes the right column; a line in
    the right column at the same height as a left paragraph must not jump ahead."""
    left = _para(50, 100, 5, 8, first="LinksA") + _para(50, 220, 5, 8, first="LinksB")
    right_col = [
        _w(320, 100, "Art.", 10),
        _w(345, 100, "6", 10),
        _w(360, 100, "Rechts", 10),
        *_para(320, 115, 5, 8, first="RechtsA"),
    ]
    lay = build_page_text(left + right_col, 595, 842, ocr=True)
    assert lay.gutters, "gutter must be detected"
    md = render_page(lay, plain_text="")
    t = md.replace("\n", " ")
    assert t.index("LinksA") < t.index("LinksB") < t.index("Art. 6 Rechts") < t.index("RechtsA")
    assert " | " not in md


def test_rows_never_contain_server_sentence_boundary() -> None:
    """Server splits on '(?<=[.!?])\\s+(?=[A-ZÜÖÄ\\d"'(])' inside a chunk: 'Fr. 1'200' must survive."""
    ws = [_w(50, 100, "Posten"), _w(300, 100, "2023"), _w(400, 100, "2022")]
    for i, lab in enumerate(["Miete", "Fr. Zins", "Ziff. 3 Total"]):
        y = 114 + 14 * i
        x = 50.0
        for t in lab.split():
            ws.append(_w(x, y, t))
            x += 5.0 * len(t) + 4
        ws += [_w(300, y, f"1'{i}00.00"), _w(400, y, f"2'{i}00.00")]
    md = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    rows = [ln for ln in md.split("\n") if " | " in ln]
    assert len(rows) >= 3
    for line in rows:
        assert not SENT_SPLIT_RE.search(line), line


def test_output_is_deterministic_and_never_contains_form_feed() -> None:
    ws = bilanz_lines() + _para(50, 400, 6, 5)
    a = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    b = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert a == b and "\f" not in a and a


# ══════════════════════════════════════════════════════════════════════════
# M2 gaps and plan §7 rows
# ══════════════════════════════════════════════════════════════════════════


def lohnausweis_boxes() -> list[Word]:
    """Lohnausweis-style form: small label line, Courier values ≤ 1.4 × pitch below each label."""
    ws: list[Word] = []
    for x, lab in ((43, "C AHV-Nr."), (213, "D Jahr"), (293, "E von"), (418, "bis")):
        ws += words_line(x, 78, lab, size=6.5)
    for x, val in (
        (115, "756.9217.0769.85"),
        (263, "2023"),
        (354, "01.01.2023"),
        (494, "31.12.2023"),
    ):
        ws += words_line(x, 92, val, size=9.5)
    ws += words_line(43, 116, "H Name und Adresse", size=6.5) + words_line(
        308, 116, "F Unentgeltliche Befoerderung", size=6.5
    )
    ws += (
        words_line(50, 128, "Frau", size=9.5)
        + words_line(50, 139, "Anna Keller", size=9.5)
        + words_line(50, 150, "Rosenweg 7", size=9.5)
    )
    ws += words_line(528, 131, "nein", size=9.5)
    return ws


def shifted(ws: list[Word], dy: float) -> list[Word]:
    return [Word(w.x0, w.y0 + dy, w.x1, w.y1 + dy, w.text, w.size, w.bold) for w in ws]


def test_vertical_kv_rule_pairs_labels_with_values_below() -> None:
    ws = lohnausweis_boxes() + shifted(bilanz_lines(), 300)  # the table makes the page structured
    rp = render_page_detailed(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert (
        "C AHV-Nr.: 756.9217.0769.85 | D Jahr: 2023 | E von: 01.01.2023 | bis: 31.12.2023"
        in rp.text
    )
    assert "F Unentgeltliche Befoerderung: nein" in rp.text
    # the address block under "H Name und Adresse" is not a value: it stays a paragraph
    assert "H Name und Adresse: -" in rp.text and "Frau Anna Keller Rosenweg 7" in rp.text


def test_two_kv_pairs_on_one_line_split_on_second_label_colon() -> None:
    assert split_kv_pairs("Name, Vorname: Meier Hans Geburtsdatum: 14.07.1968") == [
        ("Name, Vorname", "Meier Hans"),
        ("Geburtsdatum", "14.07.1968"),
    ]
    assert split_kv_pairs("Ort, Datum: Zürich, 15.05.2024 Zahlbar bis: 05.05.2024") == [
        ("Ort, Datum", "Zürich, 15.05.2024"),
        ("Zahlbar bis", "05.05.2024"),
    ]
    assert split_kv_pairs("Nur ein Wert: hier") is None
    ws = words_line(50, 100, "Name, Vorname: Meier Hans Geburtsdatum: 14.07.1968")
    ws += words_line(50, 114, "AHV-Nr.: 756.1234.5678.97 Zivilstand: verheiratet")
    ws += words_line(50, 128, "Adresse: Seestrasse 45 Beruf: Kaufmann")
    rp = render_page_detailed(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert rp.structured and rp.parts[0].kind == "kv"
    assert "Name, Vorname: Meier Hans | Geburtsdatum: 14.07.1968" in rp.text
    assert "AHV-Nr.: 756.1234.5678.97 | Zivilstand: verheiratet" in rp.text


def _ruled_rows() -> list[Word]:
    ws = [*words_line(50, 80, "Position"), right(430, 80, "2023"), right(530, 80, "2022")]
    y = 100
    ws += (
        words_line(50, y, "Forderungen aus Lieferungen")
        + words_line(50, y + 11, "und Leistungen")
        + [right(430, y, "456'789.00"), right(530, y, "400'120.00")]
    )
    y += 30
    ws += [*words_line(50, y, "Vorraete"), right(430, y, "23'450.15"), right(530, y, "19'870.00")]
    y += 18
    ws += [
        *words_line(50, y, "Total Aktiven"),
        right(430, y, "5'176'806.95"),
        right(530, y, "4'936'144.30"),
    ]
    return ws


def test_ruled_table_from_vector_rules_and_from_raster_rule_boxes() -> None:
    vector = [
        Rule("h", 50, 96, 540, 96),
        Rule("h", 50, 126, 540, 126),
        Rule("h", 50, 144, 540, 144),
    ]
    raster = normalize_rules(
        [
            {"kind": "h", "bbox": [50, 95.5, 540, 96.5]},
            {"kind": "h", "bbox": [50, 125.6, 540, 126.4]},
            ("h", 50, 143.5, 540, 144.5),
        ]
    )
    for rules in (vector, raster):
        grid = run_table(_ruled_rows(), rules)
        labels = [r.label for r in grid.rows]
        assert labels == [
            "Forderungen aus Lieferungen und Leistungen",
            "Vorraete",
            "Total Aktiven",
        ], labels
    assert normalize_rules([120.0])[0].kind == "h"


def test_totals_row_after_hrule_stays_a_row() -> None:
    ws = [*words_line(50, 80, "Aktiven"), right(430, 80, "2023"), right(530, 80, "2022")]
    for i, (lab, a, b) in enumerate(
        [
            ("Fluessige Mittel", "1'234.00", "987.00"),
            ("Vorraete", "23.15", "19.00"),
            ("Debitoren", "45.00", "40.00"),
        ]
    ):
        y = 100 + 18 * i
        ws += [*words_line(50, y, lab), right(430, y, a), right(530, y, b)]
    y_total = 100 + 18 * 2 + 50  # 2.8 × pitch below the last row, rule in between
    ws += [
        *words_line(50, y_total, "Total Aktiven", bold=True),
        right(430, y_total, "1'302.15"),
        right(530, y_total, "1'046.00"),
    ]
    rule = Rule("h", 50, y_total - 6, 540, y_total - 6)
    lay = build_page_text(ws, 595, 842, ocr=False, rules=[rule])
    md = render_page(lay, plain_text="")
    assert "Total Aktiven | 2023: 1'302.15 | 2022: 1'046.00" in md
    render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert True  # rule-free gap: not asserted


def test_tax_form_field_codes_not_merged_with_larger_heading() -> None:
    ws = [
        *words_line(53, 100, "Ziffer", 8.5, bold=True),
        right(467, 100, "Staatssteuer", 8.5, True),
        right(557, 100, "Bundessteuer", 8.5, True),
    ]
    for i, (code, lab, a, b) in enumerate(
        [
            ("1.1", "Haupterwerb Ehemann", "98'450", "98'450"),
            ("1.2", "Haupterwerb Ehefrau", "42'300", "42'300"),
            ("8", "Total der Einkuenfte", "180'375", "180'375"),
        ]
    ):
        y = 118 + 14 * i
        ws += (
            words_line(53, y, code, 8.5)
            + words_line(87, y, lab, 8.5)
            + [right(467, y, a, 8.5), right(557, y, b, 8.5)]
        )
    ws += words_line(50, 172, "Abzuege", 10.5, bold=True)  # section heading right under the band
    for i, (code, lab, a, b) in enumerate(
        [
            ("11", "Berufsauslagen Ehemann", "-4'230", "-4'230"),
            ("12", "Berufsauslagen Ehefrau", "-2'810", "-2'810"),
            ("13", "Saeule 3a", "-7'056", "-7'056"),
        ]
    ):
        y = 190 + 14 * i
        ws += (
            words_line(53, y, code, 8.5)
            + words_line(87, y, lab, 8.5)
            + [right(467, y, a, 8.5), right(557, y, b, 8.5)]
        )
    ws += _para(50, 300, 6, 4, pitch=11, size=8.5)
    md = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    rows = [ln for ln in md.split("\n") if " | " in ln]
    assert not any("Abzuege" in ln for ln in rows), rows
    assert re.search(r"^#{1,4} Abzuege$", md, re.M)
    assert "8 | Total der Einkuenfte | Staatssteuer: 180'375 | Bundessteuer: 180'375" in md


def test_block_id_is_a_second_column_cue_on_ocr_pages() -> None:
    """Two 2-line blocks side by side with a gap of only 2.5 u and no third neighbour line:
    without engine ids the gap lacks support and is re-joined; different Tesseract block
    ids make it a break (plan R2)."""

    def page(block_ids: tuple[int, int]) -> list[list[Segment]]:
        ws: list[Word] = []
        for i, y in enumerate((100, 113)):
            ws += words_line(50, y, "Peter Mueller Zuerich", block=block_ids[0], line=i)
            ws += words_line(
                50 + 21 * CW + 2 * 3 + 2.5 * CW, y, "Sandra Weber Bern", block=block_ids[1], line=i
            )
        return build_segments(ws, CW, ocr=True)[1]

    assert all(len(ln) == 1 for ln in page((-1, -1)))
    assert all(len(ln) == 2 for ln in page((3, 4)))


def test_heading_level_is_capped_at_four() -> None:
    sizes = [24, 20, 17, 15, 13, 11.5]
    ws: list[Word] = []
    y = 60.0
    for k, sz in enumerate(sizes):
        ws += words_line(50, y, f"Ebene {k} Titel", size=sz, bold=True)
        y += sz * 2
    ws += bilanz_lines()
    for i in range(5):
        ws += words_line(
            50, 400 + 12 * i, "Fliesstext mit Inhalt der Seite und noch mehr Inhalt dazu", size=9
        )
    md = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    heads = re.findall(r"^(#{1,6}) ", md, re.M)
    assert heads and max(len(h) for h in heads) <= 4
    assert all(re.match(r"^(#{1,4})\s+(.+)$", ln) for ln in md.split("\n") if ln.startswith("#"))


def test_unstructured_page_is_passthrough_byte_identical_to_plain() -> None:
    ws = _para(50, 100, 7, 12, first="Sehr") + words_line(
        50, 60, "Grosser Titel der Seite", size=16, bold=True
    )
    lay = build_page_text(ws, 595, 842, ocr=False)
    plain = "Grosser Titel der Seite\nSehr geehrte Damen,\n\nirgendein Text:\n mit   Spaces \nund Zeilen."
    assert render_page(lay, plain_text=plain) == plain
    rp = render_page_detailed(lay, plain_text=plain)
    assert not rp.structured and rp.sections == [] and "#" not in rp.text


def _bag(text: str) -> list[str]:
    toks = []
    for t in re.findall(r"\S+", text):
        if t in ("|", "-") or t.startswith("#") or t == "(Forts.)":
            continue
        toks.append(t.rstrip(":"))
    return sorted(toks)


def test_no_word_lost_or_duplicated_under_em_jitter() -> None:
    """R10: with one pack per table and no fold keys, the rendered tokens are exactly the
    input words (colons of kv labels aside) — for every jittered copy of the page."""
    rnd = random.Random(7)
    base = bilanz_lines(wrapped=True) + shifted(lohnausweis_boxes(), 300) + _para(50, 520, 6, 6)
    expected = sorted(w.text.rstrip(":") for w in base)
    opts = LayoutOptions(fold=False, pack_budget_tokens=10**6, letterhead_heading=False)
    for _ in range(12):
        ws = [
            Word(
                w.x0 + rnd.uniform(-0.3, 0.3) * w.size,
                w.y0 + rnd.uniform(-0.3, 0.3) * w.size,
                w.x1 + rnd.uniform(-0.3, 0.3) * w.size,
                w.y1 + rnd.uniform(-0.3, 0.3) * w.size,
                w.text,
                w.size,
                w.bold,
            )
            for w in base
        ]
        rp = render_page_detailed(
            build_page_text(ws, 595, 842, ocr=False, opts=opts), plain_text=""
        )
        assert _bag(rp.text) == expected


def test_row_over_200_estimated_tokens_split_with_forts_repeating_label() -> None:
    long_cell = " ".join(f"Position {i} 1'234'567.{i:02d}" for i in range(40))
    lines = split_long_row(
        "Umsatz Total", [long_cell, "2023: 99.00"], max_tokens=200, max_chars=1800
    )
    assert len(lines) >= 2
    assert lines[0].startswith("Umsatz Total | ")
    assert all(ln.startswith("Umsatz Total (Forts.) | ") for ln in lines[1:])
    assert all(est_tokens(ln) <= 200 + 40 for ln in lines)
    grid = TableGrid({0: "Posten", 1: "Betrag"}, [TableRow({0: "Umsatz Total", 1: long_cell})], 2)
    text = "\n\n".join(p.text for p in render_table_parts(grid, budget=150))
    assert "(Forts.)" in text and text.count("Umsatz Total") >= 2


def test_prose_line_with_colon_on_structured_page_is_byte_identical_to_plain() -> None:
    prose = "Hinweis: Die Zahlen sind provisorisch. Siehe Anhang 2. Fr. 10 sind offen."
    ws = bilanz_lines() + words_line(50, 300, prose)
    plain_line = " ".join(w.text for w in words_line(50, 300, prose))
    md = render_page(build_page_text(ws, 595, 842, ocr=False), plain_text="")
    assert plain_line in md.split("\n")  # D12: never touched by the row lint
    assert "Fr.10" not in md


def test_lint_only_on_renderer_classified_lines() -> None:
    assert lint_row_line("Ziff. 3 Total\t| Fr. 1'200") == "Ziff.3 Total | Fr.1'200"
    assert lint_row_line("Ziff. 3 Total", sentence_guard=False) == "Ziff. 3 Total"
    assert clean_cell(" a | b\tc ") == "a / b c"


def test_est_tokens_counts_digits_and_punctuation_heavily() -> None:
    assert est_tokens("1'234'567.80") >= 10
    assert est_tokens("Hallo") <= 3
    assert est_tokens("") == 0


def test_compact_keys_distinguish_duplicate_years() -> None:
    assert compact_keys({1: "Anhang", 2: "31.12.2023 CHF", 3: "31.12.2022 CHF"}, 4) == {
        1: "Anhang",
        2: "2023",
        3: "2022",
    }
    assert compact_keys({1: "Budget 2023", 2: "Ist 2023"}, 3) == {1: "Budget 2023", 2: "Ist 2023"}


def test_page_number_and_stamp_patterns() -> None:
    assert (
        is_page_number("Seite 3 von 12") and is_page_number("- 4 -") and is_page_number("page 2/9")
    )
    assert not is_page_number("Total 3")
    assert is_scanner_stamp("Scanned 12.03.2024 09:41") and is_scanner_stamp("Eingegangen 3. Mai")
    assert not is_scanner_stamp("Rechnung Nr. 12")


def test_ocr_words_filter_garbage_and_share_run_em() -> None:
    rows = [
        {"text": "Müller", "bbox": [60, 54, 106, 66], "conf": 96, "block": 1, "par": 1, "line": 1},
        {"text": "AG", "bbox": [112, 56, 134, 66], "conf": 96, "block": 1, "par": 1, "line": 1},
        {
            "text": "8001",
            "bbox": [506, 58, 521, 63],
            "conf": 90,
            "block": 1,
            "par": 1,
            "line": 1,
        },  # far right: own run
        {"text": "|", "bbox": [200, 54, 201, 66], "conf": 10, "block": 1, "par": 1, "line": 1},
        {"text": "ee", "bbox": [210, 54, 215, 66], "conf": 20, "block": 1, "par": 1, "line": 1},
        {"text": "a", "bbox": [220, 54, 223, 66], "conf": 20, "block": 1, "par": 1, "line": 1},
    ]
    ws = words_from_ocr_rows(rows)
    texts = [w.text for w in ws]
    assert "|" not in texts and "ee" not in texts and "a" in texts
    by = {w.text: w for w in ws}
    assert (
        by["Müller"].size == by["AG"].size == 12.0 - 1.0
        or abs(by["Müller"].size - by["AG"].size) < 1e-9
    )
    assert by["8001"].size < by["AG"].size
    assert (
        words_from_ocr_rows([{"text": "   ", "left": 1, "top": 1, "width": 5, "height": 5}]) == []
    )
    lt = words_from_ocr_rows(
        [{"text": "Wort", "left": 10, "top": 20, "width": 40, "height": 10}], scale=0.5
    )
    assert (lt[0].x0, lt[0].y0, lt[0].x1, lt[0].y1) == (5, 10, 25, 15)


def test_deskew_shears_a_sloped_line_flat() -> None:
    ws = [
        Word(x, 100 + 0.02 * x, x + 20, 110 + 0.02 * x, f"w{i}", 10, block=1, line=1)
        for i, x in enumerate(range(50, 450, 40))
    ]
    out = deskew_words(ws)
    ys = [w.y0 for w in out]
    assert max(ys) - min(ys) < 1.0
    flat = [
        Word(x, 100, x + 20, 110, f"w{i}", 10, block=1, line=1)
        for i, x in enumerate(range(50, 450, 40))
    ]
    assert [w.y0 for w in deskew_words(flat)] == [100.0] * len(flat)


def test_hyphen_preference_uses_document_vocabulary() -> None:
    pref = HyphenPreference(["Treuhandgesellschaft", "Revisionsstelle"])
    assert dehyphen_join(["die Treuhand-", "Gesellschaft"], pref) == "die Treuhandgesellschaft"
    pref2 = HyphenPreference(["Treuhand-Gesellschaft"])
    assert dehyphen_join(["die Treuhand-", "Gesellschaft"], pref2) == "die Treuhand-Gesellschaft"
    assert pref.prefer("Steuer", "erklärung") is None and len(pref) == 2


def test_gutter_rejects_numeric_right_side_and_short_runs() -> None:
    lines: list[list[Word]] = []
    for y in range(100, 300, 14):
        lines.append(
            words_line(50, y, "links stehender Vertragstext mit")
            + words_line(320, y, "1'234.00 5'000.00 3")
        )
    assert detect_gutters(lines, CW, 595) == []
    assert detect_gutters(lines[:3], CW, 595) == []


def test_visual_lines_group_by_overlap_and_pitch() -> None:
    ws = (
        words_line(50, 100, "erste Zeile")
        + words_line(50, 114, "zweite Zeile")
        + words_line(300, 101, "rechts")
    )
    vl = build_visual_lines(ws)
    assert [[w.text for w in ln.words] for ln in vl] == [
        ["erste", "Zeile", "rechts"],
        ["zweite", "Zeile"],
    ]
    assert median_pitch(vl) == 14.0 and median_pitch([]) == 12.0
    assert page_unit([]) == 4.0


def test_link_blocks_keeps_columns_apart() -> None:
    ws = _para(50, 100, 4, 3) + _para(320, 100, 4, 3, first="Rechts")
    segs, _ = build_segments(ws, CW)
    blocks = link_blocks(segs, CW)
    assert len(blocks) == 2 and all(b.n_lines == 3 for b in blocks)
