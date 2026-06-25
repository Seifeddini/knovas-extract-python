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
    """

    max_input_bytes: int = 100 * 1024 * 1024  # 100 MiB
    max_pages: int = 10_000
    max_decompression_ratio: int = 100  # ZIP-bomb guard
    max_text_bytes: int = 50 * 1024 * 1024  # 50 MiB extracted text
    max_recursion_depth: int = 256  # RTF / nested-XML guard


@dataclass(slots=True)
class Source:
    mime_type: str
    sha256: str
    size_bytes: int
    filename: str | None = None


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


@dataclass(slots=True)
class Section:
    heading: str
    level: int
    text: str


@dataclass(slots=True)
class Content:
    text: str
    pages: list[Page] | None = None
    sections: list[Section] | None = None


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
        # asdict turns `Content.pages` / `Content.sections` into lists-of-dicts
        # (good) but does NOT collapse `[]` to `None`. We want explicit null when
        # the format isn't paginated/sectioned, so the contract is unambiguous.
        if not d["content"]["pages"]:
            d["content"]["pages"] = None
        if not d["content"]["sections"]:
            d["content"]["sections"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractionResult:
        return cls(
            spec_version=data["spec_version"],
            source=Source(**data["source"]),
            metadata=Metadata(**data["metadata"]),
            content=Content(
                text=data["content"]["text"],
                pages=[Page(**p) for p in data["content"]["pages"]]
                if data["content"].get("pages")
                else None,
                sections=[Section(**s) for s in data["content"]["sections"]]
                if data["content"].get("sections")
                else None,
            ),
            warnings=list(data.get("warnings") or []),
            extractor=Extractor(**data["extractor"]),
        )
