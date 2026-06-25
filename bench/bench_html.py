"""pytest-benchmark suite for the HTML extractor."""

from __future__ import annotations

import pytest

pytest.importorskip("selectolax")

from knovas_extract import extract  # noqa: E402


def _make_html(paragraphs: int) -> bytes:
    body = "\n".join(
        f"<h2>Section {i // 5}</h2>\n<p>Paragraph {i}: lorem ipsum.</p>" for i in range(paragraphs)
    )
    return f"<!DOCTYPE html><html><body>{body}</body></html>".encode()


SMALL = _make_html(10)
MEDIUM = _make_html(200)
LARGE = _make_html(2000)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(SMALL, id="html-10para"),
        pytest.param(MEDIUM, id="html-200para"),
        pytest.param(LARGE, id="html-2000para"),
    ],
)
def test_extract_html(benchmark, payload: bytes) -> None:
    r = benchmark(lambda: extract(payload, mime="text/html"))
    assert len(r.content.text) > 0
