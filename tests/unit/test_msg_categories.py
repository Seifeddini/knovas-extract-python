"""Outlook categories come from the named property Keywords (PS_PUBLIC_STRINGS)."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from knovas_extract.extractors.msg import _categories

pytestmark = [pytest.mark.unit]

PS_PUBLIC_STRINGS = "{00020329-0000-0000-C000-000000000046}"


class _Msg:
    """The part of extract-msg's ``Message`` that ``_categories`` reads."""

    def __init__(self, named: dict[tuple[str, str], Any]) -> None:
        self._named = named

    def getNamedProp(self, name: str, guid: str, default: Any = None) -> Any:
        return self._named.get((name, guid), default)


def test_categories_come_from_the_keywords_named_property() -> None:
    msg = _Msg({("Keywords", PS_PUBLIC_STRINGS): ["Mandat Muster AG", "Rechnung"]})
    assert _categories(msg) == ["Mandat Muster AG", "Rechnung"]


def test_an_attribute_wins_and_a_missing_property_is_none() -> None:
    class WithAttribute(_Msg):
        categories: ClassVar[list[str]] = ["A"]

    assert _categories(WithAttribute({})) == ["A"]
    assert _categories(_Msg({})) is None


def test_a_damaged_named_property_stream_never_fails_the_extraction() -> None:
    class Broken:
        def getNamedProp(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("damaged stream")

    assert _categories(Broken()) is None
    assert _categories(object()) is None
