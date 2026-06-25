"""Unit tests for the markdown extractor."""
from __future__ import annotations

import pytest

from knovas_extract import extract


@pytest.mark.unit
def test_simple_markdown_with_headings() -> None:
    src = b"# Title\n\nIntro paragraph.\n\n## Subsection\n\nDetails."
    r = extract(src, mime="text/markdown")
    assert "Title" in r.content.text
    assert r.content.sections is not None
    assert len(r.content.sections) == 2
    assert r.content.sections[0].heading == "Title"
    assert r.content.sections[0].level == 1
    assert r.content.sections[1].heading == "Subsection"
    assert r.content.sections[1].level == 2


@pytest.mark.unit
def test_frontmatter_lifts_title_author() -> None:
    src = b"---\ntitle: My Doc\nauthor: Siran\nlanguage: en\n---\n\nBody here."
    r = extract(src, mime="text/markdown")
    assert r.metadata.title == "My Doc"
    assert r.metadata.author == "Siran"
    assert r.metadata.language == "en"
    assert r.content.text == "Body here."
    assert r.content.sections is None  # no headings


@pytest.mark.unit
def test_frontmatter_unknown_keys_go_to_extra() -> None:
    src = b"---\ntitle: T\ntags: foo\nproject: knovas\n---\n\nBody."
    r = extract(src, mime="text/markdown")
    assert r.metadata.title == "T"
    assert r.metadata.extra.get("md:tags") == "foo"
    assert r.metadata.extra.get("md:project") == "knovas"


@pytest.mark.unit
def test_no_frontmatter() -> None:
    src = b"# Plain\n\nContent without frontmatter."
    r = extract(src, mime="text/markdown")
    assert r.metadata.title is None
    assert r.content.sections is not None
    assert r.content.sections[0].heading == "Plain"


@pytest.mark.unit
def test_section_text_runs_to_next_heading_of_equal_or_lower_level() -> None:
    src = b"# H1\n\nA\n\n## H2a\n\nB\n\n### H3\n\nC\n\n## H2b\n\nD"
    r = extract(src, mime="text/markdown")
    sections = r.content.sections
    assert sections is not None
    by_heading = {s.heading: s for s in sections}
    # H2a's text spans B and the H3+C content, ending at H2b.
    assert "B" in by_heading["H2a"].text
    assert "C" in by_heading["H2a"].text
    assert "D" not in by_heading["H2a"].text
    # H2b's text is just D.
    assert "D" in by_heading["H2b"].text
    assert "B" not in by_heading["H2b"].text
