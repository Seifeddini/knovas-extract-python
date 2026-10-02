"""Wiring of the markdown-lite renderer into the PDF extractor (``text_mode="layout"``).

`extractors/pdf.py` collects, per page, the plain-mode text plus the word
boxes that produced it (the born-digital text layer through the very same
PyMuPDF text page, or the OCR words of a scanned page) and hands them to
:func:`render_layout_pages`, which runs the document pass and renders every
page. The renderer output *is* the page text (plan §4 "carrier"): the
extractor canonicalises and joins it exactly as in plain mode, so every
offset / sentence contract applies unchanged.

GI-EXTRACT-03: an unstructured page comes back as the plain-mode text it was
given, byte for byte; a structured page carries ``#`` headings and `` | ``
rows. The library never emits ``\\f``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from knovas_extract._layout import (
    DocumentLayoutPass,
    LayoutOptions,
    Rule,
    SectionRecord,
    Word,
    build_page_text,
    render_page_detailed,
    words_from_ocr_rows,
)
from knovas_extract._layout.words import is_amount, is_dateyear, is_numlike
from knovas_extract.normalize import canonicalize_text, word_count

TextModeT = Literal["plain", "layout"]
TEXT_MODES: tuple[str, ...] = ("plain", "layout")

# A folded numeric cell (`2023: 1'234.00`, `Anhang: 2.1`): a short key, ": ", a
# numeric / date value. Only ever produced by the table renderer (fold keys are
# copies of header words), so stripping it restores the plain-mode word count.
_FOLD_CELL_RE = re.compile(r"^(\S+(?: \S+){0,2}): (.+)$")
_BULLET_RE = re.compile(r"^\s*- ")


def validate_text_mode(value: object) -> TextModeT:
    """``"plain"`` / ``"layout"`` or `ValueError` (never silently another mode)."""
    if value == "plain":
        return "plain"
    if value == "layout":
        return "layout"
    raise ValueError(f"text_mode must be one of {', '.join(TEXT_MODES)}; got {value!r}")


@dataclass(slots=True)
class LayoutPageInput:
    """One page as the extractor collected it, before the document pass."""

    index: int
    page_w: float
    page_h: float
    plain_text: str  # the plain-mode page text (raw, not yet canonicalised)
    words: list[Word] = field(default_factory=list)
    ocr: bool = False  # words came from OCR (conservative heading policy, gutters count)
    rules: list[Rule] = field(default_factory=list)
    lead_text: str = ""  # a kept text layer (scanner stamp) prepended on a structured OCR page


@dataclass(slots=True)
class LayoutPageOutput:
    """The page text in layout mode plus what the extractor reports about it."""

    text: str  # raw page text; == ``plain_text`` when the page is unstructured
    structured: bool
    sections: list[SectionRecord] = field(default_factory=list)  # 0-based lines into ``text``
    tables: int = 0  # table regions rendered on this page (key-value grids excluded)
    word_count: int = 0  # whitespace tokens of the markup-stripped text


def ocr_words_to_rows(words: Iterable[Any]) -> list[dict[str, Any]]:
    """`OcrWord` records (boxes already in page points, mapped by the backend through
    ``frame_to_page``) → the row mappings :func:`words_from_ocr_rows` accepts. A
    negative confidence means "not reported" (MuPDF's OCR, cached text-only results)
    and is treated as trusted so short tokens such as ``AG`` / ``CHF`` are kept."""
    rows: list[dict[str, Any]] = []
    for w in words:
        conf = float(w.conf)
        rows.append(
            {
                "text": w.text,
                "bbox": [float(w.x0), float(w.y0), float(w.x1), float(w.y1)],
                "conf": conf if conf >= 0 else 100.0,
                "block": int(w.block),
                "par": int(w.par),
                "line": int(w.line),
            }
        )
    return rows


def layout_words_from_ocr(words: Iterable[Any]) -> list[Word]:
    """Layout words for an OCR'd page (page points, scale 1.0 — the OCR backends map
    frame pixels to points before returning, see ``_ocr.backend.finish_words``)."""
    return words_from_ocr_rows(ocr_words_to_rows(words), scale=1.0)


def _numeric_value(v: str) -> bool:
    toks = v.split()
    return bool(toks) and all(is_amount(t) or is_dateyear(t) or is_numlike(t) for t in toks)


def strip_line_markup(line: str, kind: str) -> str:
    """Remove the markdown-lite markup of one rendered line so a word count over the
    result matches plain mode: ``#`` prefixes, ``- `` bullets, `` | `` separators and —
    on table lines only — the compact fold keys of numeric cells."""
    if kind == "heading":
        return line.lstrip("#").strip()
    if kind == "list":
        return _BULLET_RE.sub("", line, count=1)
    if " | " not in line:
        return line
    cells = line.split(" | ")
    if kind == "table":
        out: list[str] = []
        for cell in cells:
            m = _FOLD_CELL_RE.match(cell)
            out.append(m.group(2) if m and _numeric_value(m.group(2)) else cell)
        cells = out
    return " ".join(c for c in cells if c and c != "-")


def strip_layout_markup(text: str, kinds: Sequence[str] | None = None) -> str:
    """Markup-stripped page text (``kinds`` are the renderer's per-line kinds; without
    them every line is treated as a table/row line). The header line a table repeats
    at the top of every pack is kept once, as the plain text has it once."""
    lines = text.split("\n")
    if kinds is None or len(kinds) != len(lines):
        kinds = ["table"] * len(lines)
    stripped: list[str] = []
    seen_headers: set[str] = set()
    for i, (ln, kind) in enumerate(zip(lines, kinds, strict=True)):
        k = kind
        if k == "table" and ln.startswith("#"):
            k = "heading"
        if k == "table" and (i == 0 or kinds[i - 1] != "table"):  # first line of a pack
            if ln in seen_headers:
                continue
            seen_headers.add(ln)
        stripped.append(strip_line_markup(ln, k))
    return "\n".join(stripped)


def render_layout_pages(
    inputs: Sequence[LayoutPageInput], opts: LayoutOptions | None = None
) -> list[LayoutPageOutput]:
    """Document pass + per-page rendering. Deterministic: identical inputs → identical
    outputs, independent of how the OCR words were produced (worker count)."""
    layouts = [
        build_page_text(
            inp.words,
            inp.page_w,
            inp.page_h,
            ocr=inp.ocr,
            rules=inp.rules,
            opts=opts,
            page_index=inp.index,
        )
        for inp in inputs
    ]
    DocumentLayoutPass(opts).fit(layouts)
    outputs: list[LayoutPageOutput] = []
    for inp, lay in zip(inputs, layouts, strict=True):
        rp = render_page_detailed(lay, plain_text=inp.plain_text)
        if not rp.structured:
            outputs.append(
                LayoutPageOutput(inp.plain_text, False, word_count=word_count(inp.plain_text))
            )
            continue
        text = rp.text
        offset = 0
        lead = canonicalize_text(inp.lead_text)
        if lead:
            text = f"{lead}\n\n{text}" if text else lead
            offset = lead.count("\n") + 2 if rp.text else 0
        sections = [
            SectionRecord(s.heading, s.level, s.line_start + offset, s.line_end + offset)
            for s in rp.sections
        ]
        tables = 0
        if lay.structure is not None:
            tables = sum(1 for _run, _grid, kv in lay.structure.tables if not kv)
        stripped = strip_layout_markup(rp.text, rp.line_kinds)
        outputs.append(
            LayoutPageOutput(
                text,
                True,
                sections,
                tables,
                word_count(stripped) + word_count(lead),
            )
        )
    return outputs
