# Layout text mode (`text_mode="layout"`, markdown-lite)

`knovas_extract._layout` renders a PDF page from its **word boxes** into
*markdown-lite* and returns it as the page text itself. It is opt-in
(`text_mode="layout"`; plain mode stays the default) and pure Python: no
third-party import at module import time, no numpy. PyMuPDF is touched only
inside the two adapters `words.words_from_fitz_page` and
`words.vector_rules_from_fitz_page`; Tesseract words come in through
`words.words_from_ocr_rows` (TSV level-5 rows or the saved JSON dumps).

Why: the Knovas server chunks text on blank lines, line breaks and sentence
boundaries and prepends the active `#` headings to every embedded piece. A
Bilanz row that is one physical line therefore becomes one chunk whose label and
amounts can never be separated, and a `# Jahresrechnung 2023` line above it
becomes retrieval context for every row below. Plain mode emits such a row as
four lines (label, note, amount, amount) — four chunks, none of which answers
"Flüssige Mittel 2023".

## The structured-page guarantee (GI-EXTRACT-03)

A page is **structured** iff

* `detect_tables` finds ≥ 1 table region (≥ 3 row-aligned lines sharing ≥ 2
  column anchors), or
* the page carries ≥ 3 key-value lines (horizontal grid, vertical form boxes or
  inline `Key: value` lines), or
* (OCR pages only) a prose column gutter is detected.

Every other page is emitted **byte-identical to plain mode**: `render_page`
returns the `plain_text` it was given, untouched. No heading, no list marker, no
row lint is ever applied to an unstructured page, so a law-firm corpus of
one- and two-column prose is unaffected by the fiduciary format. The two-column
digital contract in the golden fixtures is such a page (its plain text is
already in reading order); the same page as an OCR scan has a gutter, is
structured, and is re-ordered column by column.

## Public API

```python
from knovas_extract._layout import (
    Word, Rule, LayoutOptions, DocumentLayoutPass,
    build_page_text, is_structured, render_page, render_page_detailed,
    words_from_fitz_page, vector_rules_from_fitz_page, words_from_ocr_rows,
)

layouts = []
for i, page in enumerate(doc):                      # pass 1, per page
    words = words_from_fitz_page(page)              # or words_from_ocr_rows(tsv_rows)
    rules = vector_rules_from_fitz_page(page)       # or raster rule boxes from the OCR step
    layouts.append(build_page_text(words, page.rect.width, page.rect.height,
                                   ocr=False, rules=rules, page_index=i))

DocumentLayoutPass().fit(layouts)                   # document pass: furniture, heading
                                                    # ranks, hyphen spelling preference
for layout, plain in zip(layouts, plain_texts):
    text = render_page(layout, plain_text=plain)    # == plain when the page is unstructured
    sections = layout.rendered.sections             # SectionRecord(heading, level,
                                                    #   line_start, line_end) per '#' line,
                                                    #   0-based lines inside this page text
```

`render_page_detailed` returns the `RenderedPage` (text, `structured`, `parts`,
`sections`, `line_kinds`). `build_page_text(..., doc_stats=...)` accepts a
pre-computed `DocStats` for callers that already ran the document pass.
`LayoutOptions` holds the knobs (`pack_budget_tokens=150`, `row_max_tokens=200`,
`row_max_chars=1800`, `headings="structured_only" | "none"`, `fold`,
`sentence_guard`, `furniture`, `kv_blocks`, `section_rows`, `letterhead_heading`,
`min_table_lines=3`, `min_kv_lines=3`, `max_heading_level=4`).

The caller keeps the plain-mode page text (`page.get_text("text")` or the OCR
text), joins pages with `"\n\n"` and canonicalises exactly as before, so
`Page.line_start/line_end`, sentence splitting and the consumer contracts apply
unchanged. The library never emits `\f`; page markers are the RemoteController's.

## Grammar (per page; blocks separated by exactly one blank line)

```
page        := block ("\n\n" block)*
block       := passthrough | heading | paragraph | list | kv_block | table
passthrough := the plain-mode page text verbatim — iff the page is UNSTRUCTURED (then it is the only block)
heading     := "#"{1,4} " " text        (1–120 chars, ≤ 12 words, no trailing . ; : , ; blank line after)
paragraph   := one physical line, words joined by single spaces, dehyphenated
list        := ("- " text "\n")+          (hanging-indent bullets/enumerators; 2 spaces per ≥ 1.5 em indent)
kv_block    := (kv_line "\n")+            kv_line := key ": " value (" | " key ": " value)*
table       := pack ("\n\n" pack)*        packs ≤ 150 ESTIMATED tokens
pack        := header_line "\n" (row "\n")+   header repeated at the top of EVERY pack
header_line := cell (" | " cell)+         stacked headers folded cell-wise ("31.12.2023 CHF")
row         := cell (" | " cell)+         cell := text | key ": " value — the fold only for NUMERIC/date
                                          cells with a short distinguishable key; empty cells omitted when
                                          every cell has a key, else "-"; "|" inside a cell → "/"
section_row := "#### " label              a label-only row heading ≥ 2 data rows
```

Rows over 200 estimated tokens or 1800 characters are split into continuation
lines that repeat the label plus `(Forts.)`.

## Server facts the grammar is designed against

(`KnowledgeBase/knovas-software/app/src/classes/information_object_manager.py`)

* Headings are recognised only by `^(#{1,4})\s+(.+)`; the heading stack at a
  chunk's position is BM25-preprocessed and prepended as `heading_context`.
  Hence the level cap of 4 and the blank line after every heading.
* Hierarchical chunking splits each `\f` segment on `\n{2,}`, then `\n`, then
  the sentence regex `(?<=[.!?])\s+(?=[A-ZÜÖÄ\d"'(])` (no abbreviation list).
  A row is one line, so it is never split — unless it contains that sentence
  boundary (`Fr. 1'200`, `Ziff. 3`). The row lint removes exactly that
  whitespace, only on renderer-classified header/row/kv lines (decision D12).
* Chunks > 1800 chars are hard-split; embed pieces are capped at 248 tokens
  before `hc: ` is prepended (D14), so packs stay ≤ 150 estimated tokens and
  rows ≤ 200.
* BM25 strips digits; the embedding tokenizer spends ~1 token per digit, which
  is why cells fold with *compact* keys (`2023: 1'234.00`, not
  `31.12.2023 CHF: 1'234.00`).
* Redis keeps only snippet/page/sentence, so tables must live in the text.

## Rules

* **R1 headings** — digital: size ≥ 1.15 × body em (level by size rank, max 3),
  bold in a mostly non-bold document, ALL CAPS, or a numbering prefix with bold
  or standing alone (one level below the size ranks). OCR: line height ≥ 1.3 ×
  the body height **plus** one more cue (numbering prefix, standalone before a
  paragraph, isolated block, caps). Page-1 letterhead on a structured page
  becomes the first `# ` line. Headings are emitted on structured pages only
  (D4), every level is capped at 4, and each emitted heading yields a
  `SectionRecord`.
* **R2 reading order** — visual lines → segments (a gap > 2 u is a cell break
  only with a leader, ≥ 6 u width, neighbour-line support, a vertical ruling,
  a gutter it covers, or — on OCR pages — different Tesseract blocks on either
  side) → prose gutters (reading-order cues only, never a cell boundary or a
  table) → blocks by union-find within a column → Breuel partial order with the
  column rule. Tables are atomic.
* **R3 tables** — candidate lines, anchors (x1 numeric / x0 text), regions of
  ≥ 3 lines sharing ≥ 2 anchors, columns by the modal segment count plus sparse
  extras, header = leading amount-free rows (also the column-aligned lines just
  above the run), rulings for multi-line cells, and the **heading-size guard**:
  once a run has two lines, a text line ≥ 1.2 × (OCR: 1.3 ×) the band's label em
  never joins it — tax-form field codes stay out of the `Abzüge` heading and
  section titles never become rows.
* **R4 key-value forms** — horizontal grids (`Name: | Meier`), vertical boxes
  (label line, values ≤ 1.4 × pitch below inside each label's box, one value per
  box, ≤ 6 words, no continuation line under the value — a Lohnausweis), inline
  `Key: value` lines; two pairs on one physical line are split on the second
  label-colon (`Name, Vorname: Meier Hans | Geburtsdatum: 14.07.1968`).
* **R5 lists** — bullets become `- `; enumerators are kept after `- `; nesting
  by 2 spaces per 1.5 em of indent.
* **R6 hyphenation** — `Steuerer-` + `klärungen` → `Steuererklärungen`;
  `Lieferungs-` + `und` keeps hyphen and space (`HYPH_KEEP_NEXT`); a
  capitalised continuation keeps the hyphen unless the document's own
  vocabulary shows the joined spelling.
* **R7 furniture** — top/bottom 9 % band lines repeating (digits masked) on
  ≥ max(2, 0.4 n) pages (first occurrence kept: the first-page letterhead is
  never dropped), page-number patterns, scanner stamps.
* **R8 row lint** (only renderer-classified lines) — no server sentence boundary
  inside a line, ≤ 200 est. tokens / 1800 chars else `(Forts.)`, no empty cell,
  no ` | ` inside a cell, digits never altered, years/dates stay in rows, no tabs.
* **R10 invariants** — no 3+ consecutive newlines, no whitespace-only line,
  every heading matches the server regex and is followed by a blank line, never
  `\f`, deterministic (identical input → identical bytes, independent of worker
  count), bag of words == input words minus furniture/leaders, and
  `len(layout) ≤ 1.5 × len(plain) + 4096`. `lint.check_invariants` checks them.

## Examples

### Bilanz (digital or OCR words)

```
# Müller AG

Bahnhofstrasse 12, 8001 Zürich

## Jahresrechnung 2023

### Bilanz per 31. Dezember 2023

Aktiven | Anhang | 31.12.2023 CHF | 31.12.2022 CHF
Flüssige Mittel | 2023: 1'234'567.80 | 2022: 987'654.30
Forderungen aus Lieferungen und Leistungen | Anhang: 2.1 | 2023: 456'789.00 | 2022: 400'120.00
Übrige kurzfristige Forderungen | 2023: 23'450.15 | 2022: 19'870.00

Aktiven | Anhang | 31.12.2023 CHF | 31.12.2022 CHF
Vorräte und nicht fakturierte Dienstleistungen | 2023: 78'300.00 | 2022: 81'250.00
```

### Tax form (field codes, two stacked tables, section heading between them)

```
# Steuererklärung 2023

Name, Vorname: Meier Hans | Geburtsdatum: 14.07.1968
AHV-Nr.: 756.1234.5678.97 | Zivilstand: verheiratet

## Einkünfte im In- und Ausland

Ziffer | Staatssteuer CHF | Bundessteuer CHF
1.1 | Unselbständige Haupterwerbstätigkeit Ehemann | Staatssteuer: 98'450 | Bundessteuer: 98'450
8 | Total der Einkünfte | Staatssteuer: 180'375 | Bundessteuer: 180'375

## Abzüge

Ziffer | Staatssteuer CHF | Bundessteuer CHF
11 | Berufsauslagen Ehemann | Staatssteuer: -4'230 | Bundessteuer: -4'230
```

### Bank statement (six columns, two rows per pack at 150 tokens)

```
Konto: Kontokorrent CHF
IBAN: CH56 0483 5012 3456 7800 9

Datum | Text | Valuta | Belastung | Gutschrift | Saldo
01.09.2023 | Saldovortrag | Valuta: 01.09.2023 | Saldo: 148'230.45
02.09.2023 | Gutschrift Kunde Brunner Architektur AG RE 2023-0815 | Valuta: 02.09.2023 | Gutschrift: 11'581.08 | Saldo: 159'811.53

Datum | Text | Valuta | Belastung | Gutschrift | Saldo
04.09.2023 | Zahlung Lieferant Holzbau Steiner GmbH | Valuta: 04.09.2023 | Belastung: 18'239.07 | Saldo: 141'572.46
```

### Two-column contract

Digital: unstructured → the plain-mode text, byte for byte. OCR scan: the gutter
makes the page structured; the whole left column is emitted before the right
column, each paragraph as one physical line:

```
# Aktionärbindungsvertrag

zwischen

Peter Müller, geboren am 3. April 1961, von Zürich, wohnhaft in Küsnacht (nachfolgend «Partei 1»)

Präambel

Die Parteien sind gemeinsam Eigentümer sämtlicher 500 Namenaktien der Müller AG mit einem Nennwert von je CHF 1'000.00. …

Art. 1 Zweck

Dieser Vertrag bezweckt die einheitliche Ausübung der Aktionärsrechte, …
```

(`Präambel` / `Art. 1 Zweck` are not headings here: on OCR pages the
conservative policy needs the 1.3 × height cue, which a 10.5 pt heading over
9.5 pt body text does not give.)

## Token cost (real `knovas_embedding_v1` tokenizer, 90 ground-truth rows)

| serialization | tokens / row | relative | note |
|---|---|---|---|
| plain cell-per-line (today) | 35.6 | 1.00 | four chunks per row, label and amounts apart |
| bare pipe row | 37.4 | 1.05 | |
| GFM table | 42.6 | 1.20 | rejected: separator rows, no fold keys |
| **fold-compact** (this mode) | **48.5** | **1.36** | `2023: 1'234.00` — year/column key per numeric cell |
| fold-full | 56.9 | 1.60 | rejected: `31.12.2023 CHF: 1'234.00` |

Page level: fold-compact ≈ 875 tokens/page vs bare pipe 803 (+9 %). Packs are
sized with `lint.est_tokens` — a least-squares fit against the real tokenizer
(MAPE ≈ 7 %; digits and punctuation ≈ 1 token each) because the client ships no
tokenizer.

## Measured on the Treuhand fixtures (`tests/golden/test_layout_golden.py`)

Thirteen pages of ten synthetic Swiss fiduciary documents, rendered from saved
word boxes (no Tesseract at test time), ≈ 5 ms/page:

| source | row integrity | row (cond.) | cells | header | kv | headings R / P (structured pages) | numeric F1 | contract tau / intact |
|---|---|---|---|---|---|---|---|---|
| digital | 0.993 | 0.993 | 0.998 | 1.000 | 1.000 | 1.00 / 0.94 | 1.000 | 1.00 / 1.00 |
| clean300 | 0.901 | 0.913 | 0.964 | 0.977 | 0.812 | — | 0.999 | 1.00 / 0.98 |
| office300 | 0.849 | 0.902 | 0.908 | 0.977 | 0.719 | — | 0.982 | 1.00 / 1.00 |
| gray200 | 0.829 | 0.881 | 0.929 | 0.977 | 0.594 | — | 0.989 | 1.00 / 0.99 |
| skew15 | 0.822 | 0.887 | 0.908 | 0.977 | 0.688 | — | 0.974 | 1.00 / 0.99 |
| stamp | 0.763 | 0.879 | 0.865 | 0.864 | 0.656 | — | 0.959 | 0.87 / 0.89 |
| fax150 | 0.513 | 0.736 | 0.754 | 0.795 | 0.438 | — | 0.889 | 1.00 / 0.66 |

OCR heading recall is low by design in v1 (conservative policy, D4); the
gates for OCR headings and the stamp/fax tau are M3 targets. The thresholds
live in `tests/fixtures/treuhand/MANIFEST.yaml`; the measured numbers in
`baseline.yaml`.
