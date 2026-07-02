# Changelog — knovas-extract (Python)

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); SemVer.

A **major** version bump matches the major of `spec_version` it conforms to.

## [Unreleased]

### Added — sentence citations (`spec_version` 1.2.0)
- **`content.sentences` field + `extract(..., emit_sentences=True)` opt-in.**
  Deterministic pysbd-based tokenization with exact char offsets, 1-based
  line coordinates (into `content.text`), page back-pointers (PDF), and
  section back-pointers (formats that emit `content.sections`). Every
  guarantee documented in [`docs/citations.md`](docs/citations.md) is
  enforced by a post-condition in `dispatch.extract` — exact retrieval
  (`content.text[s.char_start : s.char_end] == s.text`), non-overlapping
  ordering, monotonic 0-based `index`, page↔sentence linkage, section
  bounds. Off by default; existing callers see zero behaviour or
  performance change.
- **`Sentence` dataclass** — `index`, `text`, `char_start`, `char_end`,
  `line_start`, `line_end`, `page_index`, `page_number`, `section_index`.
- **`Source.path` field.** Caller-supplied source path (via `extract(...,
  path="...")` or automatically from a path-like input). Stored verbatim
  after validation; never opened, canonicalized, or resolved.
  `Source.filename` still derives from the basename.
- **Line coordinates on `Page` and `Section`.** New `line_start` /
  `line_end` fields (1-based, into `content.text`). Populated by every
  extractor that produces pages or sections.
- **New optional-dependency group `[sentences]`** (adds `pysbd`, MIT,
  pure-Python, 22 languages, no data download).
- **New `Limits` knobs**: `max_sentences` (default 100 000),
  `max_path_length` (default 4096, POSIX PATH_MAX), `max_metadata_value_length`
  (default 4096), `max_xmp_bytes` (default 1 MiB).
- **`--emit-sentences` CLI flag** mirroring the kwarg; argv path
  auto-flows into `Source.path`.
- **`src/knovas_extract/_sentences.py`** — pysbd wrapper with
  `split_sentences`, `split_sentences_for_pages` (PDF per-page
  stitching), and `attach_section_indices` (innermost-enclosing-section
  back-pointer).

### Added — metadata gap-fill + hardening
- **Per-format `Metadata.extra` expansion** — every format's
  `extra` namespace grew substantially. Full reference:
  [`docs/metadata-and-paths.md`](docs/metadata-and-paths.md).
  - **PDF**: `pdf:pdf_version`, `pdf:permissions` (bitmask decoded to
    tokens `print,modify,copy,...`), `pdf:outline_count`,
    `pdf:is_form_pdf`, `pdf:pdfa_part`; whole-doc XMP metadata
    (`pdf:xmp_description`, `pdf:xmp_creator_tool`) parsed via
    `defusedxml` with a `max_xmp_bytes` size cap **before** parse. XMP
    `dc:title` / `dc:creator` / `dc:language` are merged into top-level
    `Metadata` (XMP wins over the older `Info` dict when both non-empty).
  - **DOCX**: parses `docProps/app.xml` via `defusedxml` alongside the
    existing `core.xml`. New `docx:application`, `docx:app_version`,
    `docx:template`, `docx:total_time`, `docx:pages_declared`,
    `docx:paragraph_count`, `docx:word_count_declared`,
    `docx:character_count`, `docx:company`, `docx:manager`. From core.xml:
    `docx:category`, `docx:content_status`, `docx:version`.
  - **HTML**: `html:author`, `html:canonical`, `html:robots`,
    `html:generator`; Open Graph via **`property=`** selectors (`html:og:title`,
    `html:og:description`, `html:og:url`, `html:og:type`,
    `html:og:site_name`, `html:og:image`); Twitter Card (accepts both
    `name="twitter:..."` and `property="twitter:..."`); article schema
    (`html:article_published_time`, `html:article_modified_time`,
    `html:article_author`, `html:article_section` — the timestamps also
    feed into `Metadata.created` / `.modified` when null).
  - **EML**: 11 new headers via stdlib `email`: `eml:reply_to`,
    `eml:sender`, `eml:in_reply_to`, `eml:references`,
    `eml:delivered_to`, `eml:return_path`, `eml:list_id`,
    `eml:list_unsubscribe`, `eml:priority`, `eml:importance`,
    `eml:auth_results`, `eml:content_language`.
  - **MSG**: `msg:cc`, `msg:bcc`, `msg:reply_to`, `msg:in_reply_to`,
    `msg:conversation_topic`, `msg:conversation_index`,
    `msg:categories`, `msg:sent_representing`, `msg:importance` (int),
    `msg:sensitivity` (int).
  - **Markdown**: unknown frontmatter keys now land at
    `md:frontmatter.<key>` (previously `md:<key>`). Nested dicts are
    JSON-serialized with sorted keys; scalars pass through.
- **`src/knovas_extract/_metadata.py`** — new shared `sanitize_scalar`
  helper. Every gap-filled `Metadata.extra` value flows through it:
  rejects NUL / ASCII control (except `\t`) / Unicode bidi-override chars
  (CVE-2021-42574 Trojan Source), truncates over `max_metadata_value_length`.
  Drops and truncations produce **counted, content-free warnings**
  (`"metadata: 3 values truncated"` etc.) — the offending payload never
  appears in `result.warnings`.
- **`src/knovas_extract/_paths.py`** — validates `Source.path` under the
  same character policy plus length cap. Rejections raise `ValueError`
  with class-of-violation messages that never contain the payload.

### Changed
- **`spec_version` bumped to `1.2.0`** to signal the additive
  `content.sentences`, `Source.path`, and `Page.line_*` / `Section.line_*`
  fields. Consumers on 1.1.0 schemas will read the new fields as unknown
  keys; the parallel spec PR on `knovas-extract-spec` opens
  `content.sentences` and `source.path` in the schema (skipped until it
  lands — see `tests/unit/test_result.py`).
- HTML section extraction now computes `line_start` / `line_end` against
  the final `content.text` (post-canonicalization), so the retrieval
  formula in `docs/citations.md` resolves cleanly.
- DOCX section extraction likewise takes the canonicalized text as input
  for line-coord computation.
- Markdown frontmatter unknown-key namespace changed from `md:<key>` to
  `md:frontmatter.<key>` for clarity (matches the `html:og:` /
  `html:twitter:` shape).

### Security
- **`Source.path` and `Metadata.extra` are new trust boundaries.** Both
  are conduits for hostile content from an untrusted document into
  consumer logs, terminals, and downstream renderers. Every value flows
  through a shared character-safety validator that rejects NUL / ASCII
  control / Unicode bidi-override chars (Trojan Source). Rejections
  produce counted, content-free warnings so re-logging never
  re-introduces the payload. See [`SECURITY.md`](SECURITY.md) promise #7
  for the full contract.
- **PDF XMP metadata parsing is size-capped** at `Limits.max_xmp_bytes`
  **before** `defusedxml` sees the bytes. defusedxml already disables
  external-entity fetching and DTD expansion; the cap is
  defense-in-depth for CPU/memory pressure from a "valid but huge" XMP
  block.
- **Sentence-emission path is regex-heavy (pysbd) and pure-Python.** No
  network, no C extensions. Bounded by `Limits.max_text_bytes` upstream
  and `Limits.max_sentences` downstream. Consumers processing untrusted
  documents should tighten `max_text_bytes` (5–10 MiB) to keep pysbd's
  worst-case work bounded.

### Added — earlier in Unreleased (Markdown emission, `spec_version` 1.1.0)
- **`content.markdown` field + `extract(..., emit_markdown=True)` opt-in.**
  Populates `content.markdown` with a sanitized whole-document Markdown
  rendering when requested. Off by default — existing callers see no
  behavior or performance change. Whole-doc scope (per-page markdown is
  not emitted; use `content.pages[*].text` for per-page granularity).
- **New optional-dependency group `[markdown]`** (adds `markdownify`,
  MIT). Also adds `pymupdf4llm` (Artistic-2.0, transitively AGPL via
  PyMuPDF — same license posture as the existing `[pdf]` extra) to
  `[pdf]` for the PDF markdown path.
- **`Limits.max_markdown_expansion_ratio`** (default `3.0`) — guards
  hostile inputs (nested tables / deeply-nested emphasis) whose Markdown
  output balloons disproportionately relative to plain text.
- **`--emit-markdown` CLI flag** mirroring the kwarg.
- **`src/knovas_extract/_markdown.py`** — the shared HTML→Markdown
  sanitizer. Enforces a tag denylist, attribute denylist, URL scheme
  allowlist (`http`, `https`, `mailto`, `tel`), and image-alt-only
  policy. See [SECURITY.md → Markdown emission](SECURITY.md) for the
  full contract.
- **Adversarial fixtures** in `tests/fixtures/adversarial/markdown/` and
  a corpus test that runs each through `extract(..., emit_markdown=True)`
  asserting no denylist literal survives.

### Changed
- **`spec_version` bumped to `1.1.0`** to signal the additive
  `content.markdown` field. Consumers on 1.0.0 schemas will read the
  new field as an unknown key; the parallel spec PR on
  `knovas-extract-spec` opens `content.markdown` in the schema (skipped
  until it lands — see `tests/unit/test_result.py`).
- The DOCX extractor's mammoth HTML conversion is now called **once**
  and reused for both `sections[]` and (when requested) `content.markdown`.

### Security
- The Markdown emission path is a new trust boundary. All hostile HTML
  routed through the sanitizer is stripped before conversion; downstream
  renderers of `content.markdown` should still apply their own
  escape/render policy. See `SECURITY.md` promise #2 for the detailed
  contract. `bandit` and `pip-audit` gates unchanged.

## [0.1.3] — 2026-06-25

### Fixed
- **macOS Golden tests** now run strictly. The actual failure mode (finally
  captured via a maintainer-pasted CI log) is a PyMuPDF C-extension segfault
  at `import` on the macOS-latest GH runners — not anything in knovas-extract
  itself. Linux + Windows are unaffected. Skipped PDF tests on macOS at the
  test layer (`pytest.skip("PyMuPDF C-extension segfaults on macOS runners
  (upstream)")`) so the other 7 formats DO run strictly on macOS. The
  blanket `continue-on-error: ${{ runner.os == 'macOS' }}` in `ci.yml`
  is removed; the macOS diagnostic step is also gone (purpose served).
- The previous claim that the dispatch generic-MIME fallback (0.1.2) was
  what fixed macOS was wrong — that change is a real improvement but
  unrelated to the actual platform failure.

### Known limitations
- **PDF extraction is not validated on macOS** in our CI matrix. The library
  *should* work on macOS for end users (the segfault may be GH-runner
  specific, related to dyld/codesigning quirks on the macos-latest image),
  but we cannot certify it until upstream PyMuPDF ships a fix. Track at
  https://github.com/pymupdf/PyMuPDF/issues. Linux + Windows PDF support is
  fully verified.

## [0.1.2] — 2026-06-25

### Fixed
- `dispatch._detect_mime` now prefers the filename extension over libmagic
  when libmagic returns a generic MIME (`text/plain`, `application/zip`,
  `text/xml`, `application/xml`, `application/octet-stream`). Fixes
  libmagic-version-skew on macOS where some installs classify `.eml` as
  `text/plain` (sending dispatch to the wrong extractor).
- `dispatch.extract` now re-frames the late-stage `ImportError` an
  extractor's lazy backend import can raise into `DependencyMissingError`,
  keeping the "every public call returns `ExtractionResult` or raises a
  subclass of `ExtractError`" contract honest in minimal-install envs.

### Added
- `tests/property/test_no_leaks.py` — extracts a synthetic per-format
  payload set 200× and asserts RSS stays bounded (≤10 % growth + 30 MiB
  slack). Marked `slow` + `linux_only`; runs in the `property` hatch env.
- `extras/oss-fuzz/` — `project.yaml` + `Dockerfile` + `build.sh` ready
  to copy into a `google/oss-fuzz` fork for continuous fuzzing (covers
  all 7 atheris targets, seeded from the spec golden corpus).
- `CONTRIBUTING.md` — engineering onboarding (already shipped in 0.1.1
  branch but worth highlighting).

### Internal
- `tests/golden/test_corpus.py` + `test_adversarial.py` skip cleanly with
  `pytest.skip` when a format extra isn't installed (rather than failing
  on `DependencyMissingError`). Means `hatch run test` (default env)
  passes against a real spec sibling without needing every backend.
- `pyproject.toml::tool.hatch.envs.golden` now installs format extras so
  the golden corpus actually validates output (was failing in CI
  because no PyMuPDF/python-docx/etc. → DependencyMissingError on every
  fixture).
- `pyproject.toml::tool.hatch.envs.property` now installs format extras
  too (for the new leak test).
- `release.yml` — `actions/upload-artifact@v4` no longer nests `dist/`;
  `sigstore/gh-action-sigstore-python` bumped to v3.4.0 (was the v0.1.0
  blocker; same fix as 0.1.1).

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
