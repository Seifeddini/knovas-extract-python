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
