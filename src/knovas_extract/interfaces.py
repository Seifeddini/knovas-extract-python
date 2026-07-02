"""IExtractor protocol — the per-format contract.

Every extractor module (`extractors/txt.py`, `extractors/pdf.py`, ...) exposes
exactly one class implementing this protocol. The class is registered with
`dispatch` at import time via the `MIME_REGISTRY` map.

The single-method protocol mirrors the IDocumentChunker style established in
the Semantix backend (`knovas-software/app/src/interfaces/IDocumentChunker.py`).
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from knovas_extract.result import ExtractionResult, Limits


@runtime_checkable
class IExtractor(Protocol):
    """Strategy interface for one document format.

    Implementations MUST:
    - Declare `supported_mimes` as a frozenset of MIME types they accept.
    - Declare `name` for diagnostics.
    - Return an ExtractionResult OR raise a subclass of ExtractError.
    - Never make a network call.
    - Honor the provided `Limits`.
    """

    supported_mimes: ClassVar[frozenset[str]]
    name: ClassVar[str]

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
        emit_markdown: bool = False,
        emit_sentences: bool = False,
    ) -> ExtractionResult: ...
