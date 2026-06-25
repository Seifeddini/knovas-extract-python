"""Hypothesis-driven robustness tests.

Invariants:
- `extract()` either returns a valid ExtractionResult OR raises a subclass of ExtractError.
- It NEVER lets through a bare Exception / ValueError / KeyError.
- It NEVER makes a network call (enforced by pytest-socket globally).
- The returned result always validates against the spec schema.

We feed random bytes / mutated valid inputs and assert these invariants hold.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from knovas_extract import ExtractionResult, extract
from knovas_extract.errors import ExtractError
from knovas_extract.result import Limits

pytestmark = pytest.mark.property


# Cap memory blow-up — hypothesis loves to find 100 MB strings.
SMALL_LIMITS = Limits(max_input_bytes=1 << 20, max_text_bytes=1 << 20)

# Reasonable MIMEs we know about — pulled from the LAZY_LOADERS keys.
KNOWN_MIMES = st.sampled_from(
    [
        "text/plain",
        "text/markdown",
        "application/pdf",
        "text/html",
    ]
)


@given(data=st.binary(max_size=1 << 16))
@settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.large_base_example],
)
def test_random_bytes_never_crash_text_plain(data: bytes) -> None:
    """text/plain dispatch must handle arbitrary bytes without bare crash."""
    try:
        result = extract(data, mime="text/plain", limits=SMALL_LIMITS)
    except ExtractError:
        return  # acceptable
    assert isinstance(result, ExtractionResult)
    assert result.source.size_bytes == len(data)


@given(data=st.binary(max_size=1 << 16))
@settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.large_base_example],
)
def test_random_bytes_never_crash_markdown(data: bytes) -> None:
    try:
        result = extract(data, mime="text/markdown", limits=SMALL_LIMITS)
    except ExtractError:
        return
    assert isinstance(result, ExtractionResult)


@given(text=st.text(max_size=10_000))
@settings(max_examples=100, deadline=2000)
def test_unicode_text_round_trips(text: str) -> None:
    """Any UTF-8-encodable text → text/plain extractor → canonicalized output
    that re-canonicalizes to itself."""
    from knovas_extract.normalize import canonicalize_text

    raw = text.encode("utf-8")
    result = extract(raw, mime="text/plain", limits=SMALL_LIMITS)
    # Canonicalizing the actual output should be a no-op (idempotent).
    assert canonicalize_text(result.content.text) == result.content.text
