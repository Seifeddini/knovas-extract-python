"""Unit tests for the PDF extractor — edge cases not covered by the golden corpus.

The golden tests prove "happy-path round-trip". This file pins behavioral
contracts for failure modes: encrypted, corrupted, oversize, page-cap.
"""

from __future__ import annotations

import io

import pytest

from knovas_extract import extract
from knovas_extract.errors import (
    CorruptDocumentError,
    EncryptedDocumentError,
    ResourceExhaustedError,
)
from knovas_extract.result import Limits

pytest.importorskip("fitz")  # extras [pdf] not installed → skip module
import fitz  # noqa: E402


@pytest.fixture
def simple_pdf_bytes() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Hello world.")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def n_page_pdf() -> callable:
    def _make(n: int) -> bytes:
        doc = fitz.open()
        for i in range(n):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {i + 1}")
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        return buf.getvalue()

    return _make


@pytest.mark.unit
def test_extracts_text_and_metadata(simple_pdf_bytes: bytes) -> None:
    r = extract(simple_pdf_bytes, mime="application/pdf")
    assert "Hello world." in r.content.text
    assert r.metadata.page_count == 1
    assert r.content.pages is not None
    assert len(r.content.pages) == 1


@pytest.mark.unit
def test_corrupt_pdf_raises_typed() -> None:
    with pytest.raises(CorruptDocumentError):
        extract(b"%PDF-1.7\nthis is not a valid PDF object stream", mime="application/pdf")


@pytest.mark.unit
def test_empty_input_is_corrupt() -> None:
    with pytest.raises(CorruptDocumentError):
        extract(b"", mime="application/pdf")


@pytest.mark.unit
def test_encrypted_pdf_raises_encrypted_error() -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "secret content")
    buf = io.BytesIO()
    doc.save(
        buf,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="o-pw",
        user_pw="u-pw",
    )
    doc.close()
    with pytest.raises(EncryptedDocumentError):
        extract(buf.getvalue(), mime="application/pdf")


@pytest.mark.unit
def test_page_cap_enforced(n_page_pdf) -> None:
    data = n_page_pdf(15)
    with pytest.raises(ResourceExhaustedError) as exc:
        extract(data, mime="application/pdf", limits=Limits(max_pages=10))
    assert exc.value.what == "page_count"
    assert exc.value.limit == 10


@pytest.mark.unit
def test_input_size_cap_enforced(simple_pdf_bytes: bytes) -> None:
    tiny_limit = Limits(max_input_bytes=10)
    with pytest.raises(ResourceExhaustedError) as exc:
        extract(simple_pdf_bytes, mime="application/pdf", limits=tiny_limit)
    assert exc.value.what == "input size"


@pytest.mark.unit
def test_pages_have_per_page_text(n_page_pdf) -> None:
    r = extract(n_page_pdf(3), mime="application/pdf")
    assert r.content.pages is not None
    assert [p.text for p in r.content.pages] == ["Page 1", "Page 2", "Page 3"]
    assert [p.index for p in r.content.pages] == [0, 1, 2]


@pytest.mark.unit
def test_dispatch_detects_pdf_from_header() -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "header-detected")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    r = extract(buf.getvalue())  # no mime override; rely on dispatch
    assert r.source.mime_type == "application/pdf"
