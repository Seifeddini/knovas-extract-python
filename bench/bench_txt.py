"""pytest-benchmark suite for the txt extractor.

Run:
    hatch -e bench run run

CI gates a >10% regression vs the `main` baseline (see release/CI doc).
"""

from __future__ import annotations

import pytest

from knovas_extract import extract

SMALL = b"Hello world. " * 100
MEDIUM = b"Lorem ipsum dolor sit amet. " * 10_000
LARGE = b"A" * (1 << 20)  # 1 MiB


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(SMALL, id="small-1kb"),
        pytest.param(MEDIUM, id="medium-280kb"),
        pytest.param(LARGE, id="large-1mb"),
    ],
)
def test_extract_txt(benchmark, payload: bytes) -> None:
    result = benchmark(lambda: extract(payload, mime="text/plain"))
    assert result.source.size_bytes == len(payload)
