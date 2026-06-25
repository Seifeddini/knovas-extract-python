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
    """Locate clients/extraction/spec/ in the KnowledgeBase monorepo.

    Resolution order:
    1. $KNOVAS_EXTRACT_SPEC_DIR (set this in CI to point at a checkout).
    2. ../KnowledgeBase/clients/extraction/spec/ (sibling-dir dev layout).
    3. ./tests/spec/ (submodule layout — production CI).
    4. Skip the test.
    """
    import os

    candidates: list[Path] = []
    if os.environ.get(SPEC_ENV):
        candidates.append(Path(os.environ[SPEC_ENV]))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent.parent / "KnowledgeBase" / "clients" / "extraction" / "spec")
    candidates.append(here / "spec")

    for c in candidates:
        if c.is_dir() and (c / "schema.json").is_file():
            return c

    pytest.skip(
        f"spec directory not found. Set ${SPEC_ENV} or run from a sibling-of-KnowledgeBase layout."
    )


@pytest.fixture(scope="session")
def schema(spec_dir: Path) -> dict:
    import json

    return json.loads((spec_dir / "schema.json").read_text(encoding="utf-8"))
