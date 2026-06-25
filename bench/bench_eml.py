"""pytest-benchmark suite for the EML extractor."""

from __future__ import annotations

import pytest

from knovas_extract import extract


def _make_eml(body_repeats: int) -> bytes:
    body = ("Lorem ipsum dolor sit amet. " * 20 + "\r\n") * body_repeats
    return (
        b"From: a@example.com\r\nTo: b@example.com\r\nSubject: bench\r\n"
        b"Date: Mon, 01 Jan 2026 12:00:00 +0000\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n' + body.encode()
    )


SMALL = _make_eml(1)
MEDIUM = _make_eml(100)
LARGE = _make_eml(2000)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(SMALL, id="eml-1body"),
        pytest.param(MEDIUM, id="eml-100body"),
        pytest.param(LARGE, id="eml-2000body"),
    ],
)
def test_extract_eml(benchmark, payload: bytes) -> None:
    r = benchmark(lambda: extract(payload, mime="message/rfc822"))
    assert r.metadata.title == "bench"
