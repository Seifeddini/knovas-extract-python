"""Unit tests for the RTF extractor."""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract.errors import CorruptDocumentError

pytest.importorskip("striprtf")


@pytest.mark.unit
def test_simple_rtf_extracts_text() -> None:
    rtf = rb"{\rtf1\ansi Hello \b bold\b0  world.\par Second paragraph.}"
    r = extract(rtf, mime="application/rtf")
    assert "Hello bold world." in r.content.text
    assert "Second paragraph." in r.content.text


@pytest.mark.unit
def test_non_rtf_input_raises_corrupt() -> None:
    with pytest.raises(CorruptDocumentError):
        extract(b"this is not RTF at all", mime="application/rtf")


@pytest.mark.unit
def test_object_linking_emits_warning_but_extracts_body() -> None:
    """CVE-2017-0199-class regression: \\object control words trigger a warning
    but the payload bytes are never executed/fetched."""
    rtf = (
        rb"{\rtf1\ansi {\object\objemb\objclass{Excel.Sheet}{\*\objdata 010500}} "
        rb"body still extracts.\par}"
    )
    r = extract(rtf, mime="application/rtf")
    assert any("OLE object-linking" in w for w in r.warnings)
    assert "body still extracts." in r.content.text


@pytest.mark.unit
def test_text_rtf_mime_also_accepted() -> None:
    rtf = rb"{\rtf1\ansi Hello.}"
    r = extract(rtf, mime="text/rtf")
    # source.mime_type is what dispatch detected, not what we passed; but
    # extraction itself should succeed.
    assert "Hello." in r.content.text
