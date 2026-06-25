"""
knovas-extract — privacy-preserving, performant document extraction.

Public API:

    from knovas_extract import (
        extract,                 # primary entry point
        ExtractionResult,        # output type
        ExtractError,            # base of typed error hierarchy
        UnsupportedFormatError,
        CorruptDocumentError,
        EncryptedDocumentError,
        ResourceExhaustedError,
        DependencyMissingError,
        Limits,                  # per-call resource caps
    )

Promises (asserted by CI; see SECURITY.md):
- Never makes a network call.
- Never executes embedded code (PDF JS, DOCX macros, RTF object linking).
- Every public entry point returns a valid ExtractionResult OR raises a typed
  subclass of ExtractError. No bare exceptions, no None returns on success.

Spec conformance: this implementation pins clients/extraction/spec @ <pinned-sha>.
"""
from __future__ import annotations

from knovas_extract._version import SPEC_VERSION, __version__
from knovas_extract.dispatch import extract
from knovas_extract.errors import (
    CorruptDocumentError,
    DependencyMissingError,
    EncryptedDocumentError,
    ExtractError,
    ResourceExhaustedError,
    UnsupportedFormatError,
)
from knovas_extract.result import (
    ExtractionResult,
    Limits,
    Metadata,
    Page,
    Section,
    Source,
)

__all__ = [
    "CorruptDocumentError",
    "DependencyMissingError",
    "EncryptedDocumentError",
    "ExtractError",
    "ExtractionResult",
    "Limits",
    "Metadata",
    "Page",
    "ResourceExhaustedError",
    "SPEC_VERSION",
    "Section",
    "Source",
    "UnsupportedFormatError",
    "__version__",
    "extract",
]
