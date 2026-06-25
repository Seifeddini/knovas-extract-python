"""Unit tests for the typed error hierarchy.

Asserts the contract callers depend on: every public error is a subclass of
ExtractError; constructors set the documented attributes.
"""
from __future__ import annotations

import pytest

from knovas_extract.errors import (
    CorruptDocumentError,
    DependencyMissingError,
    EncryptedDocumentError,
    ExtractError,
    ResourceExhaustedError,
    UnsupportedFormatError,
)


@pytest.mark.unit
def test_all_errors_subclass_extract_error() -> None:
    for cls in (
        UnsupportedFormatError,
        CorruptDocumentError,
        EncryptedDocumentError,
        ResourceExhaustedError,
        DependencyMissingError,
    ):
        assert issubclass(cls, ExtractError)


@pytest.mark.unit
def test_unsupported_format_error_carries_mime_and_filename() -> None:
    exc = UnsupportedFormatError("application/x-weird", filename="foo.weird")
    assert exc.mime == "application/x-weird"
    assert exc.filename == "foo.weird"
    assert "application/x-weird" in str(exc)
    assert "foo.weird" in str(exc)


@pytest.mark.unit
def test_resource_exhausted_error_includes_observed() -> None:
    exc = ResourceExhaustedError("pages", 10_000, observed=12_345)
    assert exc.what == "pages"
    assert exc.limit == 10_000
    assert exc.observed == 12_345
    assert "10000" in str(exc)
    assert "12345" in str(exc)


@pytest.mark.unit
def test_dependency_missing_error_suggests_install_command() -> None:
    exc = DependencyMissingError("pdf", "pymupdf")
    assert exc.extra == "pdf"
    assert exc.missing_package == "pymupdf"
    assert "pip install" in str(exc)
    assert "[pdf]" in str(exc)
