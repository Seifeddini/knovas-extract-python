"""Protocols — the per-format extractor contract and the OCR seams.

Every extractor module (`extractors/txt.py`, `extractors/pdf.py`, ...) exposes
exactly one class implementing `IExtractor`. The class is registered with
`dispatch` at import time via the `MIME_REGISTRY` map.

`IOcrBackend` and `IOcrCache` are the two injection points of the PDF OCR
pipeline (`extract(..., ocr=OcrOptions(backend=..., cache=...))`): the
obligation tests run the per-page decision and the budget scheduler with a
fake backend on every CI leg, and the RemoteController supplies its own
disk cache. Both are structural (`Protocol`) so a plain class with the
right methods qualifies — no inheritance needed.

The single-method protocols mirror the IDocumentChunker style established in
the Semantix backend (`knovas-software/app/src/interfaces/IDocumentChunker.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from knovas_extract.result import ExtractionResult, Limits

if TYPE_CHECKING:
    from knovas_extract._ocr.tsv import OcrPageResult


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


@runtime_checkable
class IOcrBackend(Protocol):
    """One OCR engine.

    `recognize` receives a `knovas_extract._ocr.preprocess.PageImage` (the
    preprocessed gray page as a numpy array, its dpi, `page_index`, the
    frame → page matrix, and — for engines that render themselves — the
    PyMuPDF page) and returns either the page text (`str`) or an
    `OcrPageResult` (text + words with boxes in page points + mean
    confidence). Implementations MUST be safe to call from several worker
    threads at once (keep engine state thread-local), MUST NOT write files,
    and MUST NOT put page content into exceptions or warnings.

    Optional attributes the pipeline reads with `getattr`:
    ``name`` (reported as ``pdf:ocr_backend``), ``version``,
    ``needs_image`` (``False`` for engines that render the page themselves;
    they then run on the calling thread) and
    ``detect_orientation(gray, dpi) -> int | None`` (degrees clockwise to
    upright, used only for sideways pages).
    """

    def recognize(self, page_image: Any) -> str | OcrPageResult: ...


@runtime_checkable
class IOcrCache(Protocol):
    """Key/value store for OCR results (decision D5, GI-EXTRACT-04).

    Keys are opaque sha256 hex strings composed by the library from the
    page's image bytes plus every parameter that changes the result (dpi,
    language, psm, engine version, preprocessing). Values are JSON strings
    produced by `OcrPageResult.to_json` — they contain PAGE TEXT, so an
    implementation that persists them owns the at-rest copy: encrypt or
    scope it, purge entries when the document is deleted, create files
    0600. The library's default is a per-document in-memory dict.
    """

    def get(self, key: str) -> str | None: ...

    def put(self, key: str, value: str) -> None: ...
