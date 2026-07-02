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


# --- emit_markdown=True path -----------------------------------------------

# Hostile HTML strategy: pick a scaffold and inject random combinations of
# denylisted tags and disallowed URL schemes on real elements. Snippets
# are always well-formed HTML elements (not free-standing attribute
# strings) so the sanitizer's element / attribute / URL paths all get
# exercised.
HOSTILE_TAGS = st.sampled_from(
    [
        "<script>alertSecret</script>",
        "<iframe>alertSecret</iframe>",
        "<object>alertSecret</object>",
        "<style>alertSecret</style>",
        "<embed>",
        '<a href="javascript:alertSecret">t</a>',
        '<a href="data:text/html,x">t</a>',
        '<a href="vbscript:msg">t</a>',
        '<a href="file:///etc/passwd">t</a>',
        '<a href="https://ok" onclick="alertSecret">t</a>',
        '<p style="background:expression(alertSecret)">t</p>',
    ]
)
HOSTILE_SNIPPETS = st.lists(HOSTILE_TAGS, min_size=1, max_size=8)
_DENYLIST_LITERALS_IN_MARKDOWN = (
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<style",
    "javascript:",
    "data:text/html",
    "vbscript:",
    "file://",
)


@given(snippets=HOSTILE_SNIPPETS)
@settings(
    max_examples=100,
    deadline=3000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.large_base_example],
)
def test_emit_markdown_never_leaks_hostile_literals(snippets: list[str]) -> None:
    """Whatever hostile HTML is thrown at the sanitizer, the emitted
    markdown must contain none of the denylisted literals or disallowed
    URL schemes.

    Skips silently when the markdown extras aren't installed — the
    property-level invariant only applies when the emit_markdown path
    can actually run.
    """
    pytest.importorskip("markdownify")
    pytest.importorskip("selectolax")

    body = "<html><body><p>Body</p>" + "".join(snippets) + "</body></html>"
    # Widen the ratio guard — this test is about content leakage, not
    # expansion. The size cap in SMALL_LIMITS still applies.
    limits = Limits(
        max_input_bytes=SMALL_LIMITS.max_input_bytes,
        max_text_bytes=SMALL_LIMITS.max_text_bytes,
        max_markdown_expansion_ratio=1000.0,
    )
    try:
        result = extract(
            body.encode(),
            mime="text/html",
            emit_markdown=True,
            limits=limits,
        )
    except ExtractError:
        return  # limit / dependency error is acceptable
    md = result.content.markdown or ""
    for literal in _DENYLIST_LITERALS_IN_MARKDOWN:
        assert literal not in md, f"leak: {literal!r} survived in {md!r}"
    # The invariant is stronger than "no denylist literal": the token
    # `alertSecret` (embedded inside every stripped tag) must ALSO be
    # gone — its survival would mean a hostile inner text leaked through
    # the sanitizer.
    assert "alertSecret" not in md, f"content leak: {md!r}"
