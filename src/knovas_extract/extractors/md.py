"""Markdown extractor.

Handles `text/markdown`. Lifts YAML frontmatter into Metadata when present.
Derives `Section[]` from ATX headings (`#`, `##`, …). Does NOT render Markdown
to HTML — we want the raw source preserved (it's what humans read).
"""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import ResourceExhaustedError
from knovas_extract.extractors.txt import _decode
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Section

# ATX heading: `# foo`, `## foo`, ... up to 6 hashes.
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)

# Frontmatter keys we promote to top-level Metadata. Anything else lands in
# metadata.extra under the `md:` namespace.
_PROMOTED_KEYS = {"title", "author", "language", "date", "created", "modified"}


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """If text starts with '---\\nKEY: VALUE\\n---\\n', return (parsed-dict, body).

    Uses python-frontmatter if available; otherwise a minimal in-line parser
    that handles the common case (string values only). The in-line parser is
    deliberately strict — anything unusual falls through with empty meta.
    """
    if not text.startswith("---\n"):
        return {}, text

    try:
        import frontmatter

        post = frontmatter.loads(text)
        return dict(post.metadata or {}), post.content
    except ImportError:
        pass

    # Tiny inline parser.
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, object] = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, body


def _extract_sections(body: str, canonical_text: str) -> list[Section]:
    """Flat list of (heading, level, text) sections in document order.

    A section's `text` runs from immediately after its heading line up to the
    next heading of equal-or-lower level (so an h2 ends at the next h1 or h2,
    not at h3 children).

    Line coordinates (`line_start` / `line_end`, 1-based) are computed
    against ``canonical_text`` so they align with `content.text` and the
    consumer-facing retrieval formula. Headings not found in the canonical
    text (rare — happens when canonicalization reflows whitespace
    dramatically) get `None` coords.
    """
    matches = list(_HEADING.finditer(body))
    if not matches:
        return []

    sections: list[Section] = []
    cursor = 0
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end() + 1  # +1 to skip the newline after the heading
        # Find the next heading of equal-or-lower level.
        end = len(body)
        for j in range(i + 1, len(matches)):
            if len(matches[j].group(1)) <= level:
                end = matches[j].start()
                break
        section_text = canonicalize_text(body[start:end])

        # Line coords: locate the heading string in canonical_text.
        line_start: int | None = None
        line_end: int | None = None
        hstart = canonical_text.find(heading, cursor)
        if hstart >= 0:
            line_start = 1 + canonical_text.count("\n", 0, hstart)
            # End of section in canonical text: search for the NEXT section's
            # heading; otherwise use end-of-text.
            next_pos = len(canonical_text)
            for j in range(i + 1, len(matches)):
                if len(matches[j].group(1)) <= level:
                    nh = matches[j].group(2).strip()
                    n = canonical_text.find(nh, hstart + len(heading))
                    if n >= 0:
                        next_pos = n
                    break
            line_end = 1 + canonical_text.count("\n", 0, max(next_pos - 1, hstart))
            cursor = hstart + len(heading)

        sections.append(
            Section(
                heading=heading,
                level=level,
                text=section_text,
                line_start=line_start,
                line_end=line_end,
            )
        )
    return sections


class MdExtractor(IExtractor):
    """Markdown extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/markdown"})
    name: ClassVar[str] = "md"

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
        emit_markdown: bool = False,
        emit_sentences: bool = False,
    ) -> ExtractionResult:
        from collections import Counter

        from knovas_extract._metadata import finalize_warnings, sanitize_scalar

        limits = limits or Limits()
        if len(data) > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(data))

        raw_text, charset = _decode(data)
        meta_raw, body = _split_frontmatter(raw_text)
        text = canonicalize_text(body)
        sections = _extract_sections(body, text)

        # The body already IS markdown (frontmatter stripped). No conversion
        # required — canonicalized text is the answer. Any hostile <script>
        # in the source is a *consumer* problem, not ours (the file's
        # contract with its author is that it's markdown-as-authored).
        markdown = text if emit_markdown else None

        warnings: list[str] = []
        counts: Counter[str] = Counter()

        # Promote known frontmatter keys; everything else under md: namespace,
        # every value routed through sanitize_scalar.
        promoted: dict[str, str | None] = {}
        extra: dict[str, str | int | float | bool | None] = {}
        if charset:
            extra["md:charset_detected"] = charset
        for k, v in meta_raw.items():
            kl = k.lower()
            if kl in _PROMOTED_KEYS:
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                promoted[kl] = str(clean) if clean is not None else None
            else:
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[f"md:frontmatter.{kl}"] = clean
        finalize_warnings(counts, warnings)

        metadata = Metadata(
            title=promoted.get("title"),
            author=promoted.get("author"),
            language=promoted.get("language"),
            created=promoted.get("created") or promoted.get("date"),
            modified=promoted.get("modified"),
            word_count=word_count(text),
            extra=extra,
        )

        sentences = None
        if emit_sentences:
            from knovas_extract._sentences import split_sentences

            sentences = split_sentences(text, limits, warnings=warnings, language=metadata.language)

        return make_result(
            text=text,
            mime="text/markdown",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            sections=sections or None,
            markdown=markdown,
            sentences=sentences,
            warnings=warnings or None,
        )


MIME_REGISTRY["text/markdown"] = MdExtractor()
