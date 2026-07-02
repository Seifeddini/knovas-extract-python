"""End-to-end adversarial smoke tests over the fixtures corpus.

Each file under `tests/fixtures/adversarial/markdown/` is run through
`extract(..., emit_markdown=True)` and asserted to:

- either raise ResourceExhaustedError (limit-tripping fixtures like
  `deep_nesting.html`),
- or produce a `content.markdown` string with no denylist literal
  and no disallowed URL scheme surviving,
- and always emit at least one aggregated `markdown: N …` warning.

The fixtures themselves live in `tests/fixtures/adversarial/markdown/`.
Add new hostile inputs there when patching a class of finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knovas_extract import Limits, extract
from knovas_extract.errors import ResourceExhaustedError

pytest.importorskip("markdownify")
pytest.importorskip("selectolax")

CORPUS = Path(__file__).resolve().parent.parent / "fixtures" / "adversarial" / "markdown"

DENYLIST = (
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<style",
    "<applet",
    "javascript:",
    "data:text/html",
    "vbscript:",
    "file://",
    "chrome-extension:",
    "expression(",
)


def _fixtures() -> list[Path]:
    if not CORPUS.is_dir():
        return []
    return sorted(CORPUS.glob("*.html"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "path", _fixtures(), ids=lambda p: p.name if isinstance(p, Path) else str(p)
)
def test_hostile_fixture_sanitized_or_refused(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"no corpus at {path}")
    data = path.read_bytes()
    try:
        r = extract(
            data,
            mime="text/html",
            emit_markdown=True,
            # Relax the ratio so we're testing containment, not
            # expansion (that has its own tests). Depth cap left at
            # default to catch `deep_nesting.html`.
            limits=Limits(max_markdown_expansion_ratio=1000.0),
        )
    except ResourceExhaustedError:
        return  # limit-tripping fixture — the guard did its job

    assert r.content.markdown is not None, f"markdown was None for {path.name}"
    md = r.content.markdown
    for literal in DENYLIST:
        assert literal not in md, f"{path.name}: {literal!r} survived: {md!r}"
    # Every adversarial fixture should trip at least one sanitizer warning;
    # otherwise the fixture isn't testing anything.
    assert any(
        w.startswith("markdown:") for w in r.warnings
    ), f"{path.name}: no markdown warnings emitted; fixture may be inert"
