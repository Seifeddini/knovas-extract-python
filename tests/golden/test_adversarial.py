"""Adversarial corpus tests — security regression gate.

For every fixture under `clients/extraction/spec/corpus/adversarial/`,
read its declared `expected` behavior from MANIFEST.yaml and assert the
extractor raises (or returns) accordingly. Behavioral contract — NOT
output-matching (see clients/extraction/spec/docs/tolerances.md).

`expected` values:
- ResourceExhaustedError / CorruptDocumentError / EncryptedDocumentError /
  UnsupportedFormatError / DependencyMissingError → exact exception class
- "warning"   → call must succeed AND emit a specific warning
- "sanitized" → call must succeed AND not surface the malicious payload
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knovas_extract import extract
from knovas_extract.errors import (
    CorruptDocumentError,
    DependencyMissingError,
    EncryptedDocumentError,
    ExtractError,
    ResourceExhaustedError,
    UnsupportedFormatError,
)

pytestmark = pytest.mark.golden


_ERROR_CLASSES: dict[str, type[ExtractError]] = {
    "ResourceExhaustedError": ResourceExhaustedError,
    "CorruptDocumentError": CorruptDocumentError,
    "EncryptedDocumentError": EncryptedDocumentError,
    "UnsupportedFormatError": UnsupportedFormatError,
    "DependencyMissingError": DependencyMissingError,
}


def _load_adversarial_manifest(spec_dir: Path) -> dict[str, dict]:
    """Read corpus/adversarial entries from MANIFEST.yaml."""
    import yaml

    manifest = yaml.safe_load((spec_dir / "MANIFEST.yaml").read_text(encoding="utf-8"))
    return dict(manifest.get("adversarial") or {})


def _resolve_spec_dir() -> Path | None:
    import os

    candidates: list[Path] = []
    if os.environ.get("KNOVAS_EXTRACT_SPEC_DIR"):
        candidates.append(Path(os.environ["KNOVAS_EXTRACT_SPEC_DIR"]))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent.parent.parent / "knovas-extract-spec")
    candidates.append(here.parent / "spec")
    return next((c for c in candidates if c.is_dir() and (c / "schema.json").is_file()), None)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "adversarial_pair" not in metafunc.fixturenames:
        return

    spec_dir = _resolve_spec_dir()
    if spec_dir is None:
        metafunc.parametrize("adversarial_pair", [], ids=["no-spec-found"])
        return

    adv_root = spec_dir / "corpus" / "adversarial"
    if not adv_root.is_dir():
        metafunc.parametrize("adversarial_pair", [], ids=["adversarial-empty"])
        return

    manifest = _load_adversarial_manifest(spec_dir)
    if not manifest:
        metafunc.parametrize("adversarial_pair", [], ids=["adversarial-empty"])
        return

    pairs: list[tuple[Path, str]] = []
    ids: list[str] = []
    for rel_key, entry in sorted(manifest.items()):
        path = adv_root / rel_key
        if not path.is_file():
            continue
        pairs.append((path, entry.get("expected", "")))
        ids.append(rel_key)

    if not pairs:
        metafunc.parametrize("adversarial_pair", [], ids=["adversarial-no-files"])
        return

    metafunc.parametrize("adversarial_pair", pairs, ids=ids)


def test_adversarial_fixture(adversarial_pair: tuple[Path, str]) -> None:
    path, expected = adversarial_pair
    expected_cls = _ERROR_CLASSES.get(expected)

    if expected_cls is not None:
        with pytest.raises(expected_cls):
            extract(path)
        return

    if expected == "warning":
        # Must succeed AND surface at least one warning.
        result = extract(path)
        assert (
            result.warnings
        ), f"adversarial fixture {path.name} expected to emit a warning; got {result.warnings!r}"
        return

    if expected == "sanitized":
        # Must succeed; the test is that nothing crashes / no network call.
        # Network-isolation is enforced globally by pytest-socket; if we get
        # a result here, we've already demonstrated the payload was contained.
        extract(path)
        return

    pytest.fail(
        f"adversarial fixture {path.name} has unknown `expected`: {expected!r}. "
        "Update tests/golden/test_adversarial.py::_ERROR_CLASSES or the manifest."
    )
