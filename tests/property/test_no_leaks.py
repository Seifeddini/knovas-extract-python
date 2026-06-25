"""Memory-leak regression — extract the corpus repeatedly, assert RSS stays bounded.

This catches Python-level reference-cycle bugs in the extractors AND
C-level leaks in the third-party parsers (PyMuPDF / mammoth / extract-msg)
that aren't covered by individual unit tests.

Marked `slow` so it only runs in the `property` hatch env / nightly job,
not the per-PR matrix. Marked `linux_only` because RSS measurement is
much noisier on Windows.
"""

from __future__ import annotations

import contextlib
import gc

import pytest

from knovas_extract import extract
from knovas_extract.errors import DependencyMissingError
from knovas_extract.result import Limits

pytestmark = [pytest.mark.property, pytest.mark.slow, pytest.mark.linux_only]

psutil = pytest.importorskip("psutil")


# Cap memory + iterations so the test is bounded.
ITERATIONS = 200
# Growth ceiling: allow 10% RSS growth + 30 MiB absolute slack (CPython arena
# allocations are sticky after the first warm-up wave).
GROWTH_PCT_CAP = 10
ABS_SLACK_MIB = 30


def _rss_mib() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


@pytest.fixture
def synthetic_payloads() -> list[tuple[bytes, str]]:
    """Generate small in-memory payloads we know the lazy-loaded extractors
    can handle without needing the spec sibling. One per format we can
    synthesize from stdlib alone or with installed optional deps."""
    out: list[tuple[bytes, str]] = []
    out.append((b"plain text body. " * 20, "text/plain"))
    out.append((b"# Heading\n\nMarkdown body with a paragraph.\n", "text/markdown"))
    out.append(
        (b"<!DOCTYPE html><html><body><h1>T</h1><p>body</p></body></html>", "text/html"),
    )
    out.append((b"From: a@x.com\r\nSubject: t\r\n\r\nbody\r\n", "message/rfc822"))
    out.append((rb"{\rtf1\ansi Hello RTF.}", "application/rtf"))
    return out


def test_no_leak_in_extract_loop(synthetic_payloads: list[tuple[bytes, str]]) -> None:
    """Extract every payload ITERATIONS times; assert RSS doesn't drift up.

    Allowance: the FIRST 10 iterations are warm-up (lazy-import + parser
    initialization); we measure RSS *after* warm-up and compare to RSS
    after ITERATIONS more. Drift past GROWTH_PCT_CAP + ABS_SLACK_MIB is
    a real leak.
    """
    limits = Limits(max_input_bytes=1 << 20, max_text_bytes=1 << 20)

    # Warm-up — first call per format triggers the lazy import + any
    # one-shot allocations (e.g. python-magic loading libmagic).
    # DependencyMissingError just means the extra isn't installed in this env.
    for _ in range(10):
        for data, mime in synthetic_payloads:
            with contextlib.suppress(DependencyMissingError):
                extract(data, mime=mime, limits=limits)
    gc.collect()
    baseline = _rss_mib()

    for _ in range(ITERATIONS):
        for data, mime in synthetic_payloads:
            with contextlib.suppress(DependencyMissingError):
                extract(data, mime=mime, limits=limits)

    gc.collect()
    final = _rss_mib()
    growth_mib = final - baseline
    growth_pct = (growth_mib / max(1.0, baseline)) * 100

    print(
        f"\nRSS baseline={baseline:.1f} MiB  final={final:.1f} MiB  "
        f"growth={growth_mib:+.1f} MiB ({growth_pct:+.2f}%) over "
        f"{ITERATIONS}x{len(synthetic_payloads)} extractions"
    )

    assert growth_mib <= ABS_SLACK_MIB or growth_pct <= GROWTH_PCT_CAP, (
        f"RSS grew by {growth_mib:.1f} MiB ({growth_pct:.2f}%) over "
        f"{ITERATIONS} iterations — potential memory leak in an extractor "
        f"or its underlying parser. Investigate with tracemalloc."
    )
