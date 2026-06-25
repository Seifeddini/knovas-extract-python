"""Unit tests for the text canonicalizer.

These are the cross-language equality rules; bugs here would silently break
every golden test. Worth deep coverage.
"""

from __future__ import annotations

import pytest

from knovas_extract.normalize import canonicalize_text, word_count


@pytest.mark.unit
class TestCanonicalize:
    def test_empty(self) -> None:
        assert canonicalize_text("") == ""

    def test_idempotent(self) -> None:
        s = "Hello world\n\nSecond paragraph\n"
        assert canonicalize_text(canonicalize_text(s)) == canonicalize_text(s)

    def test_crlf_to_lf(self) -> None:
        assert canonicalize_text("a\r\nb\r\nc") == "a\nb\nc"

    def test_cr_to_lf(self) -> None:
        assert canonicalize_text("a\rb\rc") == "a\nb\nc"

    def test_trailing_whitespace_per_line(self) -> None:
        assert canonicalize_text("alpha   \nbeta\t\n") == "alpha\nbeta"

    def test_collapse_multi_blank_lines(self) -> None:
        assert canonicalize_text("para1\n\n\n\n\npara2") == "para1\n\npara2"

    def test_strip_leading_trailing_whitespace(self) -> None:
        assert canonicalize_text("\n\n  hello  \n\n") == "hello"

    def test_nfc_normalization(self) -> None:
        # 'café' as NFD (e + combining-acute) → NFC (single é codepoint).
        nfd = "café"
        nfc = "café"
        assert canonicalize_text(nfd) == nfc

    def test_preserves_internal_single_blank(self) -> None:
        s = "p1\n\np2"
        assert canonicalize_text(s) == s


@pytest.mark.unit
class TestWordCount:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", 0),
            ("hello", 1),
            ("hello world", 2),
            ("hello   world\nfoo\tbar", 4),
            ("   \n\t  ", 0),
        ],
    )
    def test_counts(self, text: str, expected: int) -> None:
        assert word_count(text) == expected
