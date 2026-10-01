"""Word boxes, rulings and token classifiers — the input side of the layout renderer.

Everything in ``_layout`` works on :class:`Word` records (page points, y down). Two
adapters produce them: :func:`words_from_fitz_page` for a born-digital text layer
(PyMuPDF is imported *inside* that function only) and :func:`words_from_ocr_rows`
for Tesseract TSV-style rows (``text, bbox, conf, block, par, line``).

Token classifiers (``is_amount`` …) live here because every later stage needs
them and ``segments``/``tables`` must not import each other circularly.
"""

from __future__ import annotations

import re
import statistics as st
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ── token classifiers ──────────────────────────────────────────────────────

AMOUNT_RE = re.compile(
    r"^[(\-–−+]?(?:CHF|Fr\.|EUR|€|\$)?\s?[(\-–−]?\d{1,3}(?:[’'`´ ]\d{3})*(?:[.,]\d{1,2})?[)%]?$"  # noqa: RUF001
    r"|^[(\-–−]?\d+(?:[.,]\d{1,2})[)%]?$|^[-–—]{1,2}$"  # noqa: RUF001
)
OCR_AMOUNT_RE = re.compile(r"^[(\-–−+_.]?\d{1,3}(?:[’'`´:,. ]\d{3})*(?:[.,:]\d{1,2})[.:,°)%]?$")  # noqa: RUF001
DATEYEAR_RE = re.compile(r"^(?:\d{1,2}\.\d{1,2}\.(?:19|20)?\d{2}|(?:19|20)\d{2}(?:/\d{2,4})?)$")
NUMLIKE_RE = re.compile(r"^[(\-–−+]?[\d'’.,/%]+[)%]?$")  # noqa: RUF001
LEADER_RE = re.compile(r"^(?:[.·…_]\s?){3,}$|^[-–]{4,}$")  # noqa: RUF001
TRAIL_LEADER_RE = re.compile(r"(.*?[^.\s])\s?(?:[.·…_]\s?){3,}$")
CURRENCY_TOKENS = frozenset({"CHF", "Fr.", "EUR", "€", "$", "USD"})
_WORD_CHAR_RE = re.compile(r"\w")
_DIGIT_RE = re.compile(r"\d")
_LETTERS3_RE = re.compile(r"[A-Za-zÄÖÜäöüßÀ-ÿ]{3}")

#: OCR tokens that are kept even when short and low-confidence.
KEEP_SHORT = frozenset(
    {"a", "à", "e", "i", "o", "y", "u", "&", "%", "-", "–", "§", "/", "+", "=", "€", "$", "*", "•"}  # noqa: RUF001
)
_GARBAGE = frozenset({"|", "||", "_", "__", "—|", "|—"})


def is_amount(t: str) -> bool:
    """Amount-like token (Swiss/EU formats, OCR-tolerant separators)."""
    return bool(AMOUNT_RE.match(t) or OCR_AMOUNT_RE.match(t))


def is_dateyear(t: str) -> bool:
    return bool(DATEYEAR_RE.match(t))


def is_numlike(t: str) -> bool:
    return bool(NUMLIKE_RE.match(t))


def has_letters(t: str) -> bool:
    """At least three consecutive letters somewhere in *t*."""
    return bool(_LETTERS3_RE.search(t))


# ── records ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Word:
    """One word box in page points (origin top-left, y down).

    ``size`` is the em estimate: the span font size for digital words, the
    height of the word's line run for OCR words. ``block``/``par``/``line`` are
    the producing engine's ids (Tesseract block/paragraph/line, PyMuPDF
    block/line) and act as a secondary column cue on OCR pages.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool = False
    conf: float = 100.0
    block: int = -1
    par: int = -1
    line: int = -1
    rotated: bool = False

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def cw(self) -> float:
        """Mean character width."""
        return (self.x1 - self.x0) / max(1, len(self.text))

    @property
    def line_id(self) -> tuple[int, int, int]:
        return (self.block, self.par, self.line)


@dataclass(frozen=True, slots=True)
class Rule:
    """A ruling line (vector drawing or raster rule box). ``kind`` is ``"h"`` or ``"v"``."""

    kind: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def y(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def x(self) -> float:
        return (self.x0 + self.x1) / 2


RuleLike = Rule | float | int | Sequence[Any] | Mapping[str, Any]


def normalize_rules(rules: Iterable[RuleLike] | None) -> list[Rule]:
    """Accept ``Rule`` objects, plain y-values (horizontal rules spanning the page),
    ``(kind, x0, y0, x1, y1)`` tuples or ``{"kind":…, "bbox": […]}`` mappings."""
    out: list[Rule] = []
    if rules is None:
        return out
    for r in rules:
        if isinstance(r, Rule):
            out.append(r)
        elif isinstance(r, int | float):
            out.append(Rule("h", float("-inf"), float(r), float("inf"), float(r)))
        elif isinstance(r, Mapping):
            bb = r["bbox"]
            out.append(
                Rule(
                    str(r.get("kind", "h")), float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
                )
            )
        else:
            kind, x0, y0, x1, y1 = r
            out.append(Rule(str(kind), float(x0), float(y0), float(x1), float(y1)))
    return sorted(out, key=lambda r: (r.kind, r.y, r.x))


def page_unit(words: Sequence[Word]) -> float:
    """Layout unit *u* = median character width of words with ≥ 3 characters (fallback 4 pt)."""
    cws = [w.cw() for w in words if len(w.text) >= 3 and not w.rotated]
    return st.median(cws) if cws else 4.0


# ── OCR rows ───────────────────────────────────────────────────────────────


def _ocr_garbage(t: str, conf: float) -> bool:
    if t in _GARBAGE:
        return True
    if len(t) <= 3 and conf < 50 and not _DIGIT_RE.search(t) and t.lower() not in KEEP_SHORT:
        return True
    return bool(not _WORD_CHAR_RE.search(t) and conf < 60 and t not in KEEP_SHORT)


def words_from_ocr_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    scale: float = 1.0,
    min_conf: float = 0.0,
    deskew: bool = True,
) -> list[Word]:
    """Build words from Tesseract word rows (level-5 TSV or the saved JSON dumps).

    Each row needs ``text`` and either ``bbox=[x0,y0,x1,y1]`` or ``left/top/width/height``
    (multiplied by *scale* to reach page points), optionally ``conf``, ``block``,
    ``par``, ``line``. The garbage filter drops ruling artefacts and short
    low-confidence tokens. The em of a word is the median height of the words of
    its engine line *run* (words separated by less than 6 char widths), so a line
    Tesseract merged across a wide gap (letterhead left + address right) does not
    share one em.
    """
    raw: list[Word] = []
    for r in rows:
        t = str(r.get("text") or "").strip()
        conf_v = r.get("conf")
        conf = float(conf_v) if conf_v is not None else 100.0
        if not t or _ocr_garbage(t, conf) or conf < min_conf:
            continue
        if "bbox" in r:
            x0, y0, x1, y1 = (float(v) * scale for v in r["bbox"])
        else:
            left, top = float(r["left"]) * scale, float(r["top"]) * scale
            x0, y0 = left, top
            x1, y1 = left + float(r["width"]) * scale, top + float(r["height"]) * scale
        raw.append(
            Word(
                x0,
                y0,
                x1,
                y1,
                t,
                max(y1 - y0, 0.1),
                False,
                conf,
                int(r.get("block", -1)),
                int(r.get("par", -1)),
                int(r.get("line", -1)),
            )
        )
    if not raw:
        return []
    byline: dict[tuple[int, int, int], list[Word]] = defaultdict(list)
    for w in raw:
        byline[w.line_id].append(w)
    cws = [w.cw() for w in raw if len(w.text) >= 3]
    cw = st.median(cws) if cws else 4.0
    for group in byline.values():
        group.sort(key=lambda w: w.x0)
        run: list[Word] = []
        for w in group:
            if run:
                ry0, ry1 = min(r.y0 for r in run), max(r.y1 for r in run)
                ov = min(ry1, w.y1) - max(ry0, w.y0)
                stacked = ov < 0.5 * min(ry1 - ry0, w.h)  # engine merged two stacked lines
                if w.x0 - run[-1].x1 > 6 * cw or stacked:
                    _assign_run_em(run)
                    run = []
            run.append(w)
        _assign_run_em(run)
    return deskew_words(raw) if deskew else raw


def _assign_run_em(run: list[Word]) -> None:
    if not run:
        return
    hs = [w.h for w in run if len(w.text) >= 2] or [w.h for w in run]
    em = st.median(hs)
    for w in run:
        w.size = em


def deskew_words(words: list[Word]) -> list[Word]:
    """Shear-correct residual skew: median slope of per-engine-line regressions of
    word bottoms on x; ignored below 0.09°. Mutates and returns *words*."""
    groups: dict[tuple[int, int, int], list[Word]] = defaultdict(list)
    for w in words:
        groups[w.line_id].append(w)
    slopes: list[float] = []
    for g in groups.values():
        if len(g) < 4:
            continue
        xs = [w.xc for w in g]
        if max(xs) - min(xs) < 100:
            continue
        ys = [w.y1 for w in g]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 0:
            continue
        slopes.append(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx)
    if not slopes:
        return words
    a = st.median(slopes)
    if abs(a) < 0.0015:
        return words
    for w in words:
        dy = -a * w.xc
        w.y0 += dy
        w.y1 += dy
    return words


# ── PyMuPDF adapters (import inside the functions only) ────────────────────

_BOLD_FONT_RE = re.compile(r"bold|black|heavy|semibold|demi", re.I)


def words_from_fitz_page(page: Any, textpage: Any = None) -> list[Word]:
    """Words of a born-digital page with span font size / boldness attached.

    Uses the same text page as ``page.get_text("text")`` (default flags) so the
    word set equals the plain-mode text's — the bag-of-words invariant depends
    on it. Lines whose writing direction is not horizontal are flagged
    ``rotated`` (vertical margin text) and kept.
    """
    tp = textpage if textpage is not None else page.get_textpage()
    d = page.get_text("dict", textpage=tp)
    spans: dict[tuple[int, int], list[tuple[float, float, float, bool]]] = {}
    rotated: set[tuple[int, int]] = set()
    for bi, b in enumerate(d["blocks"]):
        if b.get("type") != 0:
            continue
        for li, line in enumerate(b["lines"]):
            if abs(line["dir"][1]) > 0.05:
                rotated.add((bi, li))
            spans[(bi, li)] = [
                (
                    s["bbox"][0],
                    s["bbox"][2],
                    float(s["size"]),
                    bool(s["flags"] & 16) or bool(_BOLD_FONT_RE.search(s.get("font") or "")),
                )
                for s in line["spans"]
                if s["text"].strip()
            ]
    out: list[Word] = []
    for x0, y0, x1, y1, t, b, line_no, _wno in page.get_text("words", textpage=tp):
        if not t.strip():
            continue
        sp = spans.get((b, line_no)) or [(x0, x1, y1 - y0, False)]
        best = max(sp, key=lambda s: min(s[1], x1) - max(s[0], x0))
        out.append(
            Word(
                float(x0),
                float(y0),
                float(x1),
                float(y1),
                t,
                best[2],
                best[3],
                100.0,
                int(b),
                0,
                int(line_no),
                (b, line_no) in rotated,
            )
        )
    return out


def vector_rules_from_fitz_page(page: Any, *, min_len: float = 20.0) -> list[Rule]:
    """Horizontal/vertical rulings from the page's vector drawings (lines, thin
    rectangles, and the edges of boxes wider than 5 pt)."""
    out: list[Rule] = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p, q = it[1], it[2]
                if abs(p.y - q.y) < 1 and abs(p.x - q.x) > min_len:
                    out.append(
                        Rule("h", min(p.x, q.x), (p.y + q.y) / 2, max(p.x, q.x), (p.y + q.y) / 2)
                    )
                elif abs(p.x - q.x) < 1 and abs(p.y - q.y) > min_len:
                    out.append(
                        Rule("v", (p.x + q.x) / 2, min(p.y, q.y), (p.x + q.x) / 2, max(p.y, q.y))
                    )
            elif it[0] == "re":
                r = it[1]
                if r.height < 2 and r.width > min_len:
                    out.append(Rule("h", r.x0, r.y0, r.x1, r.y1))
                elif r.width < 2 and r.height > min_len:
                    out.append(Rule("v", r.x0, r.y0, r.x1, r.y1))
                elif r.width > min_len and r.height > 5:
                    out.append(Rule("h", r.x0, r.y0, r.x1, r.y0))
                    out.append(Rule("h", r.x0, r.y1, r.x1, r.y1))
    return normalize_rules(out)
