"""Obligation tests: the bounded, fail-soft OCR scheduler (GI-EXTRACT-01/02).

Alloy: KnowledgeBase ``models/alloy/mechanisms/client_pipeline.als`` preds
``BoundedOcrMechanism``, ``NoSpuriousSkipMechanism``, ``FailSoftMechanism``
(model ``data_plane/ocr_budget_failsoft.als``; mutants ``ocr__spurious_skip``,
``ocr__budget_raises``).

The scheduler is a pure function over "OCR this page index" callables, so
the budget semantics are tested without Tesseract, PyMuPDF or threads of
any real engine:

    run_ocr_schedule(candidates, ocr_page, *, max_ocr_pages,
                     time_budget_seconds, page_timeout_seconds, workers,
                     clock=time.monotonic) -> OcrRunResult

    OcrRunResult: texts {index: str}, attempted [idx], skipped [idx],
                  failed [idx], warnings [str], seconds float

RED until the feature lands.
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = [pytest.mark.unit]


def _ok(index: int) -> str:
    return f"text of page {index}"


class _Clock:
    """A fake monotonic clock the scheduler consults; pages advance it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


class TestBoundedOcrMechanism:
    """Alloy obligation: mechanisms/client_pipeline.als::BoundedOcrMechanism."""

    def test_attempts_never_exceed_max_ocr_pages(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        calls: list[int] = []

        def ocr(i: int) -> str:
            calls.append(i)
            return _ok(i)

        res = run_ocr_schedule(
            list(range(12)),
            ocr,
            max_ocr_pages=5,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=4,
        )
        assert len(res.attempted) == 5 and len(calls) == 5
        assert sorted(res.skipped) == list(range(5, 12))
        assert set(res.texts) == set(res.attempted)

    def test_pages_are_submitted_in_document_order(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        res = run_ocr_schedule(
            [3, 7, 9, 11],
            _ok,
            max_ocr_pages=2,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=1,
        )
        assert res.attempted == [3, 7] and res.skipped == [9, 11]


class TestNoSpuriousSkipMechanism:
    """Alloy obligation: mechanisms/client_pipeline.als::NoSpuriousSkipMechanism."""

    def test_no_page_skipped_while_budget_remains(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        res = run_ocr_schedule(
            list(range(8)),
            _ok,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=3,
        )
        assert res.skipped == [] and len(res.attempted) == 8 and res.failed == []

    def test_one_failed_page_does_not_stop_the_rest(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        def ocr(i: int) -> str:
            if i == 2:
                raise RuntimeError("engine hiccup on page 2")
            return _ok(i)

        res = run_ocr_schedule(
            list(range(6)),
            ocr,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=2,
        )
        assert res.failed == [2] and res.skipped == []
        assert sorted(res.attempted) == list(range(6))
        assert res.texts[2] == "" and res.texts[5] == _ok(5)

    def test_skip_happens_only_after_time_budget_is_spent(self):
        """The time budget stops SUBMITTING; pages submitted before the
        trip finish and count as attempted, the rest are skipped."""
        from knovas_extract._ocr.pool import run_ocr_schedule

        clock = _Clock()

        def ocr(i: int) -> str:
            clock.tick(10.0)
            return _ok(i)

        res = run_ocr_schedule(
            list(range(10)),
            ocr,
            max_ocr_pages=500,
            time_budget_seconds=35.0,
            page_timeout_seconds=60,
            workers=1,
            clock=clock,
        )
        assert len(res.attempted) == 4, "4 x 10 s fit a 35 s budget; the 5th is not submitted"
        assert sorted(res.skipped) == list(range(4, 10))


class TestFailSoftMechanism:
    """Alloy obligation: mechanisms/client_pipeline.als::FailSoftMechanism."""

    def test_budget_trip_returns_partial_result_with_counts(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        res = run_ocr_schedule(
            list(range(7)),
            _ok,
            max_ocr_pages=3,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=2,
        )
        assert len(res.attempted) == 3 and len(res.skipped) == 4
        assert any("skipped" in w and "4" in w for w in res.warnings)
        # every candidate has exactly one outcome
        assert sorted(res.attempted + res.skipped) == list(range(7))

    def test_page_exception_is_counted_not_raised(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        def ocr(i: int) -> str:
            raise MemoryError("decoder blew up")

        res = run_ocr_schedule(
            [0, 1],
            ocr,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=1,
        )
        assert res.failed == [0, 1] and res.texts == {0: "", 1: ""}
        assert any("2" in w and "failed" in w for w in res.warnings)

    def test_page_timeout_is_a_failed_page_not_a_document_failure(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        def ocr(i: int) -> str:
            if i == 1:
                time.sleep(0.3)
            return _ok(i)

        res = run_ocr_schedule(
            [0, 1, 2],
            ocr,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=0.05,
            workers=2,
        )
        assert 1 in res.failed and res.texts[0] == _ok(0) and res.texts[2] == _ok(2)

    def test_result_is_deterministic_for_one_and_many_workers(self):
        from knovas_extract._ocr.pool import run_ocr_schedule

        a = run_ocr_schedule(
            list(range(9)),
            _ok,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=1,
        )
        b = run_ocr_schedule(
            list(range(9)),
            _ok,
            max_ocr_pages=500,
            time_budget_seconds=1e9,
            page_timeout_seconds=60,
            workers=4,
        )
        assert a.texts == b.texts and a.attempted == b.attempted


def test_warnings_never_contain_page_text():
    """GI-EXTRACT-04: the scheduler's warnings are counts, never content."""
    from knovas_extract._ocr.pool import run_ocr_schedule

    payload = "Lohn Meier Hans 123'456.00"

    def ocr(i: int) -> str:
        if i == 1:
            raise RuntimeError(payload)
        return payload

    res = run_ocr_schedule(
        [0, 1, 2], ocr, max_ocr_pages=2, time_budget_seconds=1e9, page_timeout_seconds=60, workers=1
    )
    assert res.warnings, "a failed page and a skipped page each produce a counted warning"
    assert all(payload not in w and "Meier" not in w for w in res.warnings)
