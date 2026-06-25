"""Unit tests for the ExtractionResult dataclass — round-trip via JSON Schema."""

from __future__ import annotations

import pytest

from knovas_extract.result import (
    Content,
    ExtractionResult,
    Extractor,
    Metadata,
    Page,
    Section,
    Source,
)


@pytest.fixture
def sample_result() -> ExtractionResult:
    return ExtractionResult(
        spec_version="1.0.0",
        source=Source(
            mime_type="text/plain",
            sha256="e" * 64,
            size_bytes=42,
            filename="x.txt",
        ),
        metadata=Metadata(
            title="t", author="a", word_count=7, extra={"txt:charset_detected": "utf-8"}
        ),
        content=Content(
            text="hello world\n\nsecond paragraph",
            pages=[Page(index=0, text="hello world")],
            sections=[Section(heading="Intro", level=1, text="hello world")],
        ),
        warnings=["test warning"],
        extractor=Extractor(name="knovas-extract-python", version="0.1.0"),
    )


@pytest.mark.unit
def test_round_trip(sample_result: ExtractionResult) -> None:
    d = sample_result.to_dict()
    rebuilt = ExtractionResult.from_dict(d)
    assert rebuilt.to_dict() == d


@pytest.mark.unit
def test_collapses_empty_pages_and_sections_to_null() -> None:
    r = ExtractionResult(
        spec_version="1.0.0",
        source=Source(mime_type="text/plain", sha256="0" * 64, size_bytes=0),
        metadata=Metadata(),
        content=Content(text="hi", pages=None, sections=None),
        warnings=[],
        extractor=Extractor(name="x", version="0.0.0"),
    )
    d = r.to_dict()
    assert d["content"]["pages"] is None
    assert d["content"]["sections"] is None


@pytest.mark.unit
def test_validates_against_spec_schema(sample_result: ExtractionResult, schema: dict) -> None:
    """sample_result must validate against the live spec/schema.json."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate(sample_result.to_dict())
