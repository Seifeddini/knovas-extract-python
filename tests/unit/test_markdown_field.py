"""Contract-level tests for the `content.markdown` field.

Guards the additive-field promise:
- `emit_markdown=False` (default) → `content.markdown is None` for every format.
- `content.markdown` round-trips through `to_dict` / `from_dict`.
- `spec_version == "1.1.0"` (bumped from 1.0.0 to signal the new field).

Format-specific markdown shape lives in `test_markdown_per_format.py`; the
sanitizer contract lives in `test_markdown_sanitization.py`.
"""

from __future__ import annotations

import pytest

from knovas_extract import SPEC_VERSION, extract
from knovas_extract.result import (
    Content,
    ExtractionResult,
    Extractor,
    Metadata,
    Source,
)


@pytest.mark.unit
def test_spec_version_is_at_least_11() -> None:
    """1.1.0 = the release that added content.markdown; 1.2.0 added sentences."""
    assert SPEC_VERSION >= "1.1.0"


@pytest.mark.unit
def test_default_is_no_markdown_for_txt() -> None:
    r = extract(b"hello world", mime="text/plain")
    assert r.content.markdown is None


@pytest.mark.unit
def test_default_is_no_markdown_for_html() -> None:
    # HTML extraction requires the `html` extra (selectolax); skip when it is
    # not installed, matching the guard in test_extractors_html.py.
    pytest.importorskip("selectolax")
    r = extract(b"<html><body><p>hi</p></body></html>", mime="text/html")
    assert r.content.markdown is None


@pytest.mark.unit
def test_default_is_no_markdown_for_md() -> None:
    r = extract(b"# H\n\nBody.", mime="text/markdown")
    assert r.content.markdown is None


@pytest.mark.unit
def test_to_dict_null_markdown() -> None:
    """Absent markdown serializes as JSON null, not missing."""
    r = extract(b"hi", mime="text/plain")
    d = r.to_dict()
    assert "markdown" in d["content"]
    assert d["content"]["markdown"] is None


@pytest.mark.unit
def test_round_trip_preserves_markdown_string() -> None:
    r = ExtractionResult(
        spec_version=SPEC_VERSION,
        source=Source(mime_type="text/plain", sha256="0" * 64, size_bytes=0),
        metadata=Metadata(),
        content=Content(text="hello", markdown="hello"),
        warnings=[],
        extractor=Extractor(name="x", version="0.0.0"),
    )
    rebuilt = ExtractionResult.from_dict(r.to_dict())
    assert rebuilt.content.markdown == "hello"


@pytest.mark.unit
def test_round_trip_distinguishes_empty_string_from_none() -> None:
    """Empty-string markdown means 'no structure produced'; null means 'not emitted'."""
    empty = ExtractionResult(
        spec_version=SPEC_VERSION,
        source=Source(mime_type="text/plain", sha256="0" * 64, size_bytes=0),
        metadata=Metadata(),
        content=Content(text="", markdown=""),
        warnings=[],
        extractor=Extractor(name="x", version="0.0.0"),
    )
    null_ = ExtractionResult(
        spec_version=SPEC_VERSION,
        source=Source(mime_type="text/plain", sha256="0" * 64, size_bytes=0),
        metadata=Metadata(),
        content=Content(text="", markdown=None),
        warnings=[],
        extractor=Extractor(name="x", version="0.0.0"),
    )
    assert empty.to_dict()["content"]["markdown"] == ""
    assert null_.to_dict()["content"]["markdown"] is None
    assert ExtractionResult.from_dict(empty.to_dict()).content.markdown == ""
    assert ExtractionResult.from_dict(null_.to_dict()).content.markdown is None
