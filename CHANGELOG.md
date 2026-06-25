# Changelog — knovas-extract (Python)

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); SemVer.

A **major** version bump matches the major of `spec_version` it conforms to.

## [Unreleased]

### Added
- Repository scaffold: pyproject (hatch), src layout, test directory tree, CI workflow skeleton, SECURITY.md.
- Core modules: `interfaces.IExtractor`, `result.ExtractionResult`, `errors.ExtractError` hierarchy, `normalize` canonicalizer, `dispatch` MIME router.
- `extractors.txt` (stdlib + chardet) and `extractors.md` (stdlib + python-frontmatter).
- Test scaffold: unit tests for txt/md, golden runner template, hypothesis property test, atheris fuzz target, pytest-benchmark scaffolding.
- Security baseline: pytest-socket network isolation, bandit/semgrep/pip-audit in CI, pre-commit (ruff/mypy/bandit/gitleaks).

[Unreleased]: https://github.com/knovas/knovas-extract-python/compare/HEAD...HEAD
