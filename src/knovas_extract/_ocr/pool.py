"""The bounded, fail-soft OCR scheduler (GI-EXTRACT-01/02, decisions D3 and D6).

Alloy: ``mechanisms/client_pipeline.als`` preds ``BoundedOcrMechanism``,
``NoSpuriousSkipMechanism`` and ``FailSoftMechanism`` (model
``data_plane/ocr_budget_failsoft.als``; mutants ``ocr__spurious_skip``,
``ocr__budget_raises``). Obligation tests: ``tests/unit/test_ocr_scheduler.py``.

`run_ocr_schedule` is a pure function over "OCR this page index" callables:

- pages are submitted in document order and reassembled by index, so the
  result is byte-identical for 1 and N workers;
- the scheduler stops SUBMITTING when the attempted count reaches
  `max_ocr_pages` or the clock has passed `time_budget_seconds` (both
  checked before every submit); pages submitted before the trip finish and
  count as attempted, the remaining candidates are `skipped`;
- a page that raises, or whose future does not complete within
  `page_timeout_seconds`, is a `failed` page with ``""`` text — never an
  exception for the document;
- warnings are COUNTS ("4 pages skipped: ocr budget", "2 pages failed"),
  never content.

The default pool is a thread pool (the backends keep one engine per thread
in thread-local state). A process pool (`OcrOptions(pool="process")`,
forkserver context) is wired in by `_ocr.pipeline` through the
`executor_factory` / `submit` / `collect` hooks: it is the only pool in
which a hung native recognition can actually be killed — a thread holding
a hung `Recognize` keeps its slot and is merely counted as failed.

`inline=True` runs the pages sequentially on the calling thread without a
pool (used for the MuPDF backend, because PyMuPDF documents are not
thread-safe); the page timeout is advisory there.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class OcrRunResult:
    texts: dict[int, str]
    attempted: list[int]
    skipped: list[int]
    failed: list[int]
    warnings: list[str]
    seconds: float
    cpu_seconds: float = 0.0
    cache_hits: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _cpu_now() -> float:
    """Process CPU time plus the CPU time of reaped children (CLI / process
    pool), in seconds."""
    total = time.process_time()
    try:
        import resource

        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        total += ru.ru_utime + ru.ru_stime
    except (ImportError, AttributeError, OSError):
        pass
    return total


def _default_executor_factory(n: int) -> Executor:
    return ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="knovas-ocr")


def run_ocr_schedule(
    candidates: Sequence[int],
    ocr_page: Callable[[int], str],
    *,
    max_ocr_pages: int,
    time_budget_seconds: float,
    page_timeout_seconds: float,
    workers: int,
    clock: Callable[[], float] = time.monotonic,
    prepare: Callable[[int], None] | None = None,
    executor_factory: Callable[[int], Executor] | None = None,
    submit: Callable[[Executor, int], Future[Any]] | None = None,
    collect: Callable[[int, Any], str] | None = None,
    inline: bool = False,
) -> OcrRunResult:
    """Run `ocr_page` over `candidates` under the budgets; never raises for a page.

    Args:
        candidates: page indices in document order.
        ocr_page: ``index -> text``. Runs on a worker (or inline).
        max_ocr_pages: hard cap on attempted pages.
        time_budget_seconds: stop submitting once ``clock() - start`` reaches it.
        page_timeout_seconds: per-page wall-clock timeout (pool mode).
        workers: pool size (sized lazily to ``min(workers, candidates)``).
        clock: monotonic clock for the time budget (tests inject a fake one).
        prepare: optional ``index -> None`` hook run on the CALLING thread
            right before the page is submitted (page rendering — PyMuPDF
            documents are not thread-safe). A raising `prepare` makes the
            page a failed page.
        executor_factory: ``n -> Executor`` (default: a thread pool).
        submit: ``(executor, index) -> Future`` (default:
            ``executor.submit(ocr_page, index)``).
        collect: ``(index, future_value) -> text`` (default: identity).
        inline: run sequentially on the calling thread, no pool.
    """
    order = list(candidates)
    start = clock()
    cpu0 = _cpu_now()
    texts: dict[int, str] = {}
    attempted: list[int] = []
    failed: list[int] = []
    workers = max(1, int(workers))

    def budget_left() -> bool:
        if len(attempted) >= max_ocr_pages:
            return False
        return (clock() - start) < time_budget_seconds

    def _collect(i: int, value: Any) -> str:
        return collect(i, value) if collect is not None else str(value)

    pos = 0
    if inline:
        while pos < len(order):
            if not budget_left():
                break
            i = order[pos]
            pos += 1
            attempted.append(i)
            try:
                if prepare is not None:
                    prepare(i)
                texts[i] = _collect(i, ocr_page(i))
            except Exception:
                failed.append(i)
                texts[i] = ""
    else:
        factory = executor_factory or _default_executor_factory
        do_submit = submit or (lambda ex, i: ex.submit(ocr_page, i))
        executor: Executor | None = None
        inflight: dict[Future[Any], tuple[int, float]] = {}

        def wait_some() -> None:
            """Block until at least one in-flight page finishes or times out."""
            now = time.monotonic()
            timeout = max(0.0, min(dl for _, dl in inflight.values()) - now)
            done, _ = wait(list(inflight), timeout=timeout, return_when=FIRST_COMPLETED)
            for fut in done:
                i, _ = inflight.pop(fut)
                try:
                    texts[i] = _collect(i, fut.result())
                except Exception:
                    failed.append(i)
                    texts[i] = ""
            now = time.monotonic()
            for fut, (i, dl) in list(inflight.items()):
                if now >= dl:
                    inflight.pop(fut)
                    fut.cancel()
                    failed.append(i)
                    texts[i] = ""

        try:
            while pos < len(order):
                if len(inflight) >= workers:
                    wait_some()
                    continue
                if not budget_left():
                    break
                i = order[pos]
                pos += 1
                attempted.append(i)
                if prepare is not None:
                    try:
                        prepare(i)
                    except Exception:
                        failed.append(i)
                        texts[i] = ""
                        continue
                if executor is None:
                    executor = factory(min(workers, len(order) - pos + 1))
                try:
                    fut = do_submit(executor, i)
                except Exception:
                    failed.append(i)
                    texts[i] = ""
                    continue
                inflight[fut] = (i, time.monotonic() + page_timeout_seconds)
            while inflight:
                wait_some()
        finally:
            if executor is not None:
                # Never join a worker that may be stuck in a native call.
                executor.shutdown(wait=False, cancel_futures=True)

    skipped = order[pos:]
    warnings: list[str] = []
    if skipped:
        warnings.append(f"{_plural(len(skipped), 'page')} skipped: ocr budget")
    if failed:
        warnings.append(f"{_plural(len(failed), 'page')} failed")
    return OcrRunResult(
        texts=texts,
        attempted=attempted,
        skipped=skipped,
        failed=failed,
        warnings=warnings,
        seconds=max(0.0, clock() - start),
        cpu_seconds=max(0.0, _cpu_now() - cpu0),
    )
