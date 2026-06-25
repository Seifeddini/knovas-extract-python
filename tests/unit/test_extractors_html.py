"""Unit tests for the HTML extractor."""

from __future__ import annotations

import pytest

from knovas_extract import extract

pytest.importorskip("selectolax")


@pytest.mark.unit
def test_simple_html_extracts_title_lang_and_text() -> None:
    html = b'<html lang="en"><head><title>T</title></head><body><p>Hi.</p></body></html>'
    r = extract(html, mime="text/html")
    assert r.metadata.title == "T"
    assert r.metadata.language == "en"
    assert "Hi." in r.content.text


@pytest.mark.unit
def test_scripts_and_styles_stripped() -> None:
    html = b"""<html><body>
<p>visible</p>
<script>alert('XSS payload that must NOT appear in text');</script>
<style>.hidden { color: red; }</style>
</body></html>"""
    r = extract(html, mime="text/html")
    assert "visible" in r.content.text
    assert "XSS payload" not in r.content.text
    assert "color: red" not in r.content.text


@pytest.mark.unit
def test_meta_description_and_keywords() -> None:
    html = b"""<html><head>
<meta name="description" content="hello">
<meta name="keywords" content="a,b,c">
</head><body><p>x</p></body></html>"""
    r = extract(html, mime="text/html")
    assert r.metadata.extra["html:description"] == "hello"
    assert r.metadata.extra["html:keywords"] == "a,b,c"


@pytest.mark.unit
def test_sections_from_headings() -> None:
    html = b"<html><body><h1>A</h1><p>a1</p><h2>B</h2><p>b1</p><h2>C</h2><p>c1</p></body></html>"
    r = extract(html, mime="text/html")
    assert r.content.sections is not None
    by_heading = {s.heading: s for s in r.content.sections}
    assert by_heading["A"].level == 1
    assert by_heading["B"].level == 2
    assert by_heading["B"].text == "b1"
    assert by_heading["C"].text == "c1"


@pytest.mark.unit
def test_malformed_html_recovers() -> None:
    """lexbor handles unclosed tags by inserting implicit closures."""
    html = b"<html><body><p>unclosed paragraph<div>still extracts</body>"
    r = extract(html, mime="text/html")
    assert "unclosed paragraph" in r.content.text
    assert "still extracts" in r.content.text


@pytest.mark.unit
def test_charset_declared_meta_tag() -> None:
    html = b'<html><head><meta charset="utf-8"><title>x</title></head><body></body></html>'
    r = extract(html, mime="text/html")
    assert r.metadata.extra["html:charset_declared"] == "utf-8"


@pytest.mark.unit
def test_entity_references_NOT_expanded() -> None:
    """selectolax is HTML5 (lexbor), not XML. <!ENTITY> declarations are ignored."""
    html = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE x [<!ENTITY foo "BOMB">]>\n'
        b"<html><body><p>&foo;</p></body></html>"
    )
    r = extract(html, mime="text/html")
    assert "BOMB" not in r.content.text  # entity never expanded
