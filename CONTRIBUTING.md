# Contributing — knovas-extract-python

If you only want to **use** the library, see the [README](README.md). This doc
is for engineers extending or fixing the package itself.

## Repo layout

```
.
├── src/knovas_extract/
│   ├── __init__.py           # public API
│   ├── _version.py           # __version__ + SPEC_VERSION
│   ├── interfaces.py         # IExtractor protocol
│   ├── result.py             # ExtractionResult dataclass + Limits
│   ├── errors.py             # typed ExtractError hierarchy
│   ├── normalize.py          # text canonicalizer (shared cross-language)
│   ├── dispatch.py           # MIME detection + lazy extractor routing
│   ├── cli.py                # `knovas-extract <file>` JSON-to-stdout
│   └── extractors/           # one module per format
│       ├── txt.py            # stdlib + chardet
│       ├── md.py             # stdlib + python-frontmatter
│       ├── pdf.py            # PyMuPDF (AGPL)
│       ├── docx.py           # python-docx + mammoth (defusedxml-hardened)
│       ├── html.py           # selectolax (lexbor)
│       ├── rtf.py            # striprtf
│       ├── eml.py            # stdlib email
│       └── msg.py            # extract-msg (GPL)
├── tests/
│   ├── unit/                 # per-extractor unit tests, no I/O
│   ├── golden/               # parametrized over the spec corpus
│   ├── property/             # hypothesis + network-isolation + leaks
│   └── fuzz/                 # atheris targets (Linux/macOS only)
├── bench/                    # pytest-benchmark per format
├── docs/
│   └── sandboxing.md         # nsjail / bubblewrap / rootless container recipes
├── pyproject.toml            # hatch envs + all tool config
├── SECURITY.md               # disclosure policy + SLAs
├── RELEASING.md              # maintainer cut-the-release checklist
└── .github/workflows/        # ci.yml + release.yml + nightly.yml
```

## Local dev setup

```bash
git clone https://github.com/Seifeddini/knovas-extract-python.git
cd knovas-extract-python

# 1) Install hatch (one-time).
pip install hatch

# 2) Optionally clone the spec repo as a sibling so golden tests work locally.
#    Without this, golden tests cleanly skip with "no-spec-found".
git clone https://github.com/Seifeddini/knovas-extract-spec.git ../knovas-extract-spec

# 3) Run the gates.
hatch -e lint run all          # ruff + ruff format + mypy --strict + pyright
hatch run test                 # unit tests (default env, fast)
hatch -e golden run run        # corpus contract tests (needs spec)
hatch -e property run run      # hypothesis + leak + thread-safety
hatch -e sec run all           # bandit + pip-audit
hatch -e bench run run         # pytest-benchmark (saves to .benchmarks/)
```

Hatch envs are defined in `pyproject.toml`. The `lint` env installs **all
format extras** so mypy + pyright introspect real types; the `default` env is
minimal so tests run fast in the CI matrix.

## Making changes

### Bugfix in an existing extractor

1. Reproduce with a unit test in `tests/unit/test_extractors_<format>.py` —
   the test should fail.
2. Fix the extractor.
3. Re-run `hatch -e lint run all` and `hatch run test` — both must pass.
4. If the bug affected golden output, re-run `hatch -e golden run run` and
   triage carefully: an extractor change that flips a golden test is usually
   a spec issue (open a PR on the spec repo) **or** a regression (revert).

### Adding a new format

1. **Spec side first.** In the spec repo, follow
   [`docs/adding-a-format.md`](https://github.com/Seifeddini/knovas-extract-spec/blob/main/docs/adding-a-format.md):
   - Allocate `metadata.extra.<format>:*` keys in `docs/schema-fields.md`.
   - Add ≥3 golden fixtures with hand-curated `.expected.json`.
   - Add ≥3 adversarial fixtures (decompression bomb / zip-slip / truncated /
     polyglot — whichever apply). Each gets an `expected` + `provenance` in
     `MANIFEST.yaml::adversarial`.
   - Bump `corpus_version`. PR + merge.
2. **Python side.** In this repo:
   - Add the runtime dep to `pyproject.toml::project.optional-dependencies`
     under a new extra name.
   - Implement `src/knovas_extract/extractors/<format>.py` using `IExtractor`.
     Register MIMEs at import-time on `MIME_REGISTRY`.
   - Add the MIME → module mapping to `dispatch._LAZY_LOADERS`.
   - Unit tests in `tests/unit/test_extractors_<format>.py` (cover at minimum:
     happy path, corrupt input, encoding edge case, every Limits cap).
   - Bench target in `bench/bench_<format>.py`.
   - Fuzz target in `tests/fuzz/fuzz_<format>.py`.
3. **Verify.** `hatch -e lint run all` + `hatch run test` + `hatch -e golden run run`
   must all be green.
4. Open the PR. CI's required status checks gate the merge.

### Adding a new language implementation

Out of scope for this repo. See
[`adding-a-language.md`](https://github.com/Seifeddini/knovas-extract-spec/blob/main/docs/adding-a-language.md)
in the spec repo.

## Style + conventions

- **Public API surface lives in `__init__.py` only.** Adding a new public name
  → add to `__init__.py::__all__` AND document it in the README.
- **Every public entry point returns `ExtractionResult` OR raises a subclass of
  `ExtractError`.** No bare exceptions, no `None` on success. Property tests
  enforce this; if you bypass the contract, fuzz will find it.
- **No network calls.** Ever. `pytest-socket` globally disables sockets in
  tests; if you ever need to add network code, that's a different package.
- **No code execution.** PDF JS, DOCX macros, RTF object linking — all
  stripped, warning emitted. Never `exec`/`eval`/`subprocess` user content.
- **Text canonicalization** (`normalize.canonicalize_text`) MUST be the LAST
  step before returning `content.text`. Cross-language equality depends on it.
- **Lazy imports per format.** Importing `knovas_extract` should NOT import
  PyMuPDF/python-docx/etc. The extractors import their backends inside the
  module body, and `dispatch._get_extractor` lazy-imports on first use.

## Security gates (every PR)

| Gate | What | How to fix |
|---|---|---|
| `ruff check` | style + many correctness bugs | `hatch -e lint run fix` |
| `ruff format --check` | formatting | `hatch -e lint run fix` |
| `mypy --strict` | type errors under strict | type the code; for genuinely-untyped third-party deps add to `pyproject.toml::[[tool.mypy.overrides]]` |
| `pyright` | second opinion (advisory; warnings allowed) | usually fixable with `cast()` |
| `bandit` | Python security antipatterns | take the warning seriously; only `# nosec` with a justification |
| `pip-audit` + `osv-scanner` | dep CVEs | bump the dep or replace it |
| `CodeQL` | semantic / taint | fix the flagged code; don't ignore |
| Adversarial corpus | typed-error contract on hostile inputs | the test will tell you which fixture broke and what error class was expected |
| CIFuzz (atheris) | random-bytes never crash | the crash repro is in the workflow logs; fix the extractor to raise a typed error |
| `gitleaks` + `trufflehog` | committed secrets | rotate the secret + history-rewrite |

If you're stuck, see `pyproject.toml` for exactly how each tool is invoked.

## Cutting a release

See [`RELEASING.md`](RELEASING.md) — the full pre-flight + tag + verify
checklist. Short version: bump `_version.py` + `pyproject.toml`, update
`CHANGELOG.md`, tag with `git tag -s vX.Y.Z`, push the tag. `release.yml`
takes over (reproducible build → Sigstore signing → SLSA L3 provenance →
SBOM → PyPI publish via OIDC Trusted Publisher).

## Reporting a vulnerability

**Do NOT open a public issue for security reports.** See [`SECURITY.md`](SECURITY.md).
