"""Missing-dependency behavior on `emit_markdown=True` paths.

When markdownify / pymupdf4llm / selectolax aren't installed, the
`emit_markdown=True` path must raise `DependencyMissingError` with the
correct extras name — never leak an ImportError, never silently return
`markdown=None`.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from knovas_extract import extract
from knovas_extract.errors import DependencyMissingError

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def hide_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[callable]:  # type: ignore[type-arg]
    """Return a callable that removes a module from `sys.modules` and blocks re-import."""

    def _hide(name: str) -> None:
        # Evict any cached copy.
        for k in list(sys.modules):
            if k == name or k.startswith(name + "."):
                monkeypatch.delitem(sys.modules, k, raising=False)
        # Also nuke the `_markdown` helper module so its lazy-imports re-run.
        for k in list(sys.modules):
            if k == "knovas_extract._markdown":
                monkeypatch.delitem(sys.modules, k, raising=False)

        real_find = __import__

        def _blocked(mod: str, *a: object, **kw: object) -> object:
            if mod == name or mod.startswith(name + "."):
                raise ImportError(name=name)
            return real_find(mod, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr("builtins.__import__", _blocked)

    yield _hide


@pytest.mark.unit
def test_missing_markdownify_raises_dependency_missing(hide_module) -> None:  # type: ignore[no-untyped-def]
    # Needs selectolax present so the HTML text path succeeds and the flow
    # reaches the markdownify import; skip when the `html` extra is absent
    # (the sibling test below covers the selectolax-missing case).
    pytest.importorskip("selectolax")
    hide_module("markdownify")
    with pytest.raises(DependencyMissingError) as excinfo:
        extract(
            b"<html><body><h1>hi</h1></body></html>",
            mime="text/html",
            emit_markdown=True,
        )
    assert excinfo.value.extra == "markdown"
    assert excinfo.value.missing_package == "markdownify"
    assert "knovas-extract[markdown]" in str(excinfo.value)


@pytest.mark.unit
def test_missing_selectolax_in_markdown_helper_raises(hide_module) -> None:  # type: ignore[no-untyped-def]
    # The markdown helper needs selectolax before markdownify — first
    # import failure wins. Extractors like HTML also need selectolax for
    # the text path; the deps map must report the helper's expectation.
    hide_module("selectolax")
    with pytest.raises(DependencyMissingError) as excinfo:
        extract(
            b"<html><body><p>x</p></body></html>",
            mime="text/html",
            emit_markdown=True,
        )
    # selectolax's absence maps to either the `html` extra (text path) or
    # `markdown` extra (helper path). Both are acceptable — the point is
    # the error is typed and points at a valid pip-installable extra.
    assert excinfo.value.extra in {"html", "markdown"}
