"""`OcrOptions` — the per-call OCR configuration passed as `extract(..., ocr=...)`.

Everything here is plain data (no numpy, no Tesseract) so importing it is
free; the heavy modules under `knovas_extract._ocr` are loaded lazily by
the PDF extractor only when a page actually needs OCR.

The `backend` and `cache` slots take *injected* implementations of the
`IOcrBackend` / `IOcrCache` protocols (see `knovas_extract.interfaces`).
The obligation tests inject a fake backend so the per-page decision and the
scheduler are exercised on every CI leg without Tesseract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from knovas_extract.interfaces import IOcrBackend, IOcrCache

OcrEngineT = Literal["auto", "tesserocr", "cli", "mupdf"]
OcrPoolT = Literal["thread", "process"]

DEFAULT_OCR_LANGUAGE = "deu+eng"
DEFAULT_OCR_PSM = 3
MAX_RENDER_DPI = 300


@dataclass(frozen=True, slots=True)
class OcrOptions:
    """OCR configuration for `extract(..., ocr=OcrOptions(...))` (PDF only).

    Args:
        engine: ``"auto"`` picks the first available backend in the order
            tesserocr → Tesseract CLI → MuPDF's built-in OCR. A named engine
            is *forced*: when it is unavailable, `extract` raises
            `DependencyMissingError` instead of falling back.
        language: Tesseract language pack string (``"deu+eng"``). Every
            pack must be installed for the chosen backend.
        dpi: Render resolution for the Tesseract backends. ``None`` (the
            default) renders at the page's native raster resolution capped
            at 300 dpi (never upsampled — a 150 dpi fax is OCR'd at 150 dpi).
        psm: Tesseract page-segmentation mode (3 = fully automatic).
        workers: Parallel OCR workers. ``None`` derives the count from the
            available CPUs (affinity + cgroup quota) capped by
            `Limits.max_ocr_workers`. The pool is sized lazily to
            ``min(workers, pages needing OCR)``.
        pool: ``"thread"`` (default; one thread-local engine per worker) or
            ``"process"`` (a forkserver pool — the only way a hung native
            recognition can be killed).
        tessdata_dir: Explicit Tesseract language-data folder. ``None`` →
            ``$TESSDATA_PREFIX`` or an auto-detected system folder.
        cache: An `IOcrCache` implementation. ``None`` → a per-document
            in-memory dict (the library never writes files).
        backend: An injected `IOcrBackend`. When set, `engine` is ignored
            and the backend's own ``name`` is reported.
        colour_dropout: Render RGB and keep the brightest channel per pixel
            before OCR (drops coloured stamps). Off until measured.
        retry_psm4: Re-run a page with psm 4 when psm 3 looks like it missed
            rows (low confidence over inked pages, or fewer text lines than
            the detected ruling lines imply) and keep the better result.
    """

    engine: OcrEngineT = "auto"
    language: str = DEFAULT_OCR_LANGUAGE
    dpi: int | None = None
    psm: int = DEFAULT_OCR_PSM
    workers: int | None = None
    pool: OcrPoolT = "thread"
    tessdata_dir: str | None = None
    cache: IOcrCache | None = None
    backend: IOcrBackend | None = None
    colour_dropout: bool = False
    retry_psm4: bool = True

    def __post_init__(self) -> None:
        if self.engine not in ("auto", "tesserocr", "cli", "mupdf"):
            raise ValueError("OcrOptions.engine must be one of auto|tesserocr|cli|mupdf")
        if self.pool not in ("thread", "process"):
            raise ValueError("OcrOptions.pool must be 'thread' or 'process'")
        if self.dpi is not None and not (30 <= self.dpi <= 1200):
            raise ValueError("OcrOptions.dpi must be between 30 and 1200")
        if not (0 <= self.psm <= 13):
            raise ValueError("OcrOptions.psm must be a Tesseract page-segmentation mode (0-13)")
        if self.workers is not None and self.workers < 1:
            raise ValueError("OcrOptions.workers must be >= 1")
        if not self.language or any(ch in self.language for ch in " /\\\0"):
            raise ValueError(
                "OcrOptions.language must be a Tesseract language string like 'deu+eng'"
            )
