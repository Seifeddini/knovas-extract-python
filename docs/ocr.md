# OCR for scanned PDFs

`knovas-extract` ≥ 0.4.0 decides **per page** whether to OCR and runs a bounded,
fail-soft OCR pipeline over the pages that need it. Born-digital PDFs produce exactly
the same output as before; scanned pages are now rendered at their native resolution
(capped at 300 dpi) instead of 72 dpi, which is a behaviour change for every scanned
document (see CHANGELOG). Everything is opt-in beyond `use_ocr="auto"` (the default),
via `extract(..., ocr=OcrOptions(...))` and the OCR fields of `Limits`.

```python
from knovas_extract import extract, OcrOptions, Limits

r = extract("scan.pdf")                                   # auto: per-page decision, best available engine
r = extract("scan.pdf", ocr=OcrOptions(engine="cli", workers=2, dpi=300))
r = extract("scan.pdf", limits=Limits(max_ocr_pages=50, ocr_time_budget_seconds=60))
```

Install: `pip install 'knovas-extract[pdf,ocr]'` (adds `tesserocr` on Linux, `numpy`,
`pillow`). Language data comes from the host: `apt install tesseract-ocr tesseract-ocr-deu
tesseract-ocr-eng`. The library never downloads anything.

## Backends (`OcrOptions.engine`)

| engine | what | needs | notes |
|---|---|---|---|
| `tesserocr` | in-process Tesseract via the `tesserocr` wheel (bundles Tesseract 5.5 + Leptonica) | `[ocr]` extra, tessdata on the host | fastest; one engine per worker thread; `OMP_THREAD_LIMIT=1` is set before it loads |
| `cli` | the system `tesseract` binary, one child per page | `tesseract` on `PATH`, `numpy`, `pillow` | gray PGM on stdin, TSV on stdout, minimal env, `stderr` discarded, hard timeout, no shell |
| `mupdf` | PyMuPDF's built-in OCR (`get_textpage_ocr(dpi=300, full=True)`) | tessdata on the host | no numpy needed; weakest on ruled tables; runs on the calling thread |
| `auto` (default) | first available of the three, in that order | | a named engine never falls back |

`metadata.extra["pdf:ocr_backend"]` reports the engine actually used (`"none"` when a
page needed OCR but no engine was available), `pdf:ocr_backend_version` its version.
The decision and scheduler are also usable with an **injected** backend
(`OcrOptions(backend=obj)` implementing `knovas_extract.interfaces.IOcrBackend`) — the
test-suite's fake backend proves the mechanism on every CI leg without Tesseract.

Tessdata resolution: `OcrOptions.tessdata_dir` → `$TESSDATA_PREFIX` → well-known system
folders (`/usr/share/tesseract-ocr/5/tessdata`, …) → the folder `tesseract --list-langs`
reports. Every pack in `OcrOptions.language` (default `deu+eng`) must exist.

## Per-page decision (`use_ocr`)

The decision is a property of the page (`knovas_extract._ocr.decision.decide_page`),
never of the document — a scanned body behind a digital cover letter is OCR'd:

Rules are evaluated in this order; "cover" is the fraction of the page area under
raster images (bboxes from `get_image_info()` clipped to the page, summed, capped at 1.0):

| # | text layer of the page | image cover | outcome |
|---|---|---|---|
| 1 | any | < 30 % | kept verbatim, never OCR'd (`reason="no_image"`) |
| 2 | empty | ≥ 30 % | OCR'd (`no_text`) |
| 3 | **garbage** — `(cid:N)` / U+FFFD / private-use > 5 % of tokens, or > 15 % garbage tokens among tokens of ≥ 4 chars (digits are never garbage) | ≥ 30 % | OCR'd, the garbage is **discarded** (`garbage_text`) |
| 4 | any non-garbage layer **< 200 chars** | **≥ 85 %** (a full-page raster) | OCR'd, the layer is kept and the OCR text **appended** (`full_page_scan`) — a scan is a scan whatever scanner stamp sits on it ("Gescannt am 12.03.2024 14:33 Seite 2 von 12" alone would pass rule 5); a real text page over a background image has far more than 200 chars |
| 5 | **usable** — not garbage, ≥ 2 alphabetic tokens, ≥ 20 chars | 30–85 % | kept verbatim, never OCR'd (`usable_text`; a title page with a 60 % logo stays byte-identical, `pdf:ocr_pages == 0`) |
| 6 | short (< 20 chars) or sparse (< 2 alphabetic tokens) non-garbage | ≥ 30 % | OCR'd, the layer is kept and the OCR text **appended** (`short_text` / `sparse_text`) |

Thresholds are module constants in `knovas_extract._ocr.decision`: `MIN_IMAGE_COVER = 0.30`,
`FULL_PAGE_COVER = 0.85`, `STAMP_MAX_CHARS = 200`, `MIN_USABLE_CHARS = 20`,
`MIN_ALPHA_TOKENS = 2`, `CID_FFFD_TOKEN_RATE = 0.05`, `GARBAGE_TOKEN_RATE = 0.15`.

`use_ocr=False` never OCRs; `use_ocr=True` OCRs every page carrying an image and the OCR
text replaces the layer. `ocr_language` / `OcrOptions.language` selects the packs.

## Render and preprocessing

Gray render at `min(300, native dpi of the page's raster)` rounded to 10 dpi (never
upsampled; `OcrOptions.dpi` overrides), raw samples straight from the pixmap (never PNG),
`fitz.TOOLS.store_shrink(100)` after each render. numpy + Pillow only:
sideways check by ink-run variance → OSD only for sideways pages → 90° rotation;
projection-profile deskew (±3°, applied from 0.15°); ruling lines and dot leaders found
by run-length opening, the image is left intact and words lying on rule ink are dropped
after OCR. psm 3, one psm-4 retry when psm 3 looks like it missed rows
(`OcrOptions.retry_psm4`). `OcrOptions.colour_dropout` (off) keeps the brightest RGB
channel to erase coloured stamps.

## Budgets and fail-soft semantics (`Limits`)

| field | default | behaviour |
|---|---|---|
| `max_ocr_pages` | 500 | the scheduler stops **submitting** once this many pages were attempted |
| `ocr_time_budget_seconds` | 240 | stops submitting once the elapsed OCR time passes it; pages already running finish |
| `ocr_page_timeout_seconds` | 60 | one page's wall clock; a timeout is a **failed** page (hard for `cli` and the process pool, advisory for in-process engines) |
| `max_ocr_workers` | 8 | worker cap; the pool is sized lazily to `min(workers, pages needing OCR)` |
| `max_ocr_image_megapixels` | 40 | checked from the PDF's image dictionaries and the page size **before any render** — an oversize page is skipped, never decoded |

Crossing a budget **never raises** (decision D3): the extraction succeeds with the text
pages searchable, the remaining candidates are counted in `pdf:ocr_pages_skipped`, a
raising page is counted in `pdf:ocr_pages_failed` with empty text. Pages are submitted in
document order and reassembled by index, so 1 and N workers produce identical output. A
missing engine in `use_ocr="auto"` leaves every candidate's text layer in place with one
counted warning and `pdf:ocr_backend="none"`; `DependencyMissingError` is raised only
when an engine is forced (`OcrOptions.engine`, `use_ocr=True`) or when the document would
otherwise be completely empty. `OcrOptions(pool="process")` runs the built-in engines in
a forkserver process pool — the only way a hung native recognition can be killed.

## Metadata and warnings

Reported when OCR was configured explicitly (`ocr=`), `use_ocr=True`, or at least one
page was a candidate — a born-digital PDF extracted with the defaults carries none of
these keys, so its output is unchanged:

`pdf:ocr_pages` (pages whose text came from OCR), `pdf:text_pages` (pages whose text
layer was used as is), `pdf:ocr_pages_skipped`, `pdf:ocr_pages_failed`,
`pdf:ocr_backend`, `pdf:ocr_backend_version`, `pdf:ocr_seconds`, `pdf:ocr_cpu_seconds`
(process + reaped children), `pdf:ocr_mean_conf` (mean word confidence, Tesseract
engines only). Scalars only; `spec_version` is unchanged.

Warnings are counts, never content: `pdf: OCR applied to N of M pages via <backend>`
(exactly one, when N > 0), `pdf: N pages skipped: OCR budget exhausted`,
`pdf: N pages skipped: image exceeds max_ocr_image_megapixels`, `pdf: N pages failed OCR`,
`pdf: OCR backend unavailable; N pages left without OCR`.

## Cache protocol (`OcrOptions.cache`)

`knovas_extract.interfaces.IOcrCache` is `get(key) -> str | None` / `put(key, value)`.
Keys are sha256 hex strings over the page content fingerprint — the **undecoded** image
stream (`xref_stream_raw` + `/Filter /Width /Height /BitsPerComponent /Decode`) when the
page is one full-page image, computed before any render so a hit skips the render, else
the rendered gray samples — plus the page's `/Rotate`, dpi, language, psm, engine
name/version and a preprocessing fingerprint. Values are `OcrPageResult.to_json()`
strings and **contain page text**: a persistent implementation owns that at-rest copy
(create files `0600`, purge per document, make it disableable). The default is a
per-document in-memory dict; the library never writes files.

## Environment variables

- `OMP_THREAD_LIMIT` — set to `1` (`setdefault`) before Tesseract loads and passed
  explicitly to the CLI child; an operator's explicit value is respected.
- `TESSDATA_PREFIX` — honoured for tessdata resolution; the only variable besides `PATH`
  (and `SYSTEMROOT` on Windows) forwarded to the CLI child.
- No other environment is read; nothing is written.

Design background: `docs/ocr-and-tables-findings.md` and the benchmark under `bench/ocr/`.
Alloy obligations: `KnowledgeBase/knovas-software/models/alloy/mechanisms/client_pipeline.als`
(`PerPageOcrDecisionMechanism`, `BoundedOcrMechanism`, `NoSpuriousSkipMechanism`,
`FailSoftMechanism`), pinned by `tests/unit/test_ocr_decision.py` and
`tests/unit/test_ocr_scheduler.py`.
