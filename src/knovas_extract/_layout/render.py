"""Page assembly: structure analysis → blocks → reading order → markdown-lite parts.

Grammar (plan §4, blocks separated by exactly one blank line)::

    page        := block ("\\n\\n" block)*
    block       := passthrough | heading | paragraph | list | kv_block | table
    passthrough := the plain-mode page text verbatim — iff the page is UNSTRUCTURED
    heading     := "#"{1,4} " " text
    paragraph   := one physical line, words joined by single spaces, dehyphenated
    list        := ("- " text "\\n")+
    kv_block    := (key ": " value (" | " key ": " value)* "\\n")+
    table       := pack ("\\n\\n" pack)*        (header repeated on top of every pack)
    section_row := "#### " label
"""

from __future__ import annotations

import re
import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

from .blocks import Block, attach_side_headings, link_blocks
from .headings import classify_heading
from .hyphen import dehyphen_join
from .kv import (
    InlineKv,
    VerticalKv,
    detect_inline_kv,
    detect_vertical_kv,
    is_kv_grid,
    render_kv_grid,
)
from .lint import lint_row_line
from .order import reading_order
from .segments import Segment
from .tables import Run, TableGrid, TablePart, detect_tables, render_table_parts, table_structure
from .words import has_letters

if TYPE_CHECKING:
    from .page import LayoutOptions, ModeStats, PageLayout

BULLET_CHARS = frozenset("•◦▪▫‣∙●○■□➢►–—-*·")  # noqa: RUF001
ENUM_RE = re.compile(
    r"^(?:\(?[a-zA-Z]\)|\(?\d{1,2}\)|\d{1,2}\.(?=\s)|[ivxIVX]{1,4}\)|\(?[ivx]{1,4}\))$"
)
_HEAD_NUM_RE = re.compile(
    r"^(?:§\s?\d+[a-z]?|Art\.?\s?\d+|Ziff(?:er|\.)?\s?\d+|[A-H]\.|[IVX]{1,4}\.|\d{1,2}(?:\.\d{1,2}){0,3}\.?)$"
)
_OCR_BULLETS = frozenset("«»°©eo*")
_DIGIT_TOKEN = re.compile(r"\d")


@dataclass(slots=True)
class Part:
    """One rendered block. ``kind`` ∈ heading | para | list | kv | table | passthrough | margin."""

    kind: str
    text: str
    level: int = 0


@dataclass(slots=True)
class SectionRecord:
    """An emitted ``#`` heading: 0-based line indices into the *page* text (inclusive end)."""

    heading: str
    level: int
    line_start: int
    line_end: int


@dataclass(slots=True)
class RenderedPage:
    text: str
    structured: bool
    parts: list[Part] = field(default_factory=list)
    sections: list[SectionRecord] = field(default_factory=list)
    line_kinds: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PageStructure:
    vlines: list[list[Segment]]
    vertical_kv: list[VerticalKv]
    inline_kv: list[InlineKv]
    tables: list[tuple[Run, TableGrid, bool]]  # (run, grid, is_kv_grid)
    kv_lines: int
    structured: bool


def _pitch(vlines: Sequence[Sequence[Segment]]) -> float:
    ds = [b[0].y0 - a[0].y0 for a, b in pairwise(vlines) if b[0].y0 > a[0].y0]
    return max(st.median(ds), 1.0) if ds else 12.0


def analyze_structure(layout: PageLayout, mode: ModeStats, opts: LayoutOptions) -> PageStructure:
    """Structured-page test (GI-EXTRACT-03): ≥ 1 table region, or ≥ ``min_kv_lines``
    key-value lines, or (OCR pages only) a prose gutter."""
    vl = [[s for s in line if not s.furniture] for line in layout.vlines]
    vl = [line for line in vl if line]
    used: set[int] = set()
    vkv: list[VerticalKv] = []
    ikv: list[InlineKv] = []
    if opts.kv_blocks and vl:
        vkv = detect_vertical_kv(vl, layout.u, _pitch(vl), body_em=mode.body_em, used=used)
        ikv = detect_inline_kv(vl, used)
    masked: list[list[Segment]] = [[s for s in line if id(s) not in used] for line in vl]
    tables: list[tuple[Run, TableGrid, bool]] = []
    if vl:
        for run in detect_tables(
            masked,
            layout.u,
            layout.page_w,
            ocr=layout.ocr,
            hrules=layout.hrules,
            min_lines=opts.min_table_lines,
        ):
            grid = table_structure(run, layout.u, layout.hrules)
            tables.append((run, grid, opts.kv_blocks and is_kv_grid(grid.raw, grid.ncols)))
    kv_lines = (
        len(vkv) + sum(len(k.lines) for k in ikv) + sum(len(g.raw) for _r, g, kv in tables if kv)
    )
    structured = (
        any(not kv for _r, _g, kv in tables)
        or kv_lines >= opts.min_kv_lines
        or (layout.ocr and bool(layout.gutters))
    )
    return PageStructure(vl, vkv, ikv, tables, kv_lines, structured)


def para_split(block: Block, u: float, ocr: bool) -> list[tuple[str, list[Segment]]]:
    """Split a text block's lines into paragraphs (``"p"``) and list items (``"li"``).

    New paragraph on: baseline pitch > 1.25 × the block's median pitch, font change
    (size ±10 % digital / ±35 % OCR, or bold flip on digital), first-line indent
    > 1.5 u, previous line short (< 70 % of the block width) ending in ``.:!?``,
    bullet/enumerator at the line start, a numbered heading line.
    """
    lines = sorted(block.segs, key=lambda s: s.y0)
    width = block.x1 - block.x0
    pitches = [b.y1 - a.y1 for a, b in pairwise(lines)]
    mp = st.median(pitches) if pitches else 0.0

    def sig(s: Segment) -> tuple[float, bool]:
        return (round(s.em() * 2) / 2, s.bold_ratio() >= 0.8)

    def numbered_short(s: Segment) -> bool:
        first = s.words[0].text
        return (
            bool(_HEAD_NUM_RE.match(first))
            and s.n_words <= 8
            and (s.x1 - block.x0) < 0.7 * width
            and not ENUM_RE.match(first)
        )

    paras: list[tuple[str, list[Segment]]] = []
    cur: list[Segment] = []
    cur_kind = "p"
    for i, s in enumerate(lines):
        first = s.words[0].text
        is_bullet = (
            (len(first) == 1 and first in BULLET_CHARS)
            or bool(ENUM_RE.match(first))
            or (
                ocr
                and len(first) == 1
                and first in _OCR_BULLETS
                and s.n_words > 1
                and s.words[0].h < 0.75 * st.median([w.h for w in s.words[1:]])
                and s.words[1].x0 - s.words[0].x1 > 0.8 * u
            )
        )
        brk = False
        if cur:
            prev = lines[i - 1]
            (pe, pb), (ce, cb) = sig(prev), sig(s)
            if (
                mp
                and (s.y1 - prev.y1) > 1.25 * mp
                or abs(pe - ce) > (0.35 if ocr else 0.1) * max(pe, ce)
                or (pb != cb and not ocr)
                or s.x0 - block.x0 > 1.5 * u
                and prev.x0 - block.x0 < 0.5 * u
                and cur_kind != "li"
                or (prev.x1 - block.x0) < 0.7 * width
                and re.search(r"[.:!?]$", prev.text)
                or is_bullet
                or cur_kind == "li"
                and s.x0 < prev.x0 - 2 * u
                or numbered_short(s)
                or (cur_kind == "p" and len(cur) == 1 and numbered_short(cur[0]))
            ):
                brk = True
        if brk and cur:
            paras.append((cur_kind, cur))
            cur = []
        if not cur:
            cur_kind = "li" if is_bullet else "p"
        cur.append(s)
    if cur:
        paras.append((cur_kind, cur))
    return paras


def _list_part(items: Sequence[tuple[str, float, float]]) -> Part:
    min_x = min(x for _t, x, _em in items)
    lines = []
    for text, x0, em in items:
        level = int((x0 - min_x) / max(1.5 * em, 1e-6) + 1e-6)
        lines.append("  " * min(level, 6) + "- " + text)
    return Part("list", "\n".join(lines))


def _strip_bullet(txt: str, first: str, ocr: bool) -> str:
    if (len(first) == 1 and not first.isalnum()) or (
        ocr and len(first) == 1 and first in _OCR_BULLETS
    ):
        return txt[len(first) :].strip()
    return txt


def _letterhead(parts: list[Part], blocks: Sequence[Block], layout: PageLayout) -> None:
    """Plan R1: on page 1 of a structured page the top-band letterhead becomes the first ``# `` line."""
    if layout.page_index != 0 or not parts or parts[0].kind != "para":
        return
    first = parts[0]
    words = first.text.split()
    if (
        not 1 <= len(words) <= 8
        or any(_DIGIT_TOKEN.search(w) for w in words)
        or not has_letters(first.text)
    ):
        return
    top = min((b.y1 for b in blocks if b.kind == "text"), default=layout.page_h)
    if top > 0.12 * layout.page_h:
        return
    parts[0] = Part("heading", "# " + first.text, 1)


def render_structured(
    layout: PageLayout, structure: PageStructure, mode: ModeStats, opts: LayoutOptions
) -> RenderedPage:
    u, ocr = layout.u, layout.ocr
    used: set[int] = set()
    blocks: list[Block] = []
    table_parts: dict[int, list[TablePart]] = {}
    for run, grid, is_kv in structure.tables:
        segs = [s for line in run for s in line]
        used.update(id(s) for s in segs)
        if is_kv:
            blocks.append(Block("kv", segs, text="\n".join(render_kv_grid(grid.raw, grid.ncols))))
        else:
            b = Block("table", segs)
            table_parts[id(b)] = render_table_parts(
                grid,
                budget=opts.pack_budget_tokens,
                row_max_tokens=opts.row_max_tokens,
                row_max_chars=opts.row_max_chars,
                fold=opts.fold,
                sentence_guard=opts.sentence_guard,
                section_rows=opts.section_rows,
            )
            blocks.append(b)
    for vk in structure.vertical_kv:
        used.update(id(s) for s in vk.segs)
        blocks.append(Block("kv", vk.segs, text=vk.text))
    for ik in structure.inline_kv:
        used.update(id(s) for s in ik.segs)
        blocks.append(Block("kv", ik.segs, text="\n".join(ik.lines)))
    rest = [s for line in structure.vlines for s in line if id(s) not in used]
    if rest:
        blocks += link_blocks(rest, u, 0.35 if ocr else 0.2)
    blocks = attach_side_headings(blocks, u)
    parts: list[Part] = []
    pending: list[tuple[str, float, float]] = []

    def flush_list() -> None:
        if pending:
            parts.append(_list_part(pending))
            pending.clear()

    for b in reading_order(blocks):
        for hp in b.head_prefix:
            flush_list()
            parts.append(Part("heading", "#### " + lint_row_line(hp, sentence_guard=False), 4))
        if b.kind == "table":
            flush_list()
            parts.extend(Part(p.kind, p.text, p.level) for p in table_parts[id(b)])
            continue
        if b.kind == "kv":
            flush_list()
            parts.append(
                Part(
                    "kv",
                    "\n".join(
                        lint_row_line(ln, sentence_guard=opts.sentence_guard)
                        for ln in (b.text or "").split("\n")
                    ),
                )
            )
            continue
        paras = para_split(b, u, ocr)
        for pi, (kind, segs) in enumerate(paras):
            txt = dehyphen_join([s.text for s in segs], layout.doc.hyphen if layout.doc else None)
            if not txt:
                continue
            if kind == "li":
                pending.append(
                    (_strip_bullet(txt, segs[0].words[0].text, ocr), segs[0].x0, segs[0].em())
                )
                continue
            flush_list()
            standalone = len(segs) == 1 and pi + 1 < len(paras) and len(paras[pi + 1][1]) >= 2
            lvl = classify_heading(
                segs,
                body_em=mode.body_em,
                body_bold_ratio=mode.body_bold_ratio,
                levels=mode.levels,
                ocr=ocr,
                standalone=standalone,
                isolated=len(b.segs) == 1,
                max_level=opts.max_heading_level,
            )
            if lvl:
                parts.append(Part("heading", "#" * lvl + " " + txt, lvl))
            else:
                parts.append(Part("para", txt))
        flush_list()
    if opts.letterhead_heading:
        _letterhead(parts, blocks, layout)
    if layout.rotated_words:
        parts.append(
            Part(
                "margin",
                " ".join(
                    w.text for w in sorted(layout.rotated_words, key=lambda w: (round(w.x0), w.y0))
                ),
            )
        )
    if opts.headings == "none":
        parts = [
            Part("para", p.text.lstrip("#").strip()) if p.kind == "heading" else p for p in parts
        ]
    return assemble(parts, structured=True)


def assemble(parts: Sequence[Part], *, structured: bool) -> RenderedPage:
    """Join parts with one blank line, drop empty lines, compute line kinds and sections."""
    clean: list[Part] = []
    for p in parts:
        lines = [
            (ln.rstrip() if p.kind == "list" else ln.strip())  # list nesting keeps its indent
            for ln in p.text.split("\n")
            if ln.strip()
        ]
        if lines:
            clean.append(Part(p.kind, "\n".join(lines), p.level))
    text = "\n\n".join(p.text for p in clean)
    kinds: list[str] = []
    sections: list[SectionRecord] = []
    for i, p in enumerate(clean):
        n = p.text.count("\n") + 1
        if p.kind == "heading":
            sections.append(
                SectionRecord(p.text.lstrip("#").strip(), p.level, len(kinds), len(kinds))
            )
        kinds.extend([p.kind] * n)
        if i + 1 < len(clean):
            kinds.append("blank")
    total = len(kinds)
    for k, sec in enumerate(sections):
        end = total - 1
        for nxt in sections[k + 1 :]:
            if nxt.level <= sec.level:
                end = nxt.line_start - 1
                break
        while end > sec.line_start and kinds[end] == "blank":
            end -= 1
        sec.line_end = end
    return RenderedPage(text, structured, clean, sections, kinds)


def passthrough(plain_text: str) -> RenderedPage:
    """GI-EXTRACT-03: an unstructured page is the plain-mode text, byte for byte."""
    n = plain_text.count("\n") + 1 if plain_text else 0
    return RenderedPage(
        plain_text,
        False,
        [Part("passthrough", plain_text)] if plain_text else [],
        [],
        ["passthrough"] * n,
    )
