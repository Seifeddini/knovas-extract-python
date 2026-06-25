"""Typed error hierarchy for knovas-extract.

Every public entry point in this package either returns a valid ExtractionResult
or raises a subclass of ExtractError. The hierarchy is closed — adding a new
error class is a minor version bump and must update the schema docs.
"""

from __future__ import annotations


class ExtractError(Exception):
    """Base class for all errors raised by knovas-extract."""


class UnsupportedFormatError(ExtractError):
    """The input's MIME type is not handled by any registered extractor."""

    def __init__(self, mime: str, *, filename: str | None = None) -> None:
        self.mime = mime
        self.filename = filename
        super().__init__(
            f"no extractor registered for MIME type {mime!r}"
            + (f" (file: {filename!r})" if filename else "")
        )


class CorruptDocumentError(ExtractError):
    """The extractor could not parse the input as a valid document of its claimed format."""


class EncryptedDocumentError(ExtractError):
    """The input is encrypted or password-protected; no password was provided."""


class ResourceExhaustedError(ExtractError):
    """Extraction was aborted because a Limits threshold was crossed.

    Raised for: decompression bombs, page-count caps, memory caps,
    pathological recursion depth.
    """

    def __init__(
        self, what: str, limit: int | float, *, observed: int | float | None = None
    ) -> None:
        self.what = what
        self.limit = limit
        self.observed = observed
        msg = f"resource limit exceeded: {what} > {limit}"
        if observed is not None:
            msg += f" (observed {observed})"
        super().__init__(msg)


class DependencyMissingError(ExtractError):
    """An optional dependency required for this format is not installed.

    Example: extracting a PDF without `pip install knovas-extract[pdf]`.
    """

    def __init__(self, extra: str, missing_package: str) -> None:
        self.extra = extra
        self.missing_package = missing_package
        super().__init__(
            f"missing optional dependency {missing_package!r}. "
            f"Install with: pip install 'knovas-extract[{extra}]'"
        )
