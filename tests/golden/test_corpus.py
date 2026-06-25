"""Golden corpus tests — the cross-language contract.

For every fixture in `clients/extraction/spec/corpus/<format>/`, run `extract`
and compare against the sibling `.expected.json` using the tolerances in
`clients/extraction/spec/MANIFEST.yaml::tolerances`. Failure here is either a
regression OR a deliberate spec change (which requires a separate spec PR).

This file is parametrized at collection time from the spec corpus, so adding
fixtures to the spec automatically extends the test matrix without code change.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from knovas_extract import extract

pytestmark = pytest.mark.golden


def _collect_fixtures(spec_dir: Path) -> list[tuple[Path, Path]]:
    """Walk corpus/<format>/ and pair every fixture with its .expected.json."""
    pairs: list[tuple[Path, Path]] = []
    corpus = spec_dir / "corpus"
    if not corpus.is_dir():
        return pairs
    for fmt_dir in sorted(corpus.iterdir()):
        if not fmt_dir.is_dir() or fmt_dir.name == "adversarial":
            continue
        for fixture in sorted(fmt_dir.iterdir()):
            if not fixture.is_file():
                continue
            if fixture.name in ("README.md", ".gitkeep"):
                continue
            if fixture.name.endswith(".expected.json"):
                continue
            # Try both naming conventions.
            expected = fixture.with_suffix(fixture.suffix + ".expected.json")
            if not expected.is_file():
                expected = fixture.with_suffix(".expected.json")
            if not expected.is_file():
                continue
            pairs.append((fixture, expected))
    return pairs


def _canon(s: str) -> str:
    """Replicate the canonicalizer here so the test doesn't depend on the impl."""
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s.strip()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Collect-time parametrization from the spec corpus.

    Runs in fixture-discovery order: `spec_dir` fixture must resolve first
    (via the conftest), so we read the corpus contents here.
    """
    if "fixture_pair" not in metafunc.fixturenames:
        return

    # Resolve spec_dir manually (we're outside fixture-evaluation scope).
    import os

    candidates: list[Path] = []
    if os.environ.get("KNOVAS_EXTRACT_SPEC_DIR"):
        candidates.append(Path(os.environ["KNOVAS_EXTRACT_SPEC_DIR"]))
    here = Path(__file__).resolve().parent
    candidates.append(here.parent.parent.parent / "knovas-extract-spec")
    candidates.append(here.parent / "spec")

    spec_dir = next((c for c in candidates if c.is_dir() and (c / "schema.json").is_file()), None)
    if spec_dir is None:
        metafunc.parametrize("fixture_pair", [], ids=["no-spec-found"])
        return

    pairs = _collect_fixtures(spec_dir)
    if not pairs:
        # Empty corpus — Phase 0 is fine, but mark it visibly.
        metafunc.parametrize("fixture_pair", [], ids=["corpus-empty"])
        return

    ids = [str(p[0].relative_to(spec_dir / "corpus")) for p in pairs]
    metafunc.parametrize("fixture_pair", pairs, ids=ids)


def test_fixture(fixture_pair: tuple[Path, Path]) -> None:
    fixture_path, expected_path = fixture_pair

    # macOS PDF skip: PyMuPDF's _extra C extension segfaults at import on
    # the macOS-latest GitHub runners (both Python 3.11 and 3.13). The crash
    # happens inside pymupdf/mupdf.py module load, before any of our code
    # runs. Upstream issue is independent of knovas-extract; track at
    # https://github.com/pymupdf/PyMuPDF/issues and pin a known-good version
    # when one ships. Until then, skip PDF on macOS rather than dropping
    # macOS coverage for the other 7 formats.
    import sys as _sys

    if _sys.platform == "darwin" and fixture_path.suffix.lower() == ".pdf":
        pytest.skip("PyMuPDF C-extension segfaults on macOS runners (upstream)")

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    try:
        actual = extract(fixture_path).to_dict()
    except __import__("knovas_extract").errors.DependencyMissingError as exc:
        # Format extra not installed in this hatch env — skip rather than
        # fail. The dedicated `golden` env has features=[pdf,docx,...] so
        # this only fires in the minimal `default` env.
        pytest.skip(f"Format extra unavailable in this env: {exc}")

    # Source: hash / size / mime exact match.
    for k in ("mime_type", "sha256", "size_bytes"):
        assert (
            actual["source"][k] == expected["source"][k]
        ), f"source.{k}: actual={actual['source'][k]!r} expected={expected['source'][k]!r}"

    # Text within tolerance (0.5% Levenshtein by default).
    a = _canon(actual["content"]["text"])
    e = _canon(expected["content"]["text"])
    if a != e:
        try:
            from rapidfuzz.distance import Levenshtein  # type: ignore[import-not-found]

            dist = Levenshtein.distance(a, e)
        except ImportError:
            # Fallback: require exact equality if we can't compute the distance.
            dist = max(len(a), len(e))
        pct = (dist / max(1, len(e))) * 100
        # MANIFEST.yaml::tolerances::text_levenshtein_pct
        TOL = 0.5
        assert pct <= TOL, (
            f"content.text drift {pct:.3f}% > tolerance {TOL}% "
            f"({dist} edits / {len(e)} chars)\n"
            f"actual[:200]={a[:200]!r}\nexpected[:200]={e[:200]!r}"
        )
