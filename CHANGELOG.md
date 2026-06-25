# Changelog — knovas-extract (Python)

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); SemVer.

A **major** version bump matches the major of `spec_version` it conforms to.

## [0.1.1] — 2026-06-25

### Fixed
- `release.yml`: the v0.1.0 tag fired the release workflow but the Sign job
  failed because `actions/upload-artifact@v4` with a multi-path config
  nested `dist/` inside itself (`dist/dist/*.whl`). Refactored to upload
  `dist/` as its own artifact (downloads land at `dist/*`) and the SBOM
  as a separate `sbom` artifact. Bumped `sigstore/gh-action-sigstore-python`
  to v3.4.0. Same fix unblocks the Publish-to-PyPI step. No code changes
  vs 0.1.0 — wheel contents are identical except for the version string.

## [0.1.0] — 2026-06-25

First public release. Conforms to `spec_version = 1.0.0`.

### Supported formats

PDF (PyMuPDF), DOCX (python-docx + mammoth), TXT, MD, HTML (selectolax),
RTF (striprtf), EML (stdlib email), MSG (extract-msg).

### Security gates (enforced by CI on every commit to `main`)

- `pytest-socket`: no extractor may make a network call. Ever.
- `bandit`, `pip-audit`, `osv-scanner`, `CodeQL`, `gitleaks`: zero
  high/critical findings to merge.
- Adversarial corpus (in [`knovas/KnowledgeBase`](https://github.com/Seifeddini/KnowledgeBase/tree/develop/clients/extraction/spec) at the pinned spec version):
  encrypted PDF → `EncryptedDocumentError`; decompression-bomb DOCX →
  `ResourceExhaustedError`; zip-slip DOCX → `CorruptDocumentError`;
  billion-laughs HTML → sanitized (selectolax ignores XML entities by
  construction); RTF `\object\objemb` → warning emitted, payload bytes
  never touched.
- Hypothesis property tests on every extractor: random bytes may raise an
  `ExtractError` subclass OR return a valid `ExtractionResult`, but never
  let through a bare `Exception` / `UnicodeDecodeError` / `RuntimeError`.

### Supply-chain

- Built reproducibly on GitHub Actions with `step-security/harden-runner`
  (egress allowlist + audit).
- Wheels are **Sigstore-signed** via OIDC; no long-lived signing keys.
- **SLSA Level 3 build provenance** attached as `provenance.intoto.jsonl`.
- **CycloneDX SBOM** (`sbom.cdx.json`) attached to every release.
- Published to PyPI via **Trusted Publishers (OIDC)** — no API tokens in
  repo secrets.

Verify a release with:
```bash
python -m sigstore verify identity \
  --cert-identity 'https://github.com/Seifeddini/knovas-extract-python/.github/workflows/release.yml@refs/tags/v0.1.0' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
  knovas_extract-0.1.0-py3-none-any.whl
```

### Performance (synthetic, single thread, Python 3.13 / Win11)

| Format | Throughput |
|---|---|
| TXT 1 MiB | ~700 MB/s (canonicalizer-bound) |
| PDF 100 pages | ~120 pages/sec |
| DOCX 500 paragraphs | ~80 docs/sec |
| HTML 2000 paragraphs | ~25 docs/sec |
| EML 2000 body lines | ~40 docs/sec |

(Reproduce with `hatch -e bench run run`.)

### Public API

```python
from knovas_extract import (
    extract,                       # path or bytes -> ExtractionResult
    ExtractionResult, Limits,
    ExtractError,                  # base
    UnsupportedFormatError,
    CorruptDocumentError,
    EncryptedDocumentError,
    ResourceExhaustedError,
    DependencyMissingError,
)
```

Stable in shape from 0.1.0. Breaking changes require a major version bump
AND a corresponding `spec_version` major bump.

[0.1.0]: https://github.com/Seifeddini/knovas-extract-python/releases/tag/v0.1.0
