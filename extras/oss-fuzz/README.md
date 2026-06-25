# OSS-Fuzz integration files

Continuous fuzzing for `knovas-extract` via Google's
[OSS-Fuzz](https://google.github.io/oss-fuzz/). Free for OSS; Google
runs the workers 24/7 and reports crashes privately to maintainers
via the email in `project.yaml::auto_ccs` and `primary_contact`.

## To enable

1. Fork [google/oss-fuzz](https://github.com/google/oss-fuzz).
2. Create `projects/knovas-extract/` in the fork and copy the three
   files in this directory there:
   ```bash
   mkdir -p projects/knovas-extract
   cp <path-to-knovas-extract-python>/extras/oss-fuzz/* projects/knovas-extract/
   ```
3. Test locally (Docker required):
   ```bash
   python infra/helper.py build_image knovas-extract
   python infra/helper.py build_fuzzers --sanitizer address knovas-extract
   python infra/helper.py check_build knovas-extract
   python infra/helper.py run_fuzzer knovas-extract fuzz_pdf
   ```
4. Open a PR against `google/oss-fuzz`. OSS-Fuzz maintainers review
   typically within 1–2 weeks. Once merged, scans start automatically
   and reports land in `security@knovas.ch`.

## What gets fuzzed

Every `tests/fuzz/fuzz_*.py` target in the Python repo. Currently:
`fuzz_txt`, `fuzz_md`, `fuzz_pdf`, `fuzz_docx`, `fuzz_html`,
`fuzz_rtf`, `fuzz_eml`.

Each target calls the public `extract()` API; the only acceptable
exceptions are subclasses of `ExtractError`. Anything else (untyped
`Exception`, `RuntimeError`, `UnicodeDecodeError`, etc.) escaping is
a contract violation and a crash report.

## Why this isn't already enabled

OSS-Fuzz registration is one-shot human effort (PR to a third-party
repo, maintainer review). Doing it ahead of 0.1.x ship would have
gated the release on someone else's queue. Now that the library is
public, we can submit the PR independently.

## Related

- [`tests/fuzz/`](../../tests/fuzz/) — per-extractor atheris targets
- [`.github/workflows/ci.yml::cifuzz`](../../.github/workflows/ci.yml) —
  CIFuzz step that runs each target for 60 s on every PR (advisory)
- [`SECURITY.md`](../../SECURITY.md) — vulnerability disclosure policy
