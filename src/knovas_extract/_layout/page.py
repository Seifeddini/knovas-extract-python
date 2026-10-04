"""Public entry points of the layout renderer (``text_mode="layout"``).

Typical use (what ``PdfExtractor.extract`` wires)::

    layouts = [build_page_text(words, w, h, ocr=is_ocr_page, rules=rules, page_index=i) …]
    doc_pass = DocumentLayoutPass()
    doc_pass.fit(layouts)                       # furniture, heading ranks, hyphen vocabulary
    for layout, plain in zip(layouts, plain_texts):
        text = render_page(layout, plain_text=plain)   # == plain when the page is unstructured
        sections = layout.rendered.sections            # SectionRecord per emitted '#' line

Every function is deterministic: identical input → identical bytes, and the
per-page result does not depend on worker count because the document pass
only reads the pages' words/segments.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .furniture import detect_furniture
from .gutters import detect_gutters
from .headings import body_stats, heading_levels
from .hyphen import HyphenPreference
from .lines import build_visual_lines
from .render import (
    PageStructure,
    RenderedPage,
    SectionRecord,
    analyze_structure,
    passthrough,
    render_structured,
)
from .segments import Segment, build_segments
from .words import Rule, RuleLike, Word, normalize_rules, page_unit

HeadingPolicy = Literal["structured_only", "none"]


@dataclass(frozen=True, slots=True)
class LayoutOptions:
    """Renderer knobs. Defaults are the plan §4 grammar."""

    pack_budget_tokens: int = (
        150  # D14: 248 − hc_max; raised per tenant once the server subtracts hc
    )
    row_max_tokens: int = 200
    row_max_chars: int = 1800
    headings: HeadingPolicy = "structured_only"
    fold: bool = True  # compact fold keys for numeric/date cells
    sentence_guard: bool = True  # R8: remove server sentence-boundary whitespace inside rows
    furniture: bool = True
    kv_blocks: bool = True
    section_rows: bool = True
    letterhead_heading: bool = True
    min_table_lines: int = 3
    min_kv_lines: int = 3
    max_heading_level: int = 4


@dataclass(slots=True)
class ModeStats:
    """Body font statistics for one modality (digital font sizes or OCR line heights)."""

    body_em: float = 10.0
    body_bold_ratio: float = 0.0
    levels: dict[float, int] = field(default_factory=dict)


@dataclass(slots=True)
class DocStats:
    """Document-level statistics shared by every page (set by :class:`DocumentLayoutPass`)."""

    digital: ModeStats = field(default_factory=ModeStats)
    ocr: ModeStats = field(default_factory=ModeStats)
    hyphen: HyphenPreference = field(default_factory=HyphenPreference)
    n_pages: int = 1
    furniture_marked: int = 0

    def mode(self, ocr: bool) -> ModeStats:
        return self.ocr if ocr else self.digital

    @classmethod
    def from_layouts(cls, layouts: Sequence[PageLayout]) -> DocStats:
        stats = cls(n_pages=len(layouts))
        for is_ocr in (False, True):
            sizes: Counter[float] = Counter()
            bold = 0
            for lay in layouts:
                if lay.ocr != is_ocr:
                    continue
                for w in lay.words:
                    if w.rotated:
                        continue
                    sizes[round(w.size * 2) / 2] += len(w.text)
                    if w.bold:
                        bold += len(w.text)
            if sizes:
                body_em, ratio = body_stats(sizes, bold)
                ms = ModeStats(body_em, ratio, heading_levels(sizes, body_em, ocr=is_ocr))
                if is_ocr:
                    stats.ocr = ms
                else:
                    stats.digital = ms
        stats.hyphen.add(w.text for lay in layouts for w in lay.words)
        return stats


@dataclass(slots=True)
class PageLayout:
    """Pass-1 result for one page: words → unit, gutters, visual lines, segments."""

    words: list[Word]
    page_w: float
    page_h: float
    ocr: bool
    rules: list[Rule]
    u: float
    gutters: list[tuple[float, float]]
    segs: list[Segment]
    vlines: list[list[Segment]]
    rotated_words: list[Word]
    opts: LayoutOptions
    page_index: int = 0
    doc: DocStats | None = None
    structure: PageStructure | None = None
    rendered: RenderedPage | None = None

    @property
    def hrules(self) -> list[Rule]:
        return [r for r in self.rules if r.kind == "h"]

    @property
    def vrules(self) -> list[Rule]:
        return [r for r in self.rules if r.kind == "v"]

    @property
    def sections(self) -> list[SectionRecord]:
        return list(self.rendered.sections) if self.rendered else []


def build_page_text(
    words: Sequence[Word],
    page_w: float,
    page_h: float,
    *,
    ocr: bool,
    rules: Iterable[RuleLike] | None = None,
    doc_stats: DocStats | None = None,
    opts: LayoutOptions | None = None,
    page_index: int = 0,
) -> PageLayout:
    """Pass 1 for one page. *words* come from :func:`~.words.words_from_fitz_page` or
    :func:`~.words.words_from_ocr_rows`; *rules* are vector/raster rulings (``Rule``,
    ``(kind, x0, y0, x1, y1)``, ``{"kind", "bbox"}`` or bare horizontal y values).
    """
    opts = opts or LayoutOptions()
    rl = normalize_rules(rules)
    live = [w for w in words if w.text.strip()]
    rotated = [w for w in live if w.rotated]
    flat = [w for w in live if not w.rotated]
    u = page_unit(flat) if flat else 4.0
    gutters: list[tuple[float, float]] = []
    segs: list[Segment] = []
    vlines: list[list[Segment]] = []
    if flat:
        gutters = detect_gutters([vl.words for vl in build_visual_lines(flat)], u, page_w)
        segs, vlines = build_segments(
            flat, u, gutters=gutters, ocr=ocr, vrules=[r for r in rl if r.kind == "v"]
        )
    return PageLayout(
        list(live),
        page_w,
        page_h,
        ocr,
        rl,
        u,
        gutters,
        segs,
        vlines,
        rotated,
        opts,
        page_index,
        doc_stats,
    )


class DocumentLayoutPass:
    """Document-level pass: furniture detection across pages, heading-level ranking,
    hyphen spelling preference. ``fit`` attaches the resulting :class:`DocStats` to
    every layout; ``render`` is a convenience over :func:`render_page`."""

    def __init__(self, opts: LayoutOptions | None = None) -> None:
        self.opts = opts or LayoutOptions()
        self.stats: DocStats | None = None

    def fit(self, layouts: Sequence[PageLayout]) -> DocStats:
        for lay in layouts:
            for s in lay.segs:
                s.furniture = False
            lay.structure = None
            lay.rendered = None
        stats = DocStats.from_layouts(layouts)
        if self.opts.furniture and layouts:
            stats.furniture_marked = detect_furniture(
                [lay.vlines for lay in layouts], [lay.page_h for lay in layouts]
            )
        for lay in layouts:
            lay.doc = stats
        self.stats = stats
        return stats

    def render(
        self, layouts: Sequence[PageLayout], plain_texts: Sequence[str]
    ) -> list[RenderedPage]:
        if self.stats is None or any(lay.doc is not self.stats for lay in layouts):
            self.fit(layouts)
        return [
            render_page_detailed(lay, plain_text=pt)
            for lay, pt in zip(layouts, plain_texts, strict=True)
        ]


def _ensure_doc(layout: PageLayout) -> DocStats:
    if layout.doc is None:
        DocumentLayoutPass(layout.opts).fit([layout])
    doc = layout.doc
    if doc is None:  # fit() always sets it; checked explicitly so -O cannot drop it
        raise RuntimeError("layout: document statistics missing after fit()")
    return doc


def _ensure_structure(layout: PageLayout) -> PageStructure:
    if layout.structure is None:
        doc = _ensure_doc(layout)
        layout.structure = analyze_structure(layout, doc.mode(layout.ocr), layout.opts)
    return layout.structure


def is_structured(layout: PageLayout) -> bool:
    """GI-EXTRACT-03 structured-page test: ≥ 1 table region, or ≥ 3 key-value lines,
    or (OCR pages only) a prose gutter."""
    return _ensure_structure(layout).structured


def render_page_detailed(layout: PageLayout, *, plain_text: str) -> RenderedPage:
    """Render one page; an unstructured page is *plain_text* verbatim (never cached, so a
    later call may pass a different plain text)."""
    structure = _ensure_structure(layout)
    if not structure.structured:
        layout.rendered = passthrough(plain_text)
        return layout.rendered
    if layout.rendered is None or not layout.rendered.structured:
        doc = _ensure_doc(layout)
        layout.rendered = render_structured(layout, structure, doc.mode(layout.ocr), layout.opts)
    return layout.rendered


def render_page(layout: PageLayout, *, plain_text: str) -> str:
    """The page's ``Page.text`` in layout mode (``plain_text`` verbatim when unstructured).
    ``layout.rendered.sections`` holds the :class:`SectionRecord` list afterwards."""
    return render_page_detailed(layout, plain_text=plain_text).text
