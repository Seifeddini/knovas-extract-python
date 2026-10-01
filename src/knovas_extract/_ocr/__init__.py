"""Per-page OCR for scanned PDFs (GI-EXTRACT-01/02/04).

Import-light on purpose: this package only pulls in plain-Python modules.
numpy / Pillow / Tesseract are loaded lazily by `_ocr.pipeline` when a
page actually needs OCR. See ``docs/ocr.md``.
"""

from __future__ import annotations

from knovas_extract._ocr.decision import OcrDecision, decide_page, page_needs_ocr
from knovas_extract._ocr.options import OcrOptions
from knovas_extract._ocr.pool import OcrRunResult, run_ocr_schedule
from knovas_extract._ocr.tsv import OcrPageResult, OcrWord, parse_tsv

__all__ = [
    "OcrDecision",
    "OcrOptions",
    "OcrPageResult",
    "OcrRunResult",
    "OcrWord",
    "decide_page",
    "page_needs_ocr",
    "parse_tsv",
    "run_ocr_schedule",
]
