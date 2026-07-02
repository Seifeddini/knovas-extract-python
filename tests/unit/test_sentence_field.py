"""Unit tests for the Sentence field + Source.path.

Positive assertions of the consumer guarantees documented in
docs/citations.md and the plan's "Must-have consumer guarantees" section.
"""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract._version import SPEC_VERSION
from knovas_extract.result import ExtractionResult

pytest.importorskip("pysbd")


@pytest.mark.unit
def test_spec_version_is_at_least_12() -> None:
    assert SPEC_VERSION >= "1.2.0"


@pytest.mark.unit
def test_default_no_sentences_txt() -> None:
    r = extract(b"hello world.", mime="text/plain")
    assert r.content.sentences is None


@pytest.mark.unit
def test_default_no_sentences_html() -> None:
    r = extract(b"<p>Hello.</p>", mime="text/html")
    assert r.content.sentences is None


@pytest.mark.unit
def test_default_no_sentences_md() -> None:
    r = extract(b"# H\n\nBody.", mime="text/markdown")
    assert r.content.sentences is None


@pytest.mark.unit
def test_emit_sentences_txt_produces_records() -> None:
    r = extract(b"First. Second. Third.", mime="text/plain", emit_sentences=True)
    assert r.content.sentences is not None
    assert len(r.content.sentences) >= 3


@pytest.mark.unit
def test_empty_input_yields_empty_list_not_none() -> None:
    """`sentences == []` means opted-in-got-nothing; `None` means opted-out."""
    r = extract(b"", mime="text/plain", emit_sentences=True)
    assert r.content.sentences == []
    r2 = extract(b"", mime="text/plain")
    assert r2.content.sentences is None


@pytest.mark.unit
def test_round_trip_preserves_sentences() -> None:
    r = extract(b"First. Second.", mime="text/plain", emit_sentences=True)
    d = r.to_dict()
    rebuilt = ExtractionResult.from_dict(d)
    assert rebuilt.to_dict() == d


@pytest.mark.unit
def test_source_path_stored_verbatim() -> None:
    r = extract(b"hi", mime="text/plain", path="/some/rel/path.txt")
    assert r.source.path == "/some/rel/path.txt"


@pytest.mark.unit
def test_source_path_none_by_default() -> None:
    r = extract(b"hi", mime="text/plain")
    assert r.source.path is None
