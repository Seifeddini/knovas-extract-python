"""Positive assertions of the metadata scalar sanitizer.

Every rejection class has a test. Warnings are counted, content-free.
"""

from __future__ import annotations

from collections import Counter

import pytest

from knovas_extract import extract
from knovas_extract._metadata import finalize_warnings, sanitize_scalar
from knovas_extract.result import Limits

pytest.importorskip("selectolax")


@pytest.mark.unit
def test_none_returns_none() -> None:
    assert sanitize_scalar(None, limits=Limits(), counts=Counter()) is None


@pytest.mark.unit
def test_bool_passes_through() -> None:
    assert sanitize_scalar(True, limits=Limits(), counts=Counter()) is True
    assert sanitize_scalar(False, limits=Limits(), counts=Counter()) is False


@pytest.mark.unit
def test_int_float_pass_through() -> None:
    assert sanitize_scalar(42, limits=Limits(), counts=Counter()) == 42
    assert sanitize_scalar(3.14, limits=Limits(), counts=Counter()) == 3.14


@pytest.mark.unit
def test_normal_string_passes_through() -> None:
    assert sanitize_scalar("  hello  ", limits=Limits(), counts=Counter()) == "hello"


@pytest.mark.unit
def test_control_char_dropped_and_counted() -> None:
    counts: Counter[str] = Counter()
    result = sanitize_scalar("foo\x1bbar", limits=Limits(), counts=counts)
    assert result is None
    assert counts["control_chars"] == 1


@pytest.mark.unit
def test_bidi_override_dropped_and_counted() -> None:
    counts: Counter[str] = Counter()
    result = sanitize_scalar("foo‮bar", limits=Limits(), counts=counts)
    assert result is None
    assert counts["control_chars"] == 1


@pytest.mark.unit
def test_nul_byte_dropped_and_counted() -> None:
    counts: Counter[str] = Counter()
    result = sanitize_scalar("foo\x00bar", limits=Limits(), counts=counts)
    assert result is None
    assert counts["control_chars"] == 1


@pytest.mark.unit
def test_truncation_counted() -> None:
    counts: Counter[str] = Counter()
    limits = Limits(max_metadata_value_length=10)
    result = sanitize_scalar("a" * 20, limits=limits, counts=counts)
    assert result == "a" * 10
    assert counts["truncated"] == 1


@pytest.mark.unit
def test_nested_dict_serialized() -> None:
    counts: Counter[str] = Counter()
    result = sanitize_scalar({"a": 1, "b": 2}, limits=Limits(), counts=counts)
    # Deterministic sort_keys=True → known output.
    assert result == '{"a": 1, "b": 2}'


@pytest.mark.unit
def test_finalize_warnings_deterministic_order() -> None:
    warnings: list[str] = []
    counts: Counter[str] = Counter({"truncated": 3, "control_chars": 2})
    finalize_warnings(counts, warnings)
    # Sorted by key name — deterministic.
    assert warnings == [
        "metadata: 2 values dropped for NUL / control / bidi-override characters",
        "metadata: 3 values truncated",
    ]


@pytest.mark.unit
def test_finalize_warnings_content_free() -> None:
    """Warning messages must never contain the offending value."""
    warnings: list[str] = []
    counts: Counter[str] = Counter()
    payload = "SECRETSECRET\x1btoken"
    sanitize_scalar(payload, limits=Limits(), counts=counts)
    finalize_warnings(counts, warnings)
    assert warnings, "expected at least one warning"
    for w in warnings:
        assert "SECRET" not in w
        assert "token" not in w
        assert "\x1b" not in w


@pytest.mark.unit
def test_html_hostile_meta_dropped() -> None:
    """End-to-end: hostile HTML meta content lands as counted drop, not verbatim in extra."""
    hostile = (
        b'<html><head><title>OK</title><meta name="author" content="Jane\x1bDoe"></head>'
        b"<body>x</body></html>"
    )
    r = extract(hostile, mime="text/html")
    assert "html:author" not in r.metadata.extra
    assert any("metadata:" in w for w in r.warnings)


@pytest.mark.unit
def test_html_hostile_canonical_url_dropped() -> None:
    """javascript: canonical URL must be dropped with counted warning."""
    hostile = (
        b'<html><head><title>OK</title><link rel="canonical" href="javascript:alert(1)">'
        b"</head><body>x</body></html>"
    )
    r = extract(hostile, mime="text/html")
    assert "html:canonical" not in r.metadata.extra
    assert any("disallowed scheme" in w for w in r.warnings)
