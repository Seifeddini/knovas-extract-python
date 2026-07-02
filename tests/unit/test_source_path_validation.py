"""Positive assertions of the Source.path validation contract.

Every rejection class has a test. Every rejection message must be
content-free (log-safe).
"""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract._paths import validate_source_path
from knovas_extract.result import Limits


@pytest.mark.unit
def test_none_is_ok() -> None:
    assert validate_source_path(None, Limits()) is None


@pytest.mark.unit
def test_empty_is_ok() -> None:
    assert validate_source_path("", Limits()) == ""


@pytest.mark.unit
def test_normal_path_accepted() -> None:
    assert validate_source_path("/abs/normal/path.pdf", Limits()) == "/abs/normal/path.pdf"


@pytest.mark.unit
def test_relative_path_accepted() -> None:
    assert validate_source_path("../rel/dir/x.docx", Limits()) == "../rel/dir/x.docx"


@pytest.mark.unit
def test_tab_accepted() -> None:
    """Tab is intentionally the only ASCII control we allow."""
    assert validate_source_path("foo\tbar", Limits()) == "foo\tbar"


@pytest.mark.unit
def test_nul_byte_rejected() -> None:
    with pytest.raises(ValueError, match="NUL byte"):
        validate_source_path("foo\x00bar", Limits())


@pytest.mark.unit
@pytest.mark.parametrize("ch", ["\n", "\r", "\x1b", "\x08"])
def test_control_char_rejected(ch: str) -> None:
    with pytest.raises(ValueError, match="control character"):
        validate_source_path(f"pre{ch}post", Limits())


@pytest.mark.unit
@pytest.mark.parametrize(
    "ch",
    [
        "‪",  # LRE
        "‫",  # RLE
        "‬",  # PDF
        "‭",  # LRO
        "‮",  # RLO
        "⁦",  # LRI
        "⁧",  # RLI
        "⁨",  # FSI
        "⁩",  # PDI
    ],
)
def test_bidi_override_rejected(ch: str) -> None:
    """CVE-2021-42574 Trojan Source characters must be rejected."""
    with pytest.raises(ValueError, match="bidirectional-override"):
        validate_source_path(f"pre{ch}post", Limits())


@pytest.mark.unit
def test_over_max_length_rejected() -> None:
    limits = Limits()
    long = "a" * (limits.max_path_length + 1)
    with pytest.raises(ValueError, match="max_path_length"):
        validate_source_path(long, limits)


@pytest.mark.unit
def test_error_messages_are_content_free() -> None:
    """Rejection messages must not include the offending payload — log-safe."""
    payload = "SECRETSECRETSECRET\x00leak"
    with pytest.raises(ValueError) as exc:
        validate_source_path(payload, Limits())
    assert "SECRETSECRETSECRET" not in str(exc.value)
    assert "leak" not in str(exc.value)


@pytest.mark.unit
def test_extract_rejects_hostile_path() -> None:
    with pytest.raises(ValueError):
        extract(b"hi", mime="text/plain", path="foo\x00bar")


@pytest.mark.unit
def test_extract_stores_valid_path() -> None:
    r = extract(b"hi", mime="text/plain", path="/some/dir/x.txt")
    assert r.source.path == "/some/dir/x.txt"
