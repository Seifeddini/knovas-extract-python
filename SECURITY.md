# Security Policy — knovas-extract

`knovas-extract` is **parser code that runs on untrusted input**. Every supported format has a CVE history. Treat findings here with the seriousness that implies.

## Reporting a vulnerability

**Please do not file a public GitHub issue for security reports.** Use one of:

- **GitHub Private Vulnerability Reporting**: [Open a private advisory](https://github.com/knovas/knovas-extract-python/security/advisories/new). Preferred.
- **Email**: `security@knovas.ch`. PGP key fingerprint: `TODO — publish on first release`.

Include:

- Affected version(s) (output of `pip show knovas-extract`).
- Minimum reproducer (PoC file + 5-line script). Redact / synthesize if the real PoC contains sensitive data.
- Impact: RCE / DoS / info-disclosure / supply-chain.
- Your disclosure expectations (default: 90 days).

We acknowledge within **72 hours**, triage within **5 business days**, and target patches within:

| Severity (CVSS v3.1) | Patch SLA |
|---|---|
| Critical (9.0+) | 14 days |
| High (7.0–8.9) | 30 days |
| Medium (4.0–6.9) | 90 days |
| Low (< 4.0) | next minor release |

After patch and coordinated public disclosure, we file a CVE via GitHub Security Advisories, mirror it to `CHANGELOG-SECURITY.md`, and credit the reporter (unless declined).

## Security promises (enforced by CI)

1. **Never makes a network call.** `pytest-socket` globally disables `socket()` across every test; any extractor opening a connection fails CI.
2. **Never executes embedded code.** PDF JavaScript, DOCX/PPTX macros, RTF `\object` linking, HTML `<script>` — all stripped, warning emitted.
3. **Never writes outside an explicit tmpdir.** ZIP-based formats (DOCX, MSG, …) have a path-traversal guard; zip-slip paths raise `CorruptDocumentError`.
4. **XML is XXE-hardened.** All XML parsing goes through `defusedxml` with entity resolution disabled. Billion-laughs / external-entity payloads raise `ResourceExhaustedError`.
5. **Resource caps enforced.** Per-call `Limits` for input size, page count, decompression ratio, text size, recursion depth. Default values in `Limits()` are conservative.
6. **Typed errors only.** Every public entry point returns a valid `ExtractionResult` OR raises a subclass of `ExtractError`. Bare exceptions are a bug.
7. **No telemetry.** Zero outbound metrics. Logging is at INFO/WARNING and emits only counts/sizes/MIMEs — never document content.

## Supply-chain integrity

Every release:

- Built on GitHub Actions with `step-security/harden-runner` (egress allowlist).
- Wheels are **Sigstore-signed** via `sigstore-python` using OIDC; no long-lived signing keys exist.
- **SLSA L3 provenance** is attached as `provenance.intoto.jsonl`.
- **CycloneDX SBOM** (`sbom.cdx.json`) attached to the GitHub Release.
- Published to PyPI via **Trusted Publishers (OIDC)** — no PyPI API token in repo secrets.
- All transitive dependencies are **hash-pinned** in `requirements.lock`.

**Verify a release before installing in production**:

```bash
python -m sigstore verify identity \
  --cert-identity 'https://github.com/knovas/knovas-extract-python/.github/workflows/release.yml@refs/tags/v<VERSION>' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
  knovas_extract-<VERSION>-py3-none-any.whl

slsa-verifier verify-artifact knovas_extract-<VERSION>-py3-none-any.whl \
  --provenance-path knovas_extract-<VERSION>.intoto.jsonl \
  --source-uri github.com/knovas/knovas-extract-python \
  --source-tag v<VERSION>
```

See [`RELEASING.md`](RELEASING.md) for the full verification recipe.

## Sandboxing recommendation

For inputs you don't fully trust, run extraction inside a sandbox. Copy-paste recipes for `nsjail`, `bubblewrap`, and rootless Docker (no network, read-only fs, tmpfs `/tmp`, restrictive seccomp profile) in [`docs/sandboxing.md`](docs/sandboxing.md).

## Supported versions

| Version | Security fixes | Notes |
|---|---|---|
| 0.x (alpha) | only the latest 0.x release | rolling; SemVer post-1.0 |

After 1.0, we will commit to security patches on the last 2 minor releases for 12 months.

## Out of scope

The following are **NOT** considered security vulnerabilities:

- DoS via deliberately pathological inputs that fit within configured `Limits` (the cap *is* the contract).
- Extraction inaccuracy that doesn't constitute information disclosure (use `tools/diff_extraction.py` and file a bug, not a CVE).
- Issues in dependencies that are already fixed upstream — file the report there and we'll bump the pin.

## Hall of fame

(Empty — first finder gets recognized here.)
