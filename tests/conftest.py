"""Test configuration shared by every tier (unit/golden/property/fuzz/bench).

Key invariant: no test may make a network call. Enforced via pytest-socket; the
addopts in pyproject.toml run `--disable-socket --allow-unix-socket` by default.
If you genuinely need a socket (you don't), mark with `@pytest.mark.allow_hosts`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Path to the cross-language spec — set via env var SPEC_DIR, or auto-detected
# when this repo lives as a sibling of KnowledgeBase (typical dev setup).
SPEC_ENV = "KNOVAS_EXTRACT_SPEC_DIR"


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    """Locate the knovas-extract-spec repo (or its checkout).

    Resolution order:
    1. $KNOVAS_EXTRACT_SPEC_DIR (set in CI to point at a checkout).
    2. ../knovas-extract-spec/ (sibling-dir dev layout — typical local setup).
    3. ./tests/spec/ (submodule layout — alternative CI layout).
    4. Skip the test.
    """
    import os

    candidates: list[Path] = []
    if os.environ.get(SPEC_ENV):
        candidates.append(Path(os.environ[SPEC_ENV]))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent.parent / "knovas-extract-spec")
    candidates.append(here / "spec")

    for c in candidates:
        if c.is_dir() and (c / "schema.json").is_file():
            return c

    pytest.skip(
        f"spec directory not found. Set ${SPEC_ENV} or clone knovas-extract-spec "
        f"as a sibling of this repo."
    )


@pytest.fixture(scope="session")
def schema(spec_dir: Path) -> dict:
    import json

    return json.loads((spec_dir / "schema.json").read_text(encoding="utf-8"))
