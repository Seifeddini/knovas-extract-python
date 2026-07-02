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
pip install 'knovas-extract[markdown]'           # + emit_markdown=True (sanitized MD)
pip install 'knovas-extract[sentences]'          # + emit_sentences=True (pysbd, MIT)
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

### Markdown output (opt-in, sanitized)

Pass `emit_markdown=True` to populate `content.markdown` with a whole-document Markdown rendering. Requires the `[markdown]` extra (adds `markdownify`); PDFs additionally require `pymupdf4llm`, which ships in the `[pdf]` extra.

```python
result = extract("report.docx", emit_markdown=True)
print(result.content.markdown)   # ATX headings, - bullets, **bold**, tables
```

Emitted Markdown is **sanitized before conversion**: `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<applet>`, `<svg>`, `<math>`, HTML comments, and event-handler attributes are stripped with their contents; `<a href>` URLs are gated by an allowlist of `http`, `https`, `mailto`, `tel`; `<img>` is emitted as alt-text only so downstream renderers cannot beacon on render. See [SECURITY.md → Markdown emission](SECURITY.md) for the full contract. RTF, and formats without recoverable structure, leave `content.markdown = None` with a warning explaining why.

### Sentences with line + page + section citations (opt-in)

Pass `emit_sentences=True` to populate `content.sentences` with a
deterministic tokenization. Every `Sentence` carries an exact char
slice, a 1-based line window (into `content.text`), an optional page
number, and a back-pointer to the enclosing `Section`. Requires the
`[sentences]` extra (adds `pysbd`, MIT).

```python
result = extract("report.pdf", emit_sentences=True)

for s in result.content.sentences:
    print(f"[p{s.page_number or '-'}:L{s.line_start}] {s.text}")
    # Exact retrieval:
    assert result.content.text[s.char_start : s.char_end] == s.text
```

**Guaranteed contracts** (asserted on every result — never "best
effort"):

- `content.text[s.char_start : s.char_end] == s.text` — byte-for-byte
  exact retrieval.
- `line_start`/`line_end` are 1-based indexes into `content.text`; the
  window `[line_start, line_end]` contains `s.text` as a substring.
- Sentences appear in reading order; non-overlapping; `index` is
  monotonic 0-based.
- When `content.pages` is present (PDF), every sentence has a non-null
  `page_index` **and** `page_number` (with `page_number == page_index +
  1`). When absent, both are `None` — never fabricated.
- When `content.sections` is present, `section_index` points to the
  **innermost** (most specific) enclosing section, or `None` for
  sentences before the first heading.
- Same bytes → same sentences, byte-identical. No timestamps, no
  randomness.

For the full contract reference (including chunk-and-cite recipes and
per-format fidelity), see [docs/citations.md](docs/citations.md). For a
complete walked-through **example of what the library returns for a
PDF**, see [docs/example-pdf-output.md](docs/example-pdf-output.md).

### Source path — where did this document come from?

Pass `path=` (or use a path-like input) and it flows to `Source.path`
verbatim so downstream citations can identify the source:

```python
# Automatic — from a path-like input:
r = extract("reports/q4.pdf")
r.source.path       # "reports/q4.pdf"

# Explicit — for bytes input:
data = open("reports/q4.pdf", "rb").read()
r = extract(data, mime="application/pdf", path="reports/q4.pdf")
r.source.path       # "reports/q4.pdf"
```

`Source.path` is caller-supplied metadata — `knovas-extract` never
opens, canonicalizes, or resolves it. It **is** validated to reject
inputs that would corrupt logs / terminals: NUL bytes, ASCII control
characters (`\n`, `\r`, ANSI escapes), Unicode bidi-override characters
(CVE-2021-42574 Trojan Source), and lengths over
`Limits.max_path_length` (default 4096). Rejections raise `ValueError`
with a class-of-violation message that never contains the offending
payload — re-logging the exception is safe.

Full reference including per-format metadata gap-fill:
[docs/metadata-and-paths.md](docs/metadata-and-paths.md).

### CLI

```bash
knovas-extract reports/q4.pdf --emit-sentences --emit-markdown --pretty
```

Emits the full `ExtractionResult` as JSON to stdout. `--emit-sentences`
and `--emit-markdown` are independent opt-ins.

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
        max_sentences=10_000,                # explicit DoS cap on sentence array
        max_metadata_value_length=1024,      # per-scalar cap in Metadata.extra
        max_xmp_bytes=256 * 1024,            # PDF XMP metadata size cap
        max_path_length=1024,                # Source.path length cap
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

Plus one stdlib exception: `ValueError` from `Source.path` validation
(caller misuse, distinct from document corruption). See
[docs/metadata-and-paths.md](docs/metadata-and-paths.md) for the full policy.

No bare exceptions, no `None`, no `Optional[ExtractionResult]`.

## Security promises (enforced by CI)

- **Never makes a network call.** Asserted across every test via `pytest-socket`.
- **Never executes embedded code.** PDF JavaScript, DOCX macros, RTF object linking — all stripped, warning emitted.
- **Never writes outside an explicit tmpdir.** ZIP-slip paths are rejected.
- **XML parsing is XXE-hardened.** All XML goes through `defusedxml`.
- Releases are **Sigstore-signed** + ship **SLSA L3 provenance**. Verify before installing in production — see [`RELEASING.md`](RELEASING.md).

For untrusted inputs, run inside a sandbox. Copy-paste recipes for `nsjail`, `bubblewrap`, and rootless Docker in [`docs/sandboxing.md`](docs/sandboxing.md).

## Spec conformance

This implementation conforms to `spec_version = 1.2.0` of [`knovas/KnowledgeBase/clients/extraction/spec`](https://github.com/knovas/KnowledgeBase/tree/develop/clients/extraction/spec). The pinned spec sha is recorded in `tests/spec/` (Git submodule). Every release runs the spec's golden corpus + adversarial corpus before tagging.

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
