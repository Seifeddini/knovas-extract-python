"""markdown-lite layout renderer (``text_mode="layout"``), pure Python.

Renders a page from word boxes into *markdown-lite* — ``#`` headings,
reading-ordered paragraphs, one table row per line with `` | `` cells and
compact-folded numeric cells, ``Key: value`` forms, ``- `` lists — as the page
text itself. A page without detected structure is emitted byte-identical to
plain mode (GI-EXTRACT-03). No third-party import happens at module import
time; PyMuPDF is touched only inside ``words.words_from_fitz_page`` /
``words.vector_rules_from_fitz_page``.

Public API::

    build_page_text(words, page_w, page_h, *, ocr, rules=None, doc_stats=None, opts=None, page_index=0) -> PageLayout
    is_structured(layout) -> bool
    render_page(layout, *, plain_text) -> str
    render_page_detailed(layout, *, plain_text) -> RenderedPage   # text + SectionRecords + parts
    DocumentLayoutPass(opts).fit(layouts) -> DocStats              # furniture, heading ranks, hyphen prefs
"""

from __future__ import annotations

from .hyphen import HYPH_KEEP_NEXT, HyphenPreference, dehyphen_join
from .lint import SENT_SPLIT_RE, check_invariants, est_tokens
from .page import (
    DocStats,
    DocumentLayoutPass,
    LayoutOptions,
    ModeStats,
    PageLayout,
    build_page_text,
    is_structured,
    render_page,
    render_page_detailed,
)
from .render import Part, RenderedPage, SectionRecord
from .words import (
    Rule,
    Word,
    deskew_words,
    normalize_rules,
    vector_rules_from_fitz_page,
    words_from_fitz_page,
    words_from_ocr_rows,
)

__all__ = [
    "HYPH_KEEP_NEXT",
    "SENT_SPLIT_RE",
    "DocStats",
    "DocumentLayoutPass",
    "HyphenPreference",
    "LayoutOptions",
    "ModeStats",
    "PageLayout",
    "Part",
    "RenderedPage",
    "Rule",
    "SectionRecord",
    "Word",
    "build_page_text",
    "check_invariants",
    "dehyphen_join",
    "deskew_words",
    "est_tokens",
    "is_structured",
    "normalize_rules",
    "render_page",
    "render_page_detailed",
    "vector_rules_from_fitz_page",
    "words_from_fitz_page",
    "words_from_ocr_rows",
]
