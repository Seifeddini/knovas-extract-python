# Releasing — knovas-extract

Maintainer-facing checklist for cutting a release. End-users do **not** need to read this; for them, [`SECURITY.md`](SECURITY.md) explains how to verify a release they already downloaded.

## Pre-flight (every release)

- [ ] All quality gates green on `main`: lint, type, unit, golden, property, bandit, semgrep, pip-audit, CodeQL.
- [ ] Adversarial corpus 100% pass.
- [ ] Mutation score ≥ 80% on trust-boundary modules (`dispatch`, `normalize`, `errors`, `result`).
- [ ] Bench regression ≤ 10% vs the prior release (compare `.benchmarks/result.json`).
- [ ] CHANGELOG.md updated with the new version and date.
- [ ] `pyproject.toml::version` bumped (SemVer).
- [ ] `spec_version` pin in `tests/spec/` matches the version in `__init__.py::SPEC_VERSION`.
- [ ] NOTICE regenerated from the SBOM (`hatch run sec:sbom` → diff the third-party block).

## Cut

```bash
git checkout main
git pull
git tag -s v<VERSION> -m "Release v<VERSION>"
git push origin v<VERSION>
```

The `release.yml` workflow takes over:

1. Runs the full CI matrix one more time.
2. Builds wheels reproducibly (`hatch build --reproducible`).
3. Generates SLSA L3 provenance attestation.
4. Generates CycloneDX SBOM (`sbom.cdx.json`).
5. Signs every wheel with `sigstore-python` via OIDC.
6. Publishes to PyPI via Trusted Publishers (no API token).
7. Creates a GitHub Release with wheels + `.intoto.jsonl` + `sbom.cdx.json` attached.

## Verify the release (run on a clean machine)

```bash
mkdir /tmp/verify && cd /tmp/verify
pip download --no-deps knovas-extract==<VERSION> -d .
WHEEL=$(ls knovas_extract-*.whl)

# 1. Sigstore signature.
python -m sigstore verify identity \
  --cert-identity "https://github.com/knovas/knovas-extract-python/.github/workflows/release.yml@refs/tags/v<VERSION>" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$WHEEL"

# 2. SLSA L3 provenance.
curl -sLO "https://github.com/knovas/knovas-extract-python/releases/download/v<VERSION>/$WHEEL.intoto.jsonl"
slsa-verifier verify-artifact "$WHEEL" \
  --provenance-path "$WHEEL.intoto.jsonl" \
  --source-uri github.com/knovas/knovas-extract-python \
  --source-tag "v<VERSION>"

# 3. SBOM exists and matches.
curl -sLO "https://github.com/knovas/knovas-extract-python/releases/download/v<VERSION>/sbom.cdx.json"
jq -r '.metadata.component.version' sbom.cdx.json
# Expect: <VERSION>

# 4. Smoke install + extract.
pip install "$WHEEL"
python -c "from knovas_extract import extract; r = extract('/etc/hostname'); print(r.content.text)"
```

All four steps must succeed before announcing the release.

## Reproducible-build verification (optional)

`hatch build --reproducible` sets `SOURCE_DATE_EPOCH` from the latest commit timestamp. A second build from the same commit on a clean machine should produce byte-identical wheels:

```bash
git checkout v<VERSION>
SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct) hatch build
sha256sum dist/*.whl
# Compare against the SHA-256 recorded in the GH Release.
```

## Hotfix process

Same pre-flight + cut, but bump only the patch version (`X.Y.Z+1`). Cherry-pick the fix commit onto `main` from a `hotfix/<topic>` branch and tag immediately. The Sigstore identity is the same workflow file → existing verification recipes still work.

## Rotating the GPG key in SECURITY.md

When the `security@knovas.ch` PGP key rotates:

- Update `SECURITY.md::PGP key fingerprint`.
- Publish the new key to keys.openpgp.org.
- Open an advisory at GitHub Security noting the rotation.
- Old key remains valid for verifying past disclosures for 12 months, then revoked.
