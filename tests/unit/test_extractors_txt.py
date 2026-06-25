"""Unit tests for the txt extractor."""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract.errors import ResourceExhaustedError
from knovas_extract.result import Limits


@pytest.mark.unit
def test_simple_ascii() -> None:
    r = extract(b"hello world", mime="text/plain")
    assert r.content.text == "hello world"
    assert r.source.mime_type == "text/plain"
    assert r.source.size_bytes == 11
    assert r.metadata.word_count == 2


@pytest.mark.unit
def test_utf8_bom_stripped() -> None:
    r = extract(b"\xef\xbb\xbfhello", mime="text/plain")
    assert r.content.text == "hello"
    assert r.metadata.extra["txt:charset_detected"] == "utf-8"


@pytest.mark.unit
def test_utf16_le_bom() -> None:
    raw = b"\xff\xfe" + "héllo".encode("utf-16-le")
    r = extract(raw, mime="text/plain")
    assert r.content.text == "héllo"


@pytest.mark.unit
def test_crlf_normalized_to_lf() -> None:
    r = extract(b"a\r\nb\r\nc", mime="text/plain")
    assert "\r" not in r.content.text
    assert r.content.text == "a\nb\nc"


@pytest.mark.unit
def test_empty_input() -> None:
    r = extract(b"", mime="text/plain")
    assert r.content.text == ""
    assert r.metadata.word_count == 0


@pytest.mark.unit
def test_resource_limit_input_size() -> None:
    big = b"x" * 1000
    with pytest.raises(ResourceExhaustedError) as exc:
        extract(big, mime="text/plain", limits=Limits(max_input_bytes=500, max_text_bytes=500))
    assert exc.value.what == "input size"


@pytest.mark.unit
def test_dispatch_detects_text_plain_without_mime() -> None:
    r = extract(b"plain ascii content with no header")
    assert r.source.mime_type == "text/plain"
