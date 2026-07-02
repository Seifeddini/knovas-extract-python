"""Network isolation — extraction must never make a network call.

The global pytest-socket disable in pyproject.toml makes any socket() call fail
the test. This file confirms the gate is actually wired up and that a known
extraction path doesn't try to phone home.

If you ever see a `SocketBlockedError` here, that's the point — fix the extractor.
"""

from __future__ import annotations

import pytest

from knovas_extract import extract

pytestmark = pytest.mark.property


@pytest.mark.unit
def test_socket_is_disabled_in_tests() -> None:
    """Sanity: confirm pytest-socket actually blocks socket().

    pytest-socket signals via a UserWarning (subclass varies across versions);
    pyproject.toml's `filterwarnings = "error"` turns it into a hard failure
    at the call site. We accept any of the documented signals.
    """
    import socket

    with pytest.raises((Warning, OSError)):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("example.com", 80))


@pytest.mark.unit
def test_txt_extraction_makes_no_network_calls() -> None:
    # If txt.py ever grew a network call, pytest-socket would raise here.
    r = extract(b"hello", mime="text/plain")
    assert r.content.text == "hello"


@pytest.mark.unit
def test_md_extraction_makes_no_network_calls() -> None:
    src = b"---\ntitle: T\n---\n\n# H\n\nBody."
    r = extract(src, mime="text/markdown")
    assert r.content.text == "# H\n\nBody."


# --- emit_markdown=True paths must not open sockets -----------------------


@pytest.mark.unit
def test_html_emit_markdown_makes_no_network_calls() -> None:
    """selectolax + markdownify do zero I/O — sanitizing HTML with remote
    image / link references must not attempt to fetch anything."""
    pytest.importorskip("markdownify")
    pytest.importorskip("selectolax")
    from knovas_extract import Limits

    hostile = (
        b"<html><body><p>Some real body text that gives us a plausible plain-text baseline "
        b"so the markdown expansion ratio guard does not fire on this smoke test.</p>"
        b'<img src="https://attacker.example/beacon.png" alt="X">'
        b'<link rel="stylesheet" href="https://attacker.example/x.css">'
        b'<a href="https://attacker.example/">click</a>'
        b"</body></html>"
    )
    r = extract(
        hostile,
        mime="text/html",
        emit_markdown=True,
        # Widen the ratio to prevent this test from double-signalling on
        # a limit it isn't intended to exercise.
        limits=Limits(max_markdown_expansion_ratio=100.0),
    )
    # No network happened (pytest-socket would have raised); the image URL
    # is stripped, the <link> is denylisted, but click-through survives
    # because https:// is allowlisted.
    assert r.content.markdown is not None
    assert "attacker.example/beacon" not in r.content.markdown
    assert "attacker.example/x.css" not in r.content.markdown
    assert "attacker.example" in r.content.markdown  # the <a href> URL is kept


@pytest.mark.unit
def test_eml_emit_markdown_makes_no_network_calls() -> None:
    """Multipart email with remote HTML content must not phone home."""
    pytest.importorskip("markdownify")
    pytest.importorskip("selectolax")
    eml = (
        b"From: s@x\r\nTo: r@x\r\nSubject: T\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nfallback\r\n"
        b"--B\r\nContent-Type: text/html\r\n\r\n"
        b'<img src="https://attacker.example/beacon.png" alt="hi">'
        b"--B--\r\n"
    )
    r = extract(eml, mime="message/rfc822", emit_markdown=True)
    assert r.content.markdown is not None
    assert "attacker.example" not in r.content.markdown
