"""Sentence tokenization + line/char mapping — pysbd backend.

pysbd is pure-Python (MIT). It ships 22 language rule sets, no data
download. Deterministic across runs (no randomness, no time).

Contract (see docs/citations.md for the full reference):
  - `content.text[sentence.char_start:sentence.char_end] == sentence.text`
  - `line_start` / `line_end` are 1-based indices into the text they were
    computed from (so `line_start = 1 + text[:char_start].count("\\n")`).
  - Sentences are ordered and non-overlapping; `index` is monotonic 0-based.
  - Empty text → `[]` (never None).

The consumer-facing dispatch layer enforces additional guarantees
(sentence↔page, sentence↔section back-pointer). This module produces the
raw sentences; dispatch stitches page/section coords on top.

Security posture:
  - No network. pysbd is pure-Python — validated by
    `tests/property/test_network_isolation.py`.
  - ReDoS: pysbd is regex-heavy. `Limits.max_text_bytes` caps input
    upstream; `Limits.max_sentences` caps output. The fuzz harness
    (`tests/fuzz/fuzz_sentences.py`) runs with a 30 s per-call wall-clock
    timeout to catch regressions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from knovas_extract.errors import DependencyMissingError, ResourceExhaustedError
from knovas_extract.result import Sentence

if TYPE_CHECKING:
    from knovas_extract.result import Limits, Page, Section


# pysbd's Segmenter accepts many language codes. Keep the intersection
# of "commonly seen in Metadata.language" and "pysbd supports" small; on
# mismatch we fall back to English rather than crashing.
_PYSBD_LANGS = frozenset(
    {
        "en",
        "hi",
        "mr",
        "zh",
        "es",
        "am",
        "ar",
        "hy",
        "bg",
        "ur",
        "ru",
        "pl",
        "fa",
        "nl",
        "da",
        "fr",
        "it",
        "el",
        "my",
        "ja",
        "de",
        "kk",
        "sk",
    }
)


def _resolve_language(language: str | None) -> str:
    if not language:
        return "en"
    # `en-US` → `en`.
    code = language.strip().lower().split("-", 1)[0].split("_", 1)[0]
    return code if code in _PYSBD_LANGS else "en"


def split_sentences(
    text: str,
    limits: Limits,
    *,
    warnings: list[str],
    language: str | None = None,
    page_line_offset: int = 0,
    char_offset: int = 0,
    page_index: int | None = None,
    start_index: int = 0,
) -> list[Sentence]:
    """Split ``text`` into Sentence records with 1-based line coordinates.

    Coordinates account for ``page_line_offset`` (added to computed line
    numbers) and ``char_offset`` (added to computed char offsets). These
    are used by ``split_sentences_for_pages`` when stitching per-page
    tokenization into document-level coordinates.

    ``start_index`` is the ``index`` value assigned to the first sentence
    (used for the same stitching path).

    Raises ``DependencyMissingError`` if pysbd is unavailable,
    ``ResourceExhaustedError`` on ``max_sentences`` overflow, and
    ``RuntimeError`` on a producer-side invariant violation.
    """
    if not text:
        return []

    try:
        import pysbd
    except ImportError as exc:
        raise DependencyMissingError("sentences", "pysbd") from exc

    lang = _resolve_language(language)
    seg = pysbd.Segmenter(language=lang, clean=False)
    # pysbd has no type stubs; segment() returns list[str] at runtime with
    # clean=False. cast keeps pyright's second-opinion pass green (mypy treats
    # pysbd as Any via [[tool.mypy.overrides]]).
    raw_segments: list[str] = cast("list[str]", seg.segment(text))

    result: list[Sentence] = []
    cursor = 0
    missing = 0

    for raw in raw_segments:
        segment = raw.strip()
        if not segment:
            continue

        # Locate this segment in the source starting from cursor.
        loc = text.find(segment, cursor)
        if loc < 0:
            # pysbd occasionally normalizes whitespace inside a segment.
            # Try locating the first token instead — if that fails too, skip.
            first_token = segment.split(None, 1)[0] if segment else ""
            loc = text.find(first_token, cursor) if first_token else -1
            if loc < 0:
                missing += 1
                continue

        char_start = loc
        char_end = loc + len(segment)
        cursor = char_end

        line_start = 1 + text.count("\n", 0, char_start) + page_line_offset
        line_end = 1 + text.count("\n", 0, char_end) + page_line_offset

        result.append(
            Sentence(
                index=start_index + len(result),
                text=segment,
                char_start=char_start + char_offset,
                char_end=char_end + char_offset,
                line_start=line_start,
                line_end=line_end,
                page_index=page_index,
                page_number=(page_index + 1) if page_index is not None else None,
            )
        )

        if len(result) > limits.max_sentences:
            raise ResourceExhaustedError(
                "sentence count", limits.max_sentences, observed=len(result)
            )

    if missing:
        warnings.append(f"sentences: {missing} segments could not be located")

    _assert_invariants(result, text, char_offset=char_offset, page_line_offset=page_line_offset)
    return result


def _assert_invariants(
    sentences: list[Sentence], text: str, *, char_offset: int, page_line_offset: int
) -> None:
    """Producer-side asserts. Violation = producer bug, not document bug."""
    max_line = text.count("\n") + 1 + page_line_offset
    for i, s in enumerate(sentences):
        # Char-offset window inside the tokenized text (undo char_offset).
        local_start = s.char_start - char_offset
        local_end = s.char_end - char_offset
        if not (0 <= local_start < local_end <= len(text)):
            raise RuntimeError(
                f"sentence {s.index}: char window {local_start}..{local_end} "
                f"out of bounds for text of length {len(text)}"
            )
        if text[local_start:local_end] != s.text:
            raise RuntimeError(f"sentence {s.index}: exact-retrieval invariant violated")
        if s.line_start < 1 or s.line_end < s.line_start or s.line_end > max_line:
            raise RuntimeError(
                f"sentence {s.index}: line coords {s.line_start}..{s.line_end} "
                f"out of bounds (max line {max_line})"
            )
        if i > 0 and s.char_start < sentences[i - 1].char_end:
            raise RuntimeError(
                f"sentence {s.index}: char_start overlaps predecessor "
                f"(predecessor char_end={sentences[i - 1].char_end})"
            )


def split_sentences_for_pages(
    pages: list[Page],
    document_text: str,
    limits: Limits,
    *,
    warnings: list[str],
    language: str | None = None,
) -> list[Sentence]:
    """Tokenize each page and stitch into document-level coordinates.

    `document_text` is the joined `content.text` — pages are joined with
    `"\\n\\n"` by the PDF extractor, so we count that separator to keep
    char + line offsets aligned with `content.text`.

    Every returned sentence carries `page_index` (and thus `page_number`).
    """
    if not pages:
        return []

    result: list[Sentence] = []
    cursor_char = 0  # char offset into document_text

    for page in pages:
        page_text = page.text
        if not page_text:
            # Empty page still consumes coordinates (the join separator).
            if cursor_char and cursor_char < len(document_text):
                cursor_char += len("\n\n")
            continue

        # Locate this page's text in document_text starting at cursor_char.
        # PDF's canonicalize_text may reflow whitespace, so we search
        # forward from cursor rather than assuming exact concatenation.
        loc = document_text.find(page_text, cursor_char)
        if loc < 0:
            # Fall back: skip this page for sentence extraction rather
            # than emit invalid coords. A warning captures the issue.
            warnings.append("sentences: page text could not be aligned to document")
            continue

        page_line_offset = document_text.count("\n", 0, loc)

        page_sentences = split_sentences(
            page_text,
            limits,
            warnings=warnings,
            language=language,
            page_line_offset=page_line_offset,
            char_offset=loc,
            page_index=page.index,
            start_index=len(result),
        )
        result.extend(page_sentences)
        cursor_char = loc + len(page_text)

    return result


def attach_section_indices(
    sentences: list[Sentence] | None,
    sections: list[Section] | None,
) -> None:
    """Back-fill `section_index` on each sentence.

    A sentence gets `section_index = i` when `sections[i].line_start <=
    sentence.line_start <= sections[i].line_end`. When multiple sections
    match (nested headings), we pick the **most specific** — smallest
    line window — so sentences inside a subsection point to the
    subsection, not the enclosing parent.

    Sections without line coords are skipped. Sentences before the first
    heading keep `section_index = None`.

    Enforces the `0 <= section_index < len(sections)` invariant.
    """
    if not sentences or not sections:
        return

    windows: list[tuple[int, int, int]] = []
    for i, sec in enumerate(sections):
        if sec.line_start is None or sec.line_end is None:
            continue
        windows.append((sec.line_start, sec.line_end, i))

    if not windows:
        return

    n = len(sections)

    for s in sentences:
        best_i: int | None = None
        best_span = -1
        for start, end, i in windows:
            if start <= s.line_start <= end:
                span = end - start
                # Smaller span == more specific; pick the smallest.
                if best_i is None or span < best_span:
                    best_i = i
                    best_span = span
        if best_i is not None:
            if not (0 <= best_i < n):
                raise RuntimeError(
                    f"sentence {s.index}: section_index {best_i} out of range [0,{n})"
                )
            s.section_index = best_i
