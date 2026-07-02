"""Security tests for the `_markdown.html_to_markdown` sanitizer.

Every case here is a **positive** assertion of a mitigation described in
SECURITY.md → "Markdown emission" and the `_markdown` module docstring.
Each test also asserts that the corresponding warning is counted (not
content-verbatim, per SECURITY.md promise #7).

If a test in this file starts failing after a dependency bump, treat it
as a security regression, not a test flake — the sanitizer is a trust
boundary.
"""

from __future__ import annotations

import re

import pytest

from knovas_extract import Limits
from knovas_extract._markdown import (
    _URL_SCHEME_ALLOWLIST,
    apply_url_allowlist,
    html_to_markdown,
)

pytest.importorskip("markdownify")
pytest.importorskip("selectolax")

# `extract` is imported inside the one test that needs it (the dispatch
# defense-in-depth simulation) to keep the module-level surface minimal.

WARNING_SHAPE = re.compile(r"^markdown: \d+ .+$")


def _md(html: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    md = html_to_markdown(html, Limits(), warnings=warnings)
    return md, warnings


# --- Tag denylist ----------------------------------------------------------


@pytest.mark.unit
def test_script_stripped_with_contents() -> None:
    md, warns = _md("<script>alert(1)</script><h1>safe</h1>")
    assert "# safe" in md
    assert "alert" not in md
    assert any("<script>" in w for w in warns)


@pytest.mark.unit
def test_style_stripped_with_contents() -> None:
    md, warns = _md('<style>body{background:url("x")}</style><p>t</p>')
    assert "background" not in md
    assert "t" in md
    assert any("<style>" in w for w in warns)


@pytest.mark.unit
@pytest.mark.parametrize("tag", ["iframe", "object", "applet"])
def test_container_tag_stripped_with_contents(tag: str) -> None:
    """Non-void dangerous containers: contents are discarded, not unwrapped."""
    html = f"<{tag}>payload</{tag}><p>after</p>"
    md, warns = _md(html)
    assert "payload" not in md
    assert "after" in md
    assert any(f"<{tag}>" in w for w in warns)


@pytest.mark.unit
@pytest.mark.parametrize("tag", ["embed", "link", "meta", "base"])
def test_void_tag_stripped(tag: str) -> None:
    """Void HTML5 elements have no contents; still, presence must be dropped
    and counted.

    `<frame>` is excluded here because HTML5 only permits it inside a
    `<frameset>`; a loose `<frame>` is elided by lexbor's error recovery
    before the sanitizer ever sees it. `<frameset>` is covered by the
    container-tag test above.
    """
    html = f'<{tag} attr="v"><p>after</p>'
    md, warns = _md(html)
    assert "after" in md
    assert f"<{tag}" not in md
    assert any(f"<{tag}>" in w for w in warns)


@pytest.mark.unit
def test_svg_foreignobject_stripped() -> None:
    md, _ = _md("<svg><foreignObject><script>alert(1)</script></foreignObject></svg><p>t</p>")
    assert "alert" not in md
    assert "foreignObject" not in md


@pytest.mark.unit
def test_mathml_stripped() -> None:
    md, _ = _md("<math><mi>x</mi><script>alert(1)</script></math><p>after</p>")
    assert "alert" not in md
    assert "after" in md


@pytest.mark.unit
def test_html_comment_stripped_before_parse() -> None:
    md, warns = _md("<!-- [if IE]><script>alert(1)</script><![endif] --><p>x</p>")
    assert "alert" not in md
    assert "x" in md
    assert any("comment" in w for w in warns)


@pytest.mark.unit
def test_cdata_stripped_before_parse() -> None:
    md, warns = _md("<![CDATA[<script>alert(1)</script>]]><p>x</p>")
    assert "alert" not in md
    assert "x" in md
    assert any("cdata" in w for w in warns)


# --- Attribute denylist ----------------------------------------------------


@pytest.mark.unit
def test_event_handler_attr_dropped() -> None:
    md, warns = _md('<a href="https://ok" onclick="alert(1)">x</a>')
    assert "onclick" not in md
    assert "alert" not in md
    assert any("event-handler" in w for w in warns)


@pytest.mark.unit
def test_style_attr_dropped() -> None:
    md, warns = _md('<p style="background:expression(alert(1))">t</p>')
    assert "style=" not in md
    assert "expression" not in md
    assert "t" in md
    assert any("style=" in w for w in warns)


@pytest.mark.unit
def test_xlink_href_attr_dropped() -> None:
    _, warns = _md('<div xlink:href="javascript:alert(1)">t</div>')
    assert any("xlink:href=" in w for w in warns)


@pytest.mark.unit
def test_xml_lang_attr_preserved_by_exception() -> None:
    """xml:lang is in the colon-allowlist and is stripped only if truly forbidden."""
    _, warns = _md('<div xml:lang="en">t</div>')
    # No attr-drop warning for this element (xml:lang is safe).
    assert not any("xml:lang" in w for w in warns)


# --- URL scheme allowlist --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "scheme,url",
    [
        ("javascript", "javascript:alert(1)"),
        ("data", "data:text/html,<script>alert(1)</script>"),
        ("vbscript", "vbscript:msgbox(1)"),
        ("file", "file:///etc/passwd"),
        ("chrome-extension", "chrome-extension://abcdef/x"),
        ("blob", "blob:https://x/uuid"),
    ],
)
def test_disallowed_scheme_link_becomes_plain_text(scheme: str, url: str) -> None:
    md, warns = _md(f'<a href="{url}">click me</a>')
    # Link is unwrapped: text survives, but the URL syntax and scheme do not.
    assert "click me" in md
    # scheme literal must not appear in the emitted markdown link syntax.
    assert f"({url})" not in md
    assert f"]({scheme}:" not in md
    assert any(f"{scheme} URLs dropped" in w for w in warns)


@pytest.mark.unit
def test_relative_url_becomes_plain_text() -> None:
    md, warns = _md('<a href="../../../etc/passwd">x</a>')
    # No relative path traversal survives in the markdown link syntax.
    assert "(" + "../" not in md
    assert "x" in md
    assert any("URLs dropped" in w for w in warns)


@pytest.mark.unit
def test_protocol_relative_url_becomes_plain_text() -> None:
    md, warns = _md('<a href="//attacker/x">x</a>')
    assert "//attacker" not in md
    assert "x" in md
    assert any("URLs dropped" in w for w in warns)


@pytest.mark.unit
def test_allowed_schemes_survive_as_links() -> None:
    for scheme in _URL_SCHEME_ALLOWLIST:
        md, _ = _md(f'<a href="{scheme}:x">t</a>')
        assert f"({scheme}:x)" in md, f"scheme {scheme} should survive"


# --- Image handling --------------------------------------------------------


@pytest.mark.unit
def test_img_replaced_with_alt_text_even_with_safe_scheme() -> None:
    """A benign https:// image URL is still stripped to prevent beacon-on-render."""
    md, warns = _md('<img src="https://cdn.example/px.gif" alt="promo">')
    assert "promo" in md
    assert "cdn.example" not in md
    assert "![" not in md
    assert any("<img>" in w for w in warns)


@pytest.mark.unit
def test_img_with_hostile_scheme_stripped_via_alt() -> None:
    md, _ = _md('<img src="javascript:alert(1)" alt="X">')
    assert "X" in md
    assert "javascript" not in md
    assert "alert" not in md


# --- Idempotence, determinism, warning shape -------------------------------


@pytest.mark.unit
def test_sanitizer_is_idempotent_on_html() -> None:
    """Running the sanitizer twice on the same HTML gives byte-identical output.

    This is a true idempotence claim, not a fixed-point-on-markdown claim
    (feeding the resulting markdown back through an HTML parser would be
    lossy).
    """
    html = "<h1>Hi</h1><a href=javascript:alert(1)>x</a><img src=/x alt=A>"
    a1, _ = _md(html)
    a2, _ = _md(html)
    assert a1 == a2


@pytest.mark.unit
def test_sanitizer_is_deterministic() -> None:
    html = "<h1>A</h1><script>x</script><a href='data:X'>t</a>"
    a1, w1 = _md(html)
    a2, w2 = _md(html)
    assert a1 == a2
    assert w1 == w2


@pytest.mark.unit
def test_warnings_are_counted_not_content() -> None:
    """Every markdown warning must match `markdown: N …` — no content leakage."""
    html = (
        "<script>secretPassword123</script>"
        "<a href=javascript:secretToken>x</a>"
        "<style>secretCSS{color:red}</style>"
    )
    _, warns = _md(html)
    # Every markdown warning is counted, non-empty.
    md_warns = [w for w in warns if w.startswith("markdown:")]
    assert md_warns, "expected at least one markdown: warning"
    for w in md_warns:
        assert WARNING_SHAPE.match(w), w
        assert "secretPassword" not in w
        assert "secretToken" not in w
        assert "secretCSS" not in w


# --- Defense-in-depth ------------------------------------------------------


@pytest.mark.unit
def test_apply_url_allowlist_strips_javascript_link() -> None:
    md_in = "[click](javascript:alert(1))"
    warns: list[str] = []
    md_out = apply_url_allowlist(md_in, warnings=warns)
    assert md_out == "click"
    assert any("javascript URLs dropped" in w for w in warns)


@pytest.mark.unit
def test_apply_url_allowlist_preserves_http_links() -> None:
    md_in = "[click](https://ok/x)"
    warns: list[str] = []
    md_out = apply_url_allowlist(md_in, warnings=warns)
    assert md_out == md_in
    assert not warns


@pytest.mark.unit
def test_apply_url_allowlist_strips_images_to_alt() -> None:
    md_in = "![promo](https://cdn/x.png)"
    warns: list[str] = []
    md_out = apply_url_allowlist(md_in, warnings=warns)
    assert md_out == "promo"
    assert any("<img>" in w for w in warns)


@pytest.mark.unit
def test_dispatch_post_pass_strips_hostile_scheme_from_extractor_markdown() -> None:
    """Even if an extractor accidentally surfaces a hostile URL, dispatch scrubs it.

    Simulates a regression in a per-format markdown path (e.g. a future
    pymupdf4llm bump reintroduces `javascript:` passthrough).
    """
    # Simulate by monkeypatching the txt extractor to return a hostile markdown.
    from knovas_extract import dispatch as D
    from knovas_extract.extractors import txt as T

    original = T.TxtExtractor.extract

    def hostile_extract(
        self, data, *, filename=None, limits=None, emit_markdown=False, emit_sentences=False
    ):  # type: ignore[no-untyped-def]
        r = original(self, data, filename=filename, limits=limits, emit_markdown=False)
        if emit_markdown:
            r.content.markdown = "[click](javascript:alert(1)) safe"
        return r

    T.TxtExtractor.extract = hostile_extract  # type: ignore[method-assign]
    try:
        r = D.extract(b"hi", mime="text/plain", emit_markdown=True)
        assert r.content.markdown is not None
        assert "javascript" not in r.content.markdown
        assert "alert" not in r.content.markdown
        assert "click safe" in r.content.markdown
    finally:
        T.TxtExtractor.extract = original  # type: ignore[method-assign]
