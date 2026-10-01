# OCR and table extraction — findings (2026-10-01)

Findings from an investigation of a Swiss fiduciary (Treuhand) customer whose scanned and tabular
documents are indexed badly by Knovas Semantix. Everything below was reproduced in a sandbox against
this repository at `cl/eloquent-hawking-gyn8fo` (HEAD `77c1a10`, version 0.3.0) with PyMuPDF 1.28.0,
pymupdf4llm 1.28.0 and Tesseract 5.3.4. The cross-repository picture is in
`KnowledgeBase/docs/superpowers/audits/2026-10-01-fiduciary-search-audit.md`; the RemoteController
side in `KnovasComponents/docs/superpowers/specs/2026-10-01-fiduciary-search-diagnosis.md`. The OCR
benchmark harness and its result tables are in [`bench/ocr/`](../bench/ocr/README.md).

## Findings

### F1 — OCR renders pages at 72 dpi
`src/knovas_extract/extractors/pdf.py:393` calls `page.get_textpage_ocr(language=language)` with no
`dpi` and no `full`. PyMuPDF's `get_textpage_ocr` defaults to `dpi=72, full=False` ("partial" OCR:
a temporary copy of the page with the digital spans redacted, rendered at 72 dpi, then
`pdfocr_tobytes`). At 72 dpi an 8-9 pt table row is ~6 px high; Tesseract's LSTM wants ~30 px
capital height. Measured on a 300 dpi office scan of a Bilanz: "Forderungen aus Lieferungen und
Leistungen" → "Forderungenas Liaungen und Leitungen", "456'789.00" → "45070900". On the 91-page
benchmark the status quo scores **CER 0.575, 15 % of amounts exact**; `get_textpage_ocr(dpi=300,
full=True)` on the same pages scores CER 0.212 / 78 %, at the same ~1 s per page.
Every scanned PDF ingested with 0.3.0 carries this text in its chunks, its BM25 field and its
sidecar sentences.

### F2 — The OCR decision is made once per document
`pdf.py:501-514`: `_ocr_should_run(use_ocr, has_text=bool(full_text.strip()))` looks at the whole
joined text. In `auto` mode (what the RemoteController uses) a digital cover letter, a one-line
scanner/DMS stamp, or a sandwich page with a broken ToUnicode map disables OCR for **every** page.
Mixed digital+scanned PDFs are the fiduciary's normal case (cover letter + scanned Jahresrechnung,
tax return printout + scanned attachments).

### F3 — `emit_markdown=True` on a mixed PDF raises and the caller parks the file forever
`pdf.py:562-563` builds Markdown via pymupdf4llm whenever OCR did **not** run (so exactly the F2
case). pymupdf4llm ≥ 1.27 activates the `pymupdf-layout` model and runs its own OCR on image pages
(`use_ocr=True`, `ocr_language="eng"` defaults), so the Markdown is 4-13× longer than
`content.text`; `_markdown.check_expansion` (`_markdown.py:301-315`, ratio 3.0) raises
`ResourceExhaustedError("markdown expansion ratio")`. The RemoteController classifies that as
unconvertible and never retries the file. The same guard fires on table-heavy DOCX
(`docx.py:453-460`), HTML-heavy EML/MSG (`eml.py:242-248`, `msg.py:209-217`). The Markdown is also
expensive: a 20-page digital PDF takes 8.27 s with `emit_markdown=True` vs 0.45 s without, because
the layout model is loaded and run per document. The RemoteController never reads
`content.markdown`.

### F4 — `content.text` tears tables apart
`_collect_page_texts` uses `page.get_text("text")`, which emits one line per MuPDF block/line — in
practice one line per table cell. A Bilanz row becomes

```
Flüssige Mittel
1'234'567.80
987'654.30
```

so the label and its amounts land in different sentences, different chunks and different sidecar
records. For the embedder and for BM25 the row no longer exists. This affects **born-digital** PDFs
too, not only scans. The Markdown path (pymupdf4llm) rendered the same tables perfectly — but see
F3 and F6.

### F5 — `find_tables` output is unusable for financial statements
`pdf.py:263` calls `page.find_tables()` with the default `"lines"` strategy. On the sample: the
borderless Bilanz (the typical Treuhand layout) was not found at all; the ruled Erfolgsrechnung
came back with the section heading taken as the header row, cells like `"612'40000\n."` and
`"UnterhaltundReparaturen"`. The tables are sent to the server by the RemoteController and dropped
there before storage, so today they cost CPU on every page and change nothing.

### F6 — MuPDF's built-in OCR drops scanned ruled tables
`get_textpage_ocr` (both partial and `full=True, dpi=300`) returned only the heading and the prose
paragraph for a scanned page whose ruled Erfolgsrechnung occupies the middle — the table vanished.
The Tesseract CLI on the same 300 dpi render (`--psm 3` or `4`, `preserve_interword_spaces=1`)
returned every row intact. pymupdf4llm (layout mode) dropped the same table on the scan as well.

### F7 — Per-page failures and sentence limits are fatal for the whole document
`_page_text_via_ocr` (`pdf.py:390-402`) turns any non-Tesseract exception into
`CorruptDocumentError`, so one bad page fails the binder; `split_sentences` raises
`ResourceExhaustedError("sentence count")` above `Limits.max_sentences` (`_sentences.py:169-172`)
instead of returning no sentences. Both are classified as permanent by the RemoteController.

### F8 — Table-only DOCX files extract to empty text
Since spec 1.1.0 `_extract_body_text` (`docx.py:130-152`) leaves tables out of `content.text`; a
document that consists only of tables (forms, layout-table letterheads, Word-based Bilanz drafts)
yields `text == ""` and the caller treats it as "no extractable text".

### F9 — Packaging and licensing
`pyproject.toml:51` pins `pymupdf4llm >= 0.0.17, < 1.28.1`, which resolves to 1.28.0; since 1.27.2
pymupdf4llm hard-requires `pymupdf_layout`, licensed **PolyForm Noncommercial 1.0.0 or Artifex
commercial** (`pip show pymupdf-layout`), plus `onnxruntime`, `numpy`, `networkx`. The customer
images (RemoteController, KnovasPlatform) install `knovas-extract[pdf,…]` from `git+…@main`, so they
ship that model today. `NOTICE:18-22` still describes pymupdf4llm as Artistic-2.0 and does not
mention `pymupdf-layout`. `tests/unit/test_extractors_pdf.py` has a `needs_tesseract` marker, but
`.github/workflows/ci.yml` does not install Tesseract, so the OCR path is untested in CI.

## OCR benchmark

Harness: [`bench/ocr/`](../bench/ocr/README.md) (ground-truth corpus generator, engine configs,
evaluator, timing). 13 synthetic Swiss pages (Jahresrechnung with borderless and ruled tables,
Bankauszug, Lohnausweis, bilan FR, conto economico IT, Steuererklärung with dot leaders,
MWST-Abrechnung, two-column Aktionärbindungsvertrag, GV protocol, QR invoice, letter) × 7
degradations (clean 300 dpi, office 300 gray JPEG q75 + 0.4° skew + blur, gray 200, bitonal fax 150,
colour 300, 1.5° skew, rubber stamp) + one rotated page; ~40 configurations; metrics after NFC and
whitespace normalisation with case and punctuation kept; `numeric_exact` is the share of
ground-truth amount/number tokens reproduced exactly. Synthetic scans are cleaner than real ones:
the ranking is robust, the absolute numbers are optimistic.

| Config | CER | WER | numeric exact | hallucinated words/page |
|---|---|---|---|---|
| **status quo** `get_textpage_ocr(language)` (72 dpi, partial) | 0.575 | 0.834 | 0.151 | 5.6 |
| `get_textpage_ocr(dpi=300, full=True)` | 0.212 | 0.351 | 0.781 | 23.2 |
| RapidOCR bundled ONNX models | 0.093 | 0.596 | 0.821 | 0.1 |
| Tesseract CLI, fast, psm 4, 300 dpi | 0.070 | 0.165 | 0.917 | 10.9 |
| Tesseract CLI, fast, psm 3, 300 dpi | 0.054 | 0.128 | 0.924 | 7.4 |
| fast, psm 3, auto-dpi, auto-rotate, rule-line erase, deu+fra+eng | 0.020 | 0.076 | 0.919 | 1.0 |
| **best**, psm 3, auto-dpi, auto-rotate, rule-line erase, deu+fra+eng | **0.019** | 0.068 | **0.943** | 1.3 |

CER per degradation for the best config: clean 0.006 · office 300 0.008 · gray 200 0.007 ·
colour 0.008 · skew 1.5° 0.032 · stamp 0.045 · fax 150 0.031 (amounts 80 %) · rot90 0.004 — without
auto-rotate psm 3 fails on rotated pages (CER 0.79). Hardest documents: Steuererklärung (dot
leaders) and Lohnausweis (boxed form); prose and two-column contract ≤ 0.001.

What moved the needle, in order: **resolution** (72 → 300 dpi), **page segmentation** (psm 3 over
psm 4: half the CER, a third of the hallucinated words), **auto-rotation**, **erasing rule lines
and dot leaders** before OCR (keep the line geometry as table metadata), **language packs** (each
extra pack costs throughput; `deu+fra+eng` is the Swiss minimum), then `tessdata_best` over
`tessdata_fast` (+2.5 points on amounts, ~1.6× slower). Binarisation (Sauvola/Otsu), 400 dpi
upsampling and deskew did not pay for themselves on this corpus.

Throughput (26 pages, 4 cores): **88 pages/min** with four single-threaded processes
(`OMP_THREAD_LIMIT=1`) vs **10 pages/min** sequential with Tesseract's default OpenMP threads; best
model 41 pages/min. Single core on office scans: 2.2 s/page fast, ~3.5 s best. Micro-costs: render
300 dpi gray 0.02 s; PNG encoding **1.3 s** (use PGM via stdin: 0.001 s); process spawn + model load
0.16-0.31 s; per-page OCR decision (text length + image coverage) < 1 ms.

## What should change (summary; the implementation plan lives with the RemoteController)

1. `ocr_dpi` kwarg (default 300) and `full=True`; a per-**page** OCR decision (`< 50` non-space
   characters with image coverage ≥ 0.3, or ≤ 300 characters with coverage ≥ 0.8 for the stamp
   case, or ≥ 20 % U+FFFD/private-use glyphs); per-page fail-soft; `Limits.max_ocr_pages` and an OCR
   time budget with partial results.
2. A Tesseract **CLI** backend (gray PGM via stdin, `--psm 3`, TSV word boxes with confidences,
   `preserve_interword_spaces=1`, `OMP_THREAD_LIMIT=1`, no shell, timeout), falling back to MuPDF's
   OCR when the binary is missing; rule-line erase and auto-rotate as preprocessing.
3. A dependency-light **markdown-lite** renderer built from word boxes (headings, one line per
   table row with column headers folded in, two-column reading order, key-value fields, `\f` between
   pages) as the text the RemoteController uploads — row integrity without `pymupdf4llm`.
4. Split the `[pdf]` extra: `pymupdf` only; `pymupdf4llm` in an opt-in `[pdf-markdown]` extra with
   the licence note; make `check_expansion` a warning (`markdown=None`) instead of an error; fix
   `NOTICE`; install Tesseract in CI and run the `needs_tesseract` tests.
5. DOCX tables rendered inline as rows (`docx_tables="inline"`), so table-only documents are not
   empty.
