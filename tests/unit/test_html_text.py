"""HTML-only e-mail bodies → text (EML/MSG): entities, structure, hostile input."""

from __future__ import annotations

import pytest

from knovas_extract._html_text import html_to_text

pytestmark = [pytest.mark.unit]


def test_entities_are_decoded() -> None:
    s = "Gr&uuml;ezi Herr M&uuml;ller, anbei die Offerte &amp; der Vertrag."
    assert html_to_text(s) == "Grüezi Herr Müller, anbei die Offerte & der Vertrag."


def test_paragraphs_and_breaks_keep_their_lines() -> None:
    assert html_to_text("<p>Eins</p><p>Zwei</p>") == "Eins\n\nZwei"
    assert html_to_text("a<br>b<br/>c<BR />d") == "a\nb\nc\nd"
    assert html_to_text("<div>Kopf</div><div>Fuss</div>") == "Kopf\n\nFuss"
    # Mail clients put attributes on <br>: Apple Mail class="", Gmail and Yahoo clear=.
    assert html_to_text('a<br class="">b<br clear=all>c<br\nstyle="x">d') == "a\nb\nc\nd"


def test_no_tag_runs_two_words_together() -> None:
    # Text before a block start, an <hr> or an omitted end tag keeps a space...
    assert html_to_text('<div dir="ltr">Hallo<div>Welt</div></div>') == "Hallo Welt"
    assert html_to_text("Signatur<hr>Weitergeleitet") == "Signatur Weitergeleitet"
    assert html_to_text("<ul><li>Eins<li>Zwei</ul>") == "Eins Zwei"
    assert html_to_text("<dl><dt>Name</dt><dd>Meier</dd></dl>") == "Name Meier"
    # ...while inline formatting adds none, inside a word or before punctuation.
    s = 'Gr<b>ü</b>ezi, CO<sub>2</sub> und <a href="x">Link</a>.'
    assert html_to_text(s) == "Grüezi, CO2 und Link."
    # One pass over the tags: a stray "<" before inline tags swallows no text.
    assert html_to_text("a < b <b>fett</b> c > d") == "a < b fett c > d"


def test_table_rows_become_lines_with_cells_joined() -> None:
    s = (
        "<table><tr><td>Betrag</td><td>CHF 12.50</td></tr>"
        "<tr><th>MWST</th><td>8.1%</td></tr></table>"
    )
    assert html_to_text(s) == "Betrag | CHF 12.50\nMWST | 8.1%"


def test_script_style_head_and_comments_are_dropped_with_their_content() -> None:
    s = (
        "<head><title>T</title><style>p{color:red}</style></head>"
        "<!-- intern --><script>alert(1)</script><p>Text</p>"
    )
    assert html_to_text(s) == "Text"


def test_text_after_a_dropped_block_survives_a_dotted_capital_i() -> None:
    # "İ".lower() is two code points, so offsets found in s.lower() overshoot in s.
    dotted = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}" * 20
    s = "<p>" + dotted + " Teklif</p><style>p{}</style><p>Wichtiger Text</p>"
    assert html_to_text(s) == dotted + " Teklif\n\nWichtiger Text"
    # Tag names compare ASCII-only, as in HTML: "ſ" (U+017F) matches "s" under
    # re.IGNORECASE, yet <ſtyle> is no style element.
    assert html_to_text("<\N{LATIN SMALL LETTER LONG S}tyle>x</style>Rest") == "x Rest"


def test_control_and_bidi_characters_from_references_are_dropped() -> None:
    assert html_to_text("a&#x202E;b&#1;c&#x2066;d") == "abcd"


def test_c1_range_is_not_dropped() -> None:
    # A body labelled ISO-8859-1 but written in cp1252 decodes "…" to U+0085 (NEL),
    # whitespace that must keep the words apart; references in the C1 range decode
    # to cp1252 characters, so &#x9b; is "›", not the 8-bit CSI.
    assert html_to_text("prüfen\x85danke") == "prüfen danke"
    cp1252 = "a\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}b\N{EN DASH}c"
    assert html_to_text("a&#x9b;b&#150;c") == cp1252


def test_whitespace_is_collapsed_per_line_and_nbsp_is_a_space() -> None:
    assert html_to_text("<p>  viel   &nbsp;&nbsp; Raum  </p>") == "viel Raum"


def test_blank_lines_collapse_to_one() -> None:
    assert html_to_text("<p>a</p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_large_body_stays_linear() -> None:
    body = "<p>" + "Zeile mit Text &amp; Zahl 1'234.50<br>" * 20000 + "</p>"
    out = html_to_text(body)
    assert out.count("\n") == 19999
    assert "&amp;" not in out


def test_non_latin_body_is_filtered_without_a_str_per_character() -> None:
    import tracemalloc

    # A per-character filter holds a ~60-byte str object for every non-Latin-1
    # character at its peak; regex passes only copy the text a few times.
    body = "<p>" + ("".join(map(chr, range(0x430, 0x450))) + " ") * 10000 + "</p>"
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        html_to_text(body)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        if not tracing:
            tracemalloc.stop()
    assert peak < 30 * len(body)


@pytest.mark.parametrize(
    "hostile",
    [
        "<!--" * 50000 + "x",  # unterminated comments
        "<script>" * 50000 + "x",  # unclosed script blocks
        "<" * 200000 + "Text",  # angle brackets without a closing ">"
        "|" * 1000000,  # a run of cell separators
        "a" + "\u2003" * 200000 + "|",  # long runs of Unicode spaces before a separator
        "<br " * 300000,  # <br> attributes that never close
        "<b>" * 300000,  # one tag after the other
    ],
    # Short ids: the default id is the 200 kB input itself, which pytest puts into
    # PYTEST_CURRENT_TEST, and Windows refuses environment values over 32767 chars.
    ids=[
        "unterminated-comments",
        "unclosed-scripts",
        "unclosed-brackets",
        "separator-run",
        "unicode-space-run",
        "unclosed-br-attributes",
        "tag-run",
    ],
)
def test_hostile_markup_is_linear(hostile: str) -> None:
    import time

    t0 = time.perf_counter()
    html_to_text(hostile)
    assert time.perf_counter() - t0 < 2.0


def test_overlong_decimal_references_do_not_raise() -> None:
    # html.unescape() converts the digits with int(), which refuses more than 4300
    # of them; leading zeros carry no value and a longer number is out of range.
    assert html_to_text("a&#" + "9" * 5000 + ";b") == "a\N{REPLACEMENT CHARACTER}b"
    assert html_to_text("&#" + "0" * 5000 + "65;") == "A"
    # Only ASCII digits make a reference, as in html.unescape(); other digits stay text.
    nines = "\N{ARABIC-INDIC DIGIT NINE}" * 8
    assert html_to_text("&#" + nines + ";") == "&#" + nines + ";"
