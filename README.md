# knovas-extract

[![CI](https://github.com/knovas/knovas-extract-python/actions/workflows/ci.yml/badge.svg)](https://github.com/knovas/knovas-extract-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/knovas-extract.svg)](https://pypi.org/project/knovas-extract/)
[![Python](https://img.shields.io/pypi/pyversions/knovas-extract.svg)](https://pypi.org/project/knovas-extract/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Privacy-preserving, performant document extraction (text + metadata) for the [Knovas Semantix](https://knovas.ch/) platform. **Python reference implementation** of the cross-language `knovas-extract` spec.

> **Status: alpha (0.1.0.dev).** API stable in shape, not yet in version. Not yet on PyPI.

## What it does

Give it a document (PDF, DOCX, MSG, EML, HTML, RTF, MD, TXT). Get back a `ExtractionResult`: extracted text, document metadata (title, author, language, dates, page count), per-page text (when paginated), and heading-derived sections.

That's the whole library. It does **not** chunk, embed, upload to Semantix, or talk to any network. Those concerns belong in your application code.

## Why

- **Spec-first**: the [output contract](https://github.com/knovas/KnowledgeBase/tree/develop/clients/extraction/spec) is shared across every language implementation in the `knovas-extract-*` family. Your Python output and a future Node/Go/Rust output are guaranteed equivalent (within documented tolerances).
- **Fast**: uses the best native library per format (PyMuPDF for PDF, selectolax for HTML, …). PDF throughput ≈ 20–100 pages/sec on a single thread; lazy-imports keep cold start ≈ 50 ms.
- **Safe by default**: no network calls, no embedded-code execution, no path traversal, ZIP-bomb caps, XXE-hardened XML, typed errors only. See [`SECURITY.md`](SECURITY.md) for the full posture.

## Install

```bash
pip install knovas-extract                       # core only (TXT/MD/HTML/EML)
pip install 'knovas-extract[pdf]'                # + PyMuPDF (AGPL — read NOTICE)
pip install 'knovas-extract[docx,msg,rtf]'       # + DOCX/MSG/RTF
pip install 'knovas-extract[all]'                # everything
```

License-sensitive embedders: install `knovas-extract[minimal]` for the permissive-only subset (no AGPL / GPL deps). Calling `extract()` on a format that needs an unavailable backend raises `DependencyMissingError` with the exact `pip install` command to fix it.

## Quickstart

```python
from knovas_extract import extract

result = extract("report.pdf")

print(result.content.text)            # full extracted text, canonicalized
print(result.metadata.title)          # e.g. "Q4 Earnings"
print(result.metadata.page_count)     # e.g. 12
for page in result.content.pages:     # per-page text (when paginated)
    print(page.index, page.text[:80])
print(result.warnings)                # e.g. ["page 7: unrecognized font"]
```

Bytes work too:

```python
data = open("report.pdf", "rb").read()
result = extract(data, mime="application/pdf")
```

`ExtractionResult.to_dict()` round-trips through the spec's JSON Schema; pass it directly to anything expecting the contract shape.

## Resource limits

Every extraction is bounded. Override the defaults per call:

```python
from knovas_extract import extract, Limits

result = extract(
    "huge.docx",
    limits=Limits(
        max_input_bytes=50 * 1024 * 1024,    # 50 MiB cap
        max_pages=1_000,
        max_decompression_ratio=50,
        max_text_bytes=10 * 1024 * 1024,
    ),
)
```

When a limit is crossed, you get a `ResourceExhaustedError` with `.what` / `.limit` / `.observed` attributes. Defaults (in `Limits()`) are conservative; tune them with your throughput budget in mind.

## Errors

Every call either returns an `ExtractionResult` or raises a subclass of `ExtractError`:

| Exception | When |
|---|---|
| `UnsupportedFormatError` | MIME not registered (you can register a custom extractor via `knovas_extract.dispatch.MIME_REGISTRY`). |
| `CorruptDocumentError` | Bytes couldn't be parsed as the claimed format. |
| `EncryptedDocumentError` | Password-protected document, no password supplied. |
| `ResourceExhaustedError` | A `Limits` threshold was crossed. |
| `DependencyMissingError` | An optional extra isn't installed — exception tells you the exact install command. |

No bare exceptions, no `None`, no `Optional[ExtractionResult]`.

## Security promises (enforced by CI)

- **Never makes a network call.** Asserted across every test via `pytest-socket`.
- **Never executes embedded code.** PDF JavaScript, DOCX macros, RTF object linking — all stripped, warning emitted.
- **Never writes outside an explicit tmpdir.** ZIP-slip paths are rejected.
- **XML parsing is XXE-hardened.** All XML goes through `defusedxml`.
- Releases are **Sigstore-signed** + ship **SLSA L3 provenance**. Verify before installing in production — see [`RELEASING.md`](RELEASING.md).

For untrusted inputs, run inside a sandbox. Copy-paste recipes for `nsjail`, `bubblewrap`, and rootless Docker in [`docs/sandboxing.md`](docs/sandboxing.md).

## Spec conformance

This implementation conforms to `spec_version = 1.0.0` of [`knovas/KnowledgeBase/clients/extraction/spec`](https://github.com/knovas/KnowledgeBase/tree/develop/clients/extraction/spec). The pinned spec sha is recorded in `tests/spec/` (Git submodule). Every release runs the spec's golden corpus + adversarial corpus before tagging.

To run the golden tests locally against a sibling KnowledgeBase checkout:

```bash
export KNOVAS_EXTRACT_SPEC_DIR=/path/to/KnowledgeBase/clients/extraction/spec
hatch -e golden run run
```

## Development

```bash
hatch env create                  # one-time
hatch run test                    # unit tests
hatch -e golden run run           # corpus contract tests
hatch -e property run run         # hypothesis robustness
hatch -e bench run run            # benchmarks
hatch -e lint run all             # ruff + mypy + pyright
hatch -e sec run all              # bandit + pip-audit
```

CI runs the full matrix (3 Pythons × 3 OSes × every gate) on every PR.

## Reporting vulnerabilities

See [`SECURITY.md`](SECURITY.md). Please **do not** open public issues for security reports.

## License

Apache-2.0 for `knovas-extract` itself. Several optional extras pull AGPL / GPL libraries — see [`NOTICE`](NOTICE) for the full third-party license inventory.
