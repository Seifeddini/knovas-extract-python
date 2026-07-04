"""ExtractionResult — the cross-language output contract.

Round-trips through the JSON Schema at
    clients/extraction/spec/schema.json
of the KnowledgeBase monorepo (pinned by sha in tests/spec/).

`Limits` lives here too because callers pass it to `extract(…, limits=…)` and
extractors honor it; keeping the public types co-located makes the import surface
single-file: `from knovas_extract import ExtractionResult, Limits`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-call resource caps. Defaults are conservative; tune via `extract(…, limits=…)`.

    These are enforced by individual extractors. The caller's process is NOT
    sandboxed by this library — for true isolation, run inside nsjail / bwrap.
    See docs/sandboxing.md.

    `max_markdown_expansion_ratio` guards the `emit_markdown=True` path
    against hostile inputs (deeply-nested lists / tables) that produce a
    markdown output disproportionately larger than the plain text. Applied
    only when markdown is emitted; ignored otherwise.

    `max_sentences` caps the size of `content.sentences` — an explicit DoS
    cap on inputs that produce pathologically many short segments.
    `max_path_length` caps `Source.path`.
    `max_metadata_value_length` caps every gap-filled scalar in
    `Metadata.extra` after `_metadata.sanitize_scalar` runs.
    `max_xmp_bytes` bounds PDF XMP metadata before defusedxml parses it.
    """

    max_input_bytes: int = 100 * 1024 * 1024  # 100 MiB
    max_pages: int = 10_000
    max_decompression_ratio: int = 100  # ZIP-bomb guard
    max_text_bytes: int = 50 * 1024 * 1024  # 50 MiB extracted text
    max_recursion_depth: int = 256  # RTF / nested-XML guard
    max_markdown_expansion_ratio: float = 3.0  # markdown-vs-text blowup guard
    max_sentences: int = 100_000  # sentence-array DoS cap
    max_path_length: int = 4096  # Source.path length cap (POSIX PATH_MAX)
    max_metadata_value_length: int = 4096  # per-scalar cap in Metadata.extra
    max_xmp_bytes: int = 1_048_576  # 1 MiB cap on PDF XMP metadata before parse


@dataclass(slots=True)
class Source:
    mime_type: str
    sha256: str
    size_bytes: int
    filename: str | None = None
    path: str | None = None  # caller-supplied path (abs or rel); stored verbatim


@dataclass(slots=True)
class Metadata:
    title: str | None = None
    author: str | None = None
    language: str | None = None
    created: str | None = None
    modified: str | None = None
    page_count: int | None = None
    word_count: int | None = None
    extra: dict[str, str | int | float | bool | None] = field(default_factory=dict)


@dataclass(slots=True)
class Page:
    index: int
    text: str
    line_start: int | None = None  # 1-based line in content.text
    line_end: int | None = None  # 1-based line in content.text (inclusive)


@dataclass(slots=True)
class Section:
    heading: str
    level: int
    text: str
    line_start: int | None = None  # 1-based line in content.text
    line_end: int | None = None  # 1-based line in content.text (inclusive)


@dataclass(slots=True)
class Sentence:
    """A citable sentence with exact + human-readable coordinates.

    Contract (all fields guaranteed by dispatch-level post-conditions when
    `emit_sentences=True`; see docs/citations.md for the full reference):

      - `content.text[char_start:char_end] == text` (exact retrieval).
      - `line_start` / `line_end` are 1-based line indices into
        `content.text` (which uses `\\n` line endings after canonicalization);
        the line window `[line_start, line_end]` contains `text` as a
        substring.
      - `index` is monotonic 0-based within the document.
      - Sentences are ordered by `char_start`; non-overlapping.
      - `page_index` / `page_number` are non-null iff the extractor emits
        `content.pages` (currently PDF); `page_number == page_index + 1`.
      - `section_index` is non-null iff `content.sections` exists AND the
        sentence's char window falls inside a section's line window; it
        points into `content.sections`.
    """

    index: int
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    page_index: int | None = None
    page_number: int | None = None
    section_index: int | None = None


@dataclass(slots=True)
class Table:
    """One structured table detected in a document (spec 1.1.0+).

    `client_table_hint` is a stable extractor-supplied label for correlation
    across pipeline stages (e.g. "pdf_p3_t0"). It is NOT a canonical id —
    downstream consumers such as the Knovas server derive a canonical
    `table_id` from `(tenant, document, this hint, table_index)` so client
    hints cannot be trusted as security boundaries.

    `headers` and `rows` cells are NFC-normalized. Rows have the same length
    as `headers` (extractors pad short rows with `""` or drop-with-warning).
    Cell length is bounded by `Limits`-derived caps (spec: 1024 chars); values
    exceeding the cap are truncated and a warning is appended.
    """

    client_table_hint: str
    title: str | None
    headers: list[str]
    rows: list[list[str]]
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class Content:
    text: str
    pages: list[Page] | None = None
    sections: list[Section] | None = None
    markdown: str | None = None
    sentences: list[Sentence] | None = None
    tables: list[Table] | None = None


@dataclass(slots=True)
class Extractor:
    name: str
    version: str


@dataclass(slots=True)
class ExtractionResult:
    spec_version: str
    source: Source
    metadata: Metadata
    content: Content
    warnings: list[str]
    extractor: Extractor

    # ----- JSON round-trip ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict that conforms to spec/schema.json exactly."""
        d = asdict(self)
        # asdict turns list fields into lists-of-dicts (good) but does NOT
        # collapse `[]` to `None`. We want explicit null when the format
        # doesn't have that structural axis, so the contract is unambiguous.
        # Sentences: `[]` is meaningful (opted-in and got nothing), `None`
        # means "wasn't asked for". Do NOT collapse.
        if not d["content"]["pages"]:
            d["content"]["pages"] = None
        if not d["content"]["sections"]:
            d["content"]["sections"] = None
        # tables: explicit null (matches spec.content.tables nullable) when
        # the format has no structured-table support or none were detected.
        if not d["content"].get("tables"):
            d["content"]["tables"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractionResult:
        src = data["source"]
        return cls(
            spec_version=data["spec_version"],
            source=Source(
                mime_type=src["mime_type"],
                sha256=src["sha256"],
                size_bytes=src["size_bytes"],
                filename=src.get("filename"),
                path=src.get("path"),
            ),
            metadata=Metadata(**data["metadata"]),
            content=Content(
                text=data["content"]["text"],
                pages=[Page(**p) for p in data["content"]["pages"]]
                if data["content"].get("pages")
                else None,
                sections=[Section(**s) for s in data["content"]["sections"]]
                if data["content"].get("sections")
                else None,
                markdown=data["content"].get("markdown"),
                sentences=[Sentence(**s) for s in data["content"]["sentences"]]
                if data["content"].get("sentences") is not None
                else None,
                tables=[
                    Table(
                        client_table_hint=t["client_table_hint"],
                        title=t.get("title"),
                        headers=list(t.get("headers") or []),
                        rows=[list(r) for r in (t.get("rows") or [])],
                        page=t.get("page"),
                        bbox=tuple(t["bbox"]) if t.get("bbox") is not None else None,
                    )
                    for t in data["content"]["tables"]
                ]
                if data["content"].get("tables") is not None
                else None,
            ),
            warnings=list(data.get("warnings") or []),
            extractor=Extractor(**data["extractor"]),
        )
