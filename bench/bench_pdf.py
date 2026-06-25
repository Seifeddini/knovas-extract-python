"""pytest-benchmark suite for the PDF extractor.

Run:
    hatch -e bench run run -- bench/bench_pdf.py

CI gates a >10% regression vs the `main` baseline (see release/CI doc).
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("fitz")
import fitz  # noqa: E402

from knovas_extract import extract  # noqa: E402


def _make_pdf(pages: int) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}", fontsize=12)
        page.insert_text(
            (72, 120),
            ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20),
            fontsize=10,
        )
    buf = io.BytesIO()
    doc.save(buf, deflate=True)
    doc.close()
    return buf.getvalue()


SMALL = _make_pdf(1)
MEDIUM = _make_pdf(10)
LARGE = _make_pdf(100)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(SMALL, id="pdf-1page"),
        pytest.param(MEDIUM, id="pdf-10page"),
        pytest.param(LARGE, id="pdf-100page"),
    ],
)
def test_extract_pdf(benchmark, payload: bytes) -> None:
    r = benchmark(lambda: extract(payload, mime="application/pdf"))
    assert r.metadata.page_count > 0
