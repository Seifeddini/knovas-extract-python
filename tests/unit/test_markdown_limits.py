"""Resource-limit tests for the `emit_markdown=True` path.

- `max_text_bytes` caps the markdown string as well as the plain text.
- `max_markdown_expansion_ratio` catches inputs designed to blow up
  markdown-side (nested tables, run-lengths of nested emphasis).
- `max_recursion_depth` caps HTML DOM nesting via the depth walk in
  `_markdown.html_to_markdown`.

Each test triggers exactly one limit at a time; the others are relaxed
to keep the tests decoupled.
"""

from __future__ import annotations

import pytest

from knovas_extract import Limits, extract
from knovas_extract._markdown import html_to_markdown
from knovas_extract.errors import ResourceExhaustedError

pytest.importorskip("markdownify")
pytest.importorskip("selectolax")


@pytest.mark.unit
def test_max_text_bytes_caps_markdown_output() -> None:
    """A large HTML body produces large markdown; the size cap must fire."""
    body = "<p>" + ("x " * 5_000) + "</p>"
    limits = Limits(
        max_input_bytes=10 * 1024 * 1024,
        max_text_bytes=1024,  # 1 KiB — the paragraph is many times that
        max_markdown_expansion_ratio=100.0,
    )
    with pytest.raises(ResourceExhaustedError) as excinfo:
        html_to_markdown(body, limits, warnings=[])
    assert excinfo.value.what == "markdown size"


@pytest.mark.unit
def test_max_markdown_expansion_ratio_fires() -> None:
    """Small plain text + heavily nested markup blows the ratio cap."""
    # Nested emphasis: each level doubles the marker count in markdown.
    nested = "<em>" * 200 + "x" + "</em>" * 200
    body = f"<html><body><p>{nested}</p></body></html>"
    limits = Limits(
        max_text_bytes=10 * 1024 * 1024,
        max_markdown_expansion_ratio=2.0,  # very tight
    )
    with pytest.raises(ResourceExhaustedError) as excinfo:
        extract(body.encode(), mime="text/html", emit_markdown=True, limits=limits)
    assert excinfo.value.what == "markdown expansion ratio"


@pytest.mark.unit
def test_max_recursion_depth_caps_dom_walk() -> None:
    """A deeply-nested DOM raises before markdownify runs."""
    depth = 300
    body = "<div>" * depth + "leaf" + "</div>" * depth
    limits = Limits(max_recursion_depth=64)
    with pytest.raises(ResourceExhaustedError) as excinfo:
        html_to_markdown(body, limits, warnings=[])
    assert excinfo.value.what == "html nesting depth"


@pytest.mark.unit
def test_max_text_bytes_from_extract_path() -> None:
    """The extract() surface propagates the size cap for markdown too."""
    body = "<html><body><p>" + ("x " * 5_000) + "</p></body></html>"
    limits = Limits(
        max_input_bytes=10 * 1024 * 1024,
        max_text_bytes=1024,
        max_markdown_expansion_ratio=100.0,
    )
    with pytest.raises(ResourceExhaustedError):
        extract(body.encode(), mime="text/html", emit_markdown=True, limits=limits)
