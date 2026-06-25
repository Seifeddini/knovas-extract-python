"""Unit tests for the MSG extractor.

Limited coverage: synthesizing a valid Outlook .msg programmatically requires
a CFB-writer library we don't ship. The corruption path + dispatch
registration are still verifiable here; real-fixture coverage lands when we
import an OSS .msg sample (Apache Tika test corpus, etc.) in a follow-up.
"""

from __future__ import annotations

import pytest

from knovas_extract import extract
from knovas_extract.errors import CorruptDocumentError

pytest.importorskip("extract_msg")

MSG_MIME = "application/vnd.ms-outlook"


@pytest.mark.unit
def test_non_msg_input_raises_corrupt() -> None:
    with pytest.raises(CorruptDocumentError):
        extract(b"not a real .msg", mime=MSG_MIME)


@pytest.mark.unit
def test_empty_input_raises_corrupt() -> None:
    with pytest.raises(CorruptDocumentError):
        extract(b"", mime=MSG_MIME)


@pytest.mark.unit
def test_mime_registered() -> None:
    """Ensure dispatch routes the MSG MIME to the extractor."""
    from knovas_extract.dispatch import MIME_REGISTRY, _get_extractor

    extractor = _get_extractor(MSG_MIME)
    assert MIME_REGISTRY.get(MSG_MIME) is extractor
