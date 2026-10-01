"""Document-level OCR orchestration for the PDF extractor.

`run_document_ocr(doc, candidates, ...)` is what `extractors/pdf.py` calls
once it has decided per page (`_ocr.decision`) which pages need OCR:

1. pick the backend (`OcrOptions.backend` injected, else `select_backend`),
2. drop candidates whose embedded image or render exceeds
   `Limits.max_ocr_image_megapixels` — decided from `get_image_info()`
   BEFORE any render (a pixel bomb is never decoded),
3. run the bounded fail-soft scheduler (`_ocr.pool.run_ocr_schedule`):
   `prepare(i)` renders on the CALLING thread (PyMuPDF documents are not
   thread-safe) and probes the cache by image-stream fingerprint; the
   worker computes the samples fingerprint, preprocesses (autorotate,
   deskew, rule detection), recognises, retries with psm 4 when warranted
   and returns ``(cache_key, OcrPageResult)``; `collect` stores the result
   and feeds the cache,
4. return per-page results plus counts the extractor turns into
   `metadata.extra["pdf:ocr_*"]` scalars and counted warnings.

Pool choice: thread pool by default (thread-local engines); a forkserver
process pool for `OcrOptions(pool="process")` with the built-in engines;
inline (no pool) for the MuPDF backend, which must stay on the document's
thread.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from knovas_extract._ocr.backend import select_backend
from knovas_extract._ocr.cache import (
    DictOcrCache,
    compose_key,
    samples_fingerprint,
    single_full_page_image,
    stream_fingerprint,
)
from knovas_extract._ocr.options import OcrOptions
from knovas_extract._ocr.pool import OcrRunResult, run_ocr_schedule
from knovas_extract._ocr.tsv import OcrPageResult

if TYPE_CHECKING:
    from knovas_extract._ocr.preprocess import PageImage
    from knovas_extract.interfaces import IOcrBackend, IOcrCache
    from knovas_extract.result import Limits

PREPROC_FINGERPRINT = "autorot=1;deskew=1;rules=1;v=1"
RETRY_MIN_CONF = 70.0
RETRY_MIN_INK = 0.02


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Enough to rebuild a built-in backend inside a worker process."""

    engine: str
    language: str
    psm: int
    tessdata_dir: str | None
    page_timeout: float


@dataclass(slots=True)
class DocumentOcrResult:
    results: dict[int, OcrPageResult]
    run: OcrRunResult
    backend_name: str
    backend_version: str | None
    oversize: list[int] = field(default_factory=list)
    cache_hits: int = 0
    mean_conf: float | None = None


def available_cpus() -> int:
    """CPUs this process may use: affinity mask, then the cgroup v2 quota."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    with contextlib.suppress(OSError, ValueError):
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max":
            n = max(1, min(n, int(float(quota) / float(period))))
    return max(1, n)


def default_workers(limits: Limits) -> int:
    return max(1, min(limits.max_ocr_workers, available_cpus() - 1))


def _numpy_stack_available() -> bool:
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _should_retry_psm4(res: OcrPageResult, pi: PageImage) -> bool:
    rules = pi.rules
    ink = rules.ink_fraction if rules is not None else 0.0
    if res.mean_conf is not None and res.mean_conf < RETRY_MIN_CONF and ink >= RETRY_MIN_INK:
        return True
    if rules is not None and rules.h_rules >= 3:
        from knovas_extract._ocr.preprocess import count_text_lines

        expected = rules.h_rules - 1
        if count_text_lines(res.words) < 0.5 * expected:
            return True
    return False


def _better(res3: OcrPageResult, res4: OcrPageResult) -> OcrPageResult:
    c3 = res3.mean_conf if res3.mean_conf is not None else 0.0
    c4 = res4.mean_conf if res4.mean_conf is not None else 0.0
    if len(res4.words) > len(res3.words) and c4 >= c3 - 5.0:
        return res4
    return res3


def _normalize(value: Any) -> OcrPageResult:
    if isinstance(value, OcrPageResult):
        return value
    if value is None:
        return OcrPageResult(text="")
    return OcrPageResult(text=str(value))


def ocr_one(
    pi: PageImage,
    backend: Any,
    *,
    cache: IOcrCache | None,
    key_suffix: dict[str, Any],
    retry_psm4: bool,
) -> tuple[str | None, OcrPageResult]:
    """Recognise one prepared page (worker side). Returns (cache key, result)."""
    key = pi.cache_key
    if pi.gray is not None:
        if key is None:
            fp = samples_fingerprint(pi.gray.tobytes(), pi.width, pi.height)
            key = compose_key(fp, rotation=pi.rotation, **key_suffix)
            pi.cache_key = key
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                with contextlib.suppress(Exception):
                    return key, OcrPageResult.from_json(hit)
        from knovas_extract._ocr.preprocess import preprocess

        preprocess(pi, osd=getattr(backend, "detect_orientation", None))
    res = _normalize(backend.recognize(pi))
    if retry_psm4 and pi.gray is not None and pi.psm == 3 and _should_retry_psm4(res, pi):
        try:
            res4 = _normalize(backend.recognize(replace(pi, psm=4)))
        except Exception:
            res4 = None
        if res4 is not None:
            res = _better(res, res4)
    return key, res


_PROCESS_BACKENDS: dict[BackendSpec, Any] = {}


def _process_task(
    spec: BackendSpec, pi: PageImage, retry_psm4: bool, key_suffix: dict[str, Any]
) -> tuple[str | None, OcrPageResult]:
    """Entry point inside a worker process: one backend per process."""
    backend = _PROCESS_BACKENDS.get(spec)
    if backend is None:
        backend = select_backend(
            spec.engine,
            language=spec.language,
            psm=spec.psm,
            tessdata_dir=spec.tessdata_dir,
            page_timeout=spec.page_timeout,
        )
        _PROCESS_BACKENDS[spec] = backend
    return ocr_one(pi, backend, cache=None, key_suffix=key_suffix, retry_psm4=retry_psm4)


def _process_executor_factory(n: int) -> Executor:
    method = "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
    return ProcessPoolExecutor(
        max_workers=max(1, n), mp_context=multiprocessing.get_context(method)
    )


def run_document_ocr(
    doc: Any,
    candidates: list[int],
    *,
    options: OcrOptions,
    limits: Limits,
    language: str,
    backend: IOcrBackend | None = None,
) -> DocumentOcrResult:
    """OCR the candidate pages of an open PyMuPDF document (see module doc).

    Raises `DependencyMissingError` only from backend selection (the caller
    decides whether that is fail-soft); nothing a page does can raise.
    """
    from knovas_extract._ocr.preprocess import (
        PageImage,
        choose_render_dpi,
        image_infos,
        image_megapixels,
        render_gray,
        render_megapixels,
        to_array,
    )

    if backend is None:
        backend = options.backend or select_backend(
            options.engine,
            language=language,
            psm=options.psm,
            tessdata_dir=options.tessdata_dir,
            page_timeout=limits.ocr_page_timeout_seconds,
        )
    backend_name = str(getattr(backend, "name", type(backend).__name__))
    backend_version = getattr(backend, "version", None)
    needs_image = bool(getattr(backend, "needs_image", True)) and _numpy_stack_available()
    injected = options.backend is not None
    cache: IOcrCache = options.cache if options.cache is not None else DictOcrCache()
    engine_tag = f"{backend_name}:{backend_version or '?'}"

    # Megapixel cap from the image dictionaries, before any render.
    meta: dict[int, tuple[list[dict[str, Any]], int]] = {}
    oversize: list[int] = []
    scheduled: list[int] = []
    for i in candidates:
        try:
            page = doc.load_page(i)
            infos = image_infos(page)
            dpi = 300 if not needs_image else choose_render_dpi(page, infos, options.dpi)
            mp = max(image_megapixels(infos), render_megapixels(page, dpi))
        except Exception:
            infos, dpi, mp = [], 300, 0.0
        if mp > limits.max_ocr_image_megapixels:
            oversize.append(i)
            continue
        meta[i] = (infos, dpi)
        scheduled.append(i)

    images: dict[int, PageImage] = {}
    hits: dict[int, OcrPageResult] = {}
    results: dict[int, OcrPageResult] = {}
    stats = {"cache_hits": 0}

    def key_suffix(dpi: int) -> dict[str, Any]:
        return {
            "dpi": dpi,
            "language": language,
            "psm": options.psm,
            "engine": engine_tag,
            "preproc": PREPROC_FINGERPRINT if needs_image else "mupdf",
        }

    def prepare(i: int) -> None:
        page = doc.load_page(i)
        infos, dpi = meta[i]
        rect = page.rect
        pi = PageImage(
            page_index=i,
            dpi=dpi,
            width=0,
            height=0,
            page_rect=(float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
            language=language,
            psm=options.psm,
            rotation=int(getattr(page, "rotation", 0) or 0),
            tessdata_dir=options.tessdata_dir,
        )
        single = single_full_page_image(page, infos)
        if single is not None:
            fp = stream_fingerprint(doc, int(single["xref"]))
            if fp is not None:
                pi.cache_key = compose_key(fp, rotation=pi.rotation, **key_suffix(dpi))
                cached = cache.get(pi.cache_key)
                if cached is not None:
                    try:
                        hits[i] = OcrPageResult.from_json(cached)
                        stats["cache_hits"] += 1
                        images[i] = pi
                        return
                    except Exception:
                        hits.pop(i, None)
        if needs_image:
            samples, w, h = render_gray(page, dpi, colour_dropout=options.colour_dropout)
            pi.gray = to_array(samples, w, h)
            pi.width, pi.height = w, h
            del samples
        else:
            pi.page = page
        images[i] = pi

    def ocr_page(i: int) -> Any:
        if i in hits:
            return None, hits.pop(i)
        pi = images[i]
        return ocr_one(
            pi,
            backend,
            cache=cache,
            key_suffix=key_suffix(pi.dpi),
            retry_psm4=options.retry_psm4,
        )

    def collect(i: int, value: Any) -> str:
        images.pop(i, None)
        key, raw = value
        res: OcrPageResult = _normalize(raw)
        results[i] = res
        if key is not None and i not in hits:
            with contextlib.suppress(Exception):
                cache.put(key, res.to_json())
        return res.text

    workers = options.workers if options.workers is not None else default_workers(limits)
    workers = max(1, min(workers, limits.max_ocr_workers))
    inline = not needs_image or not getattr(backend, "needs_image", True)
    executor_factory: Callable[[int], Executor] | None = None
    submit: Callable[[Executor, int], Future[Any]] | None = None
    if options.pool == "process" and not injected and needs_image and not inline:
        spec = BackendSpec(
            engine=backend_name,
            language=language,
            psm=options.psm,
            tessdata_dir=getattr(backend, "tessdata_dir", options.tessdata_dir),
            page_timeout=limits.ocr_page_timeout_seconds,
        )
        executor_factory = _process_executor_factory

        def submit(executor: Executor, i: int) -> Future[Any]:
            if i in hits:
                fut: Future[Any] = Future()
                fut.set_result((None, hits.pop(i)))
                return fut
            pi = images[i]
            return executor.submit(_process_task, spec, pi, options.retry_psm4, key_suffix(pi.dpi))

    run = run_ocr_schedule(
        scheduled,
        ocr_page,
        max_ocr_pages=limits.max_ocr_pages,
        time_budget_seconds=limits.ocr_time_budget_seconds,
        page_timeout_seconds=limits.ocr_page_timeout_seconds,
        workers=workers,
        prepare=prepare,
        executor_factory=executor_factory,
        submit=submit,
        collect=collect,
        inline=inline,
    )
    images.clear()
    hits.clear()
    confs = [r.mean_conf for r in results.values() if r.mean_conf is not None]
    mean_conf = round(sum(confs) / len(confs), 2) if confs else None
    return DocumentOcrResult(
        results=results,
        run=run,
        backend_name=backend_name,
        backend_version=str(backend_version) if backend_version is not None else None,
        oversize=oversize,
        cache_hits=stats["cache_hits"],
        mean_conf=mean_conf,
    )
