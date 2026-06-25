"""Unit tests for dispatch — MIME detection + extractor routing."""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract.errors import UnsupportedFormatError


@pytest.mark.unit
def test_explicit_mime_wins() -> None:
    # Bytes look like markdown, but we explicitly say text/plain.
    r = extract(b"# Heading\nBody", mime="text/plain")
    assert r.source.mime_type == "text/plain"
    assert r.content.sections is None


@pytest.mark.unit
def test_unsupported_format_raises_typed() -> None:
    with pytest.raises(UnsupportedFormatError) as exc:
        extract(b"\x00\x01\x02\xff\xfe\xff", mime="application/x-totally-unknown")
    assert "application/x-totally-unknown" in str(exc.value)


@pytest.mark.unit
def test_dispatch_sets_canonical_source_fields() -> None:
    """source.sha256 / size_bytes / mime_type are always set by dispatch, not
    trusted from the extractor."""
    payload = b"hello world"
    r = extract(payload, mime="text/plain")
    import hashlib

    assert r.source.sha256 == hashlib.sha256(payload).hexdigest()
    assert r.source.size_bytes == len(payload)
