# OCR benchmark harness (Swiss fiduciary documents)

An exploratory, reproducible benchmark that produced the numbers in
[`docs/ocr-and-tables-findings.md`](../../docs/ocr-and-tables-findings.md). It is **not** part of
the `knovas-extract` package, is not collected by pytest (`testpaths = ["tests"]`), is not linted or
type-checked, and never runs in CI. It needs a system `tesseract` (5.x) and takes hours for the
full configuration grid.

## What it does

1. `gen_corpus.py` + `layout.py` — render 13 synthetic Swiss pages with reportlab while recording
   ground truth (every text line / table-cell line as an *item* with box, role, block id, reading
   order, table row/column, key-value and heading metadata): Jahresrechnung (borderless Bilanz,
   ruled Erfolgsrechnung), Bankauszug, Lohnausweis, bilan (FR), conto economico (IT),
   Steuererklärung with dot leaders, MWST-Abrechnung, two-column Aktionärbindungsvertrag, GV protocol,
   QR invoice, letter with stamp. Degrades each page into image-only PDFs: `clean300`, `office300`
   (gray JPEG q75, 0.4° skew, blur), `gray200`, `fax150` (bitonal), `color300`, `skew15`, `stamp`,
   `rot90`, plus a `mixed/` set (digital cover + scanned body + scanner text stamp) for per-page OCR
   decision tests. Writes `corpus/manifest.json` with the affine page→scan transform per page.
2. `engines.py` — named OCR configurations, each returning words with boxes in a common frame:
   MuPDF's `get_textpage_ocr` (status quo and 300 dpi/full), Tesseract CLI (gray PGM via stdin, TSV
   word boxes; psm 3/4/6/11; `tessdata_fast` vs `tessdata_best`; 300/400/auto dpi; Sauvola;
   deskew; auto-rotate; rule-line/dot-leader erase; language-pack variants), optional `tesserocr`
   API and RapidOCR variants, and a `+pp` post-processing filter for leader garbage.
3. `run_ocr.py` — accuracy pass over the corpus with a page-level process pool, every worker
   single-threaded (`OMP_THREAD_LIMIT=1`); resumable; writes `ocr_out/<config>/<deg>/<doc>_p<k>.json`.
4. `evaluate.py` — maps every hypothesis word box back to the original page and assigns it to the
   ground-truth item whose box contains its centre; computes CER/WER (NFC, whitespace-collapsed,
   case and punctuation kept), `numeric_exact` (share of ground-truth number tokens reproduced
   exactly), per-role CER (tables vs text/forms), native-order CER (reading order) and hallucinated
   words per page.
5. `report.py` / `make_tables.py` — aggregate `ocr_out/` into `results/page_scores.csv`,
   `results/summary.json`, `results/tables.md` (overall, per degradation, per document).
6. `timing.py` / `run_timing.sh` — single-core seconds per page (pinned with `taskset`, median of 3)
   and throughput on 4 cores: sequential with Tesseract's internal threads vs a 4-process
   single-threaded pool vs 2×2.
7. `selftest.py` — harness self-tests (ground truth consistency, transform correctness, evaluator
   behaviour under controlled perturbations).

## Running it

```bash
python -m venv .venv && . .venv/bin/activate
pip install pymupdf reportlab numpy rapidfuzz            # + tesserocr / rapidocr for the optional engines
apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-fra   # Debian "fast" models
# tessdata_best (Apache-2.0), used by the *best* configs:
mkdir -p tessdata_best && for l in deu eng fra; do
  curl -L -o tessdata_best/$l.traineddata https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/$l.traineddata; done
# tessdata_fast: copy the Debian files, e.g. cp /usr/share/tesseract-ocr/5/tessdata/{deu,eng,fra}.traineddata tessdata_fast/

cd bench/ocr
python code/gen_corpus.py                  # corpus/ + gt/
python code/selftest.py                    # harness sanity
OMP_THREAD_LIMIT=1 python code/run_ocr.py --jobs 4 --configs tfast_psm3,tbest_pro3_dfe
python code/report.py                      # results/
bash code/run_timing.sh                    # on an idle machine
```

`engines.py` expects `tessdata_fast/` and `tessdata_best/` next to `code/` (see `TESSDATA` at the
top of the file). Generated corpora, `ocr_out/` and `tessdata_*` are large and must not be
committed; only `code/`, `results/tables.md` and the small `results/timing_*.json` are versioned.

## Headline results (2026-10-01, Tesseract 5.3.4, 91 scanned pages, rot90 excluded)

| Config | CER | numeric exact | notes |
|---|---|---|---|
| `mupdf_statusquo` — `get_textpage_ocr(language)` as knovas-extract 0.3.0 calls it (72 dpi, partial) | 0.575 | 0.151 | garbage |
| `mupdf_300_full` — `get_textpage_ocr(dpi=300, full=True)` | 0.212 | 0.781 | drops scanned ruled tables |
| `rapidocr_bundled` | 0.093 | 0.821 | WER 0.60 on German |
| `tfast_psm4` | 0.070 | 0.917 | |
| `tfast_psm3` | 0.054 | 0.924 | |
| `tfast_pro3_dfe` — psm 3, auto-dpi, auto-rotate, rule erase, deu+fra+eng | 0.020 | 0.919 | |
| `tbest_pro3_dfe` — same with `tessdata_best` | **0.019** | **0.943** | ~1.6× slower |

Throughput (26 pages, 4 cores): `tfast_psm3` 88 pages/min with `pool4_omp1` vs 10 pages/min
`seq_omp4`; `tbest_pro3_dfe` 41 vs 15. Full tables: [`results/tables.md`](results/tables.md).

Caveat: synthetic renders are cleaner than real scans (no bleed-through, no handwriting, uniform
fonts). Use the harness to compare configurations and catch regressions, not to predict absolute
accuracy at a customer.
