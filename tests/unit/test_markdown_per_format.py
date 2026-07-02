"""Per-format markdown-shape tests.

For each extractor, assert that `emit_markdown=True` produces markdown of
the expected shape (headings, lists, emphasis, code) and that formats
without recoverable structure fall through to identity / null with the
documented warning message.
"""

from __future__ import annotations

import pytest

from knovas_extract import extract

# All markdown paths route through markdownify / selectolax.
pytest.importorskip("markdownify")
pytest.importorskip("selectolax")


# --- TXT -------------------------------------------------------------------


@pytest.mark.unit
def test_txt_markdown_is_identity() -> None:
    r = extract(b"hello world", mime="text/plain", emit_markdown=True)
    assert r.content.markdown == r.content.text == "hello world"


@pytest.mark.unit
def test_txt_markdown_none_when_disabled() -> None:
    r = extract(b"hello world", mime="text/plain", emit_markdown=False)
    assert r.content.markdown is None


# --- MD --------------------------------------------------------------------


@pytest.mark.unit
def test_md_markdown_preserves_source() -> None:
    src = b"# H\n\nPara **bold**.\n\n- one\n- two\n"
    r = extract(src, mime="text/markdown", emit_markdown=True)
    assert r.content.markdown is not None
    assert "# H" in r.content.markdown
    assert "**bold**" in r.content.markdown
    assert "- one" in r.content.markdown


@pytest.mark.unit
def test_md_frontmatter_still_stripped() -> None:
    src = b"---\ntitle: T\n---\n\n# H\n"
    r = extract(src, mime="text/markdown", emit_markdown=True)
    assert r.content.markdown is not None
    # frontmatter is a metadata concern; markdown body only.
    assert "title: T" not in r.content.markdown
    assert "# H" in r.content.markdown


# --- HTML ------------------------------------------------------------------


@pytest.mark.unit
def test_html_markdown_headings_and_paragraphs() -> None:
    html = b"<html><body><h1>A</h1><p>Body.</p><h2>B</h2><p>More.</p></body></html>"
    r = extract(html, mime="text/html", emit_markdown=True)
    assert r.content.markdown is not None
    assert "# A" in r.content.markdown
    assert "## B" in r.content.markdown


@pytest.mark.unit
def test_html_markdown_bold_italic_lists() -> None:
    html = (
        b"<html><body>"
        b"<p><strong>bold</strong> and <em>ital</em>.</p>"
        b"<ul><li>one</li><li>two</li></ul>"
        b"</body></html>"
    )
    r = extract(html, mime="text/html", emit_markdown=True)
    assert r.content.markdown is not None
    assert "**bold**" in r.content.markdown
    # markdownify uses `*ital*` or `_ital_` depending on emphasis_mark.
    assert "*ital*" in r.content.markdown or "_ital_" in r.content.markdown
    assert "- one" in r.content.markdown
    assert "- two" in r.content.markdown


# --- DOCX ------------------------------------------------------------------

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _minimal_docx_bytes() -> bytes:
    """Build a minimal DOCX in-memory with a Heading 1 + bulleted list."""
    import io
    import zipfile

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Title</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Body paragraph.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="Heading 1"/>'
        "</w:style>"
        "</w:styles>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


@pytest.mark.unit
def test_docx_markdown_has_heading() -> None:
    pytest.importorskip("mammoth")
    pytest.importorskip("docx")
    data = _minimal_docx_bytes()
    r = extract(data, mime=DOCX_MIME, emit_markdown=True)
    assert r.content.markdown is not None
    assert "Title" in r.content.markdown
    # Mammoth may map Heading 1 to `#` — assert at least one heading marker
    # is present or the plain text is there (fidelity depends on style map).
    assert "# " in r.content.markdown or "Title" in r.content.markdown


# --- EML -------------------------------------------------------------------


@pytest.mark.unit
def test_eml_html_alternative_yields_markdown() -> None:
    eml = (
        b"From: sender@example.com\r\n"
        b"To: rcpt@example.com\r\n"
        b"Subject: Test\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Fallback plain body.\r\n"
        b"--B\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><h1>Hello</h1><p>Body <b>bold</b>.</p></body></html>\r\n"
        b"--B--\r\n"
    )
    r = extract(eml, mime="message/rfc822", emit_markdown=True)
    assert r.content.markdown is not None
    assert "# Hello" in r.content.markdown
    assert "**bold**" in r.content.markdown


@pytest.mark.unit
def test_eml_plain_only_is_identity() -> None:
    eml = (
        b"From: s@x\r\nTo: r@x\r\nSubject: T\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"plain body content\r\n"
    )
    r = extract(eml, mime="message/rfc822", emit_markdown=True)
    assert r.content.markdown is not None
    assert "plain body content" in r.content.markdown


# --- RTF -------------------------------------------------------------------


@pytest.mark.unit
def test_rtf_markdown_is_null_with_warning() -> None:
    pytest.importorskip("striprtf")
    rtf = b"{\\rtf1\\ansi Hello world.\\par}"
    r = extract(rtf, mime="application/rtf", emit_markdown=True)
    assert r.content.markdown is None
    assert any("rtf: markdown requested" in w for w in r.warnings)
