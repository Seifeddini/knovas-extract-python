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
   - **Markdown emission** (`extract(..., emit_markdown=True)`) applies a stricter contract because Markdown is higher-fidelity than plain text and can carry hostile content that survives into a downstream renderer. Before conversion, the sanitizer in `src/knovas_extract/_markdown.py` strips **contents-and-tag** for `script`, `style`, `iframe`, `object`, `embed`, `applet`, `frame`, `frameset`, `noscript`, `svg`, `math`, `link`, `meta`, `base`, `template`, HTML comments, and CDATA sections. Attributes stripped element-wide: every `on*` event handler, `style`, `srcset`, `formaction`, `background`, `ping`, `nonce`, `integrity`, and any colon-namespaced attribute other than `xml:lang` / `xml:base`. `<a href>` URLs are allowed only for schemes `http`, `https`, `mailto`, `tel`; everything else — including `javascript:`, `data:`, `vbscript:`, `file:`, `chrome-extension:`, `blob:`, protocol-relative `//host`, and relative paths — is unwrapped to plain text with a counted warning. `<img>` is unconditionally replaced with its `alt` text (even for benign `https://` URLs) so downstream renderers cannot beacon on render. Structural DoS is bounded by `Limits.max_recursion_depth` (DOM depth), `Limits.max_text_bytes` (post-conversion size), and `Limits.max_markdown_expansion_ratio` (markdown-vs-text length). Warnings are **counted, not content**: e.g. `"markdown: 3 <script> tags stripped"` — the sanitizer never leaks the payload string. Consumers rendering `content.markdown` should still enable their renderer's HTML-passthrough safeguards; this sanitizer is defense-in-depth, not a replacement for the consumer's own escape/render policy.
3. **Never writes outside an explicit tmpdir.** ZIP-based formats (DOCX, MSG, …) have a path-traversal guard; zip-slip paths raise `CorruptDocumentError`.
4. **XML is hardened against XXE and entity-expansion bombs.** The document-*metadata* XML paths — DOCX `docProps/*.xml` and PDF XMP — parse through `defusedxml` with entity resolution forbidden. The larger XML *bodies* are parsed by third-party backends, each configured or bundled to be safe on our supported Python floor (>= 3.11), so no path resolves external entities or expands an entity bomb:
   - DOCX `word/document.xml` → python-docx / lxml with `resolve_entities=False` (no internal expansion, no external fetch; lxml also defaults to `no_network=True`). A billion-laughs DOCX surfaces as `CorruptDocumentError`; an external-entity reference is left unexpanded, never resolved.
   - DOCX headings / markdown → mammoth / stdlib expat: expat does not fetch external entities by default, and CPython >= 3.11 bundles libexpat >= 2.4 whose billion-laughs amplification cap is on by default (an amplification bomb raises, and is caught).
   - HTML → selectolax / lexbor, an HTML5 parser that ignores `<!ENTITY>` / `<!DOCTYPE>` declarations entirely (they never reach a DTD engine).

   This is deliberately **not** "all XML through `defusedxml`" — only the metadata paths are. See the "XML security posture" note in `src/knovas_extract/extractors/docx.py` for the per-parser detail. (Both the byte caps in `_guard_zip`/`max_xmp_bytes` and these entity protections are required: the byte caps bound what the parser *reads*, the entity protections bound what it *expands*, and an entity bomb is tiny on disk.)
5. **Resource caps enforced.** Per-call `Limits` for input size, page count, decompression ratio, text size, recursion depth, sentence count, path length, metadata scalar length, and XMP metadata size. Default values in `Limits()` are conservative.
6. **Typed errors only.** Every public entry point returns a valid `ExtractionResult` OR raises a subclass of `ExtractError`. `Source.path` validation is the one caller-misuse case that raises stdlib `ValueError` (with a class-of-violation message that never contains the payload). Bare exceptions are a bug.
7. **No telemetry, no content in logs, no content in warnings.** Zero outbound metrics. Logging is at INFO/WARNING and emits only counts/sizes/MIMEs — never document content. `result.warnings` follows the same rule: entries are either counted aggregates (`"metadata: 3 values truncated"`, `"markdown: 2 <script> tags stripped"`) or descriptive of a fixed condition — **never** substrings of the input. This guarantee extends to the surfaces added since `spec_version = 1.2.0` (current: `1.3.0`):
   - **`Source.path` validation** (`src/knovas_extract/_paths.py`) rejects NUL bytes, ASCII control characters 0x00–0x1F (except `\t`) — used for log injection and terminal hijacking — Unicode bidirectional-override / isolate characters (`U+202A..E`, `U+2066..9` — CVE-2021-42574 Trojan Source), and paths over `Limits.max_path_length`. Rejections raise `ValueError`; the exception message describes the class of violation without echoing the payload, so re-logging is safe. Not done deliberately: no `os.path.realpath` (leaks filesystem topology), no existence check, no file open.
   - **`Metadata.extra` scalar sanitization** (`src/knovas_extract/_metadata.py`) applies the same character policy plus a length cap (`Limits.max_metadata_value_length`) to every gap-filled scalar (HTML `<meta>` values, DOCX `docProps` fields, EML / MSG headers, PDF XMP fields, Markdown frontmatter). Drops and truncations produce counted warnings only — `"metadata: N values dropped for NUL / control / bidi-override characters"`, `"metadata: N values truncated"` — never the hostile value. URL-valued HTML meta fields (`canonical`, `og:url`, `og:image`, `twitter:url`, `twitter:image`) additionally pass through the URL scheme allowlist (`http`, `https`, `mailto`, `tel`); drops produce the counted warning `"html: dropped N URL(s) with disallowed scheme from meta / link"`.
   - **PDF XMP metadata** is size-capped at `Limits.max_xmp_bytes` (1 MiB default) **before** `defusedxml.ElementTree.parse` sees the bytes. Oversized XMP produces `"pdf: xmp metadata exceeded max_xmp_bytes; skipped"` and no XMP-derived keys in `extra`. `defusedxml` already disables external-entity fetching and DTD expansion; the cap is defense-in-depth against CPU/memory pressure from a "valid but huge" XMP block.
   - **Sentence tokenization** (`extract(..., emit_sentences=True)`) uses `pysbd`, a pure-Python (no C extensions, no network, no data download) regex-based segmenter pinned to `>=0.3.4,<0.4`. ReDoS is bounded by `Limits.max_text_bytes` upstream and `Limits.max_sentences` downstream. For pipelines processing untrusted documents, tighten `max_text_bytes` to 5–10 MiB to bound pysbd's worst-case work; chunk longer documents upstream.

- **Markdown emission** (`extract(..., emit_markdown=True)`) applies a stricter contract because Markdown is higher-fidelity than plain text and can carry hostile content that survives into a downstream renderer. See the extended section under promise #2 above for the tag / attribute / URL allowlist and DoS caps.

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
