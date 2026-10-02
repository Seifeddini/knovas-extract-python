"""Borderless/ruled table detection, column structure and pack rendering (plan R3).

Detection (:func:`detect_tables`): runs of row-aligned visual lines. A row
candidate has ≥ 2 segments and an amount/date cell or only short cells. A run
may continue through ≤ 2 label-only lines (wrapped labels, section rows). A run
is a table when ≥ 2 amount rows share a right-edge anchor (tolerance 2 u) or
≥ 3 short-text rows share ≥ 2 left anchors, and it has ≥ ``min_lines`` lines.

Heading-size guard: once a run has two lines, a line carrying a text segment
whose em is ≥ 1.2 × (OCR: 1.3 ×) the band's label em is never clustered into
the run — it ends it. Tax-form field codes (``8`` next to the ``Abzüge`` heading)
and section titles between tables stay out of the rows.

Structure (:func:`table_structure`): columns from the modal segment count
(Camelot-stream style) plus sparse extra columns, header = leading rows without
amounts in value columns, multi-line cells merged via tight/loose baseline gap
classes or, when rulings exist, only when no rule separates the lines.

Rendering (:func:`render_table_parts`): one row per line, `` | `` cells,
compact fold keys for numeric/date cells, packs ≤ ``budget`` estimated tokens
with the header repeated on top of every pack, ``#### `` section rows, rows
over the token/char limit split with ``(Forts.)``.
"""

from __future__ import annotations

import re
import statistics as st
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .lint import clean_cell, est_tokens, lint_row_line, split_long_row
from .segments import Segment
from .words import CURRENCY_TOKENS, Rule, has_letters, is_amount, is_dateyear

Run = list[list[Segment]]
YEAR_IN_RE = re.compile(r"(?:19|20)\d{2}")
_NO_KEY_WORDS = frozenset({"CHF", "Fr.", "EUR", "in", "€"})


def _is_cand(line: Sequence[Segment]) -> bool:
    if len(line) < 2:
        return False
    if any(s.numeric() or s.datelike() for s in line[1:]):
        return True
    if len(line) % 2 == 0 and all(
        line[k].text.endswith(":") and line[k].n_words <= 4 for k in range(0, len(line), 2)
    ):
        return True
    return len(line) >= 3 and all(s.n_words <= 4 for s in line)


def _label_em(run: Sequence[Sequence[Segment]], *, ocr: bool) -> float | None:
    """The band's label em (median over ≥ 2-word text segments); ``None`` when the run
    has fewer than two such segments (digit-only header rows have no reliable em on OCR)."""
    ems = [s.em() for line in run for s in line if s.n_words >= 2 and has_letters(s.text)]
    if not ems or (ocr and len(ems) < 2):
        return None if ocr else st.median([s.em() for line in run for s in line])
    return st.median(ems)


def _oversized(line: Sequence[Segment], band_em: float | None, guard: float, *, ocr: bool) -> bool:
    """Heading-size guard: a text segment ≥ guard × the band em never joins the run.
    OCR word heights are noisy for single words with colons, so OCR requires ≥ 2 words."""
    if band_em is None:
        return False
    return any(
        s.em() >= guard * band_em
        and has_letters(s.text)
        and not s.numeric()
        and (not ocr or (s.n_words >= 2 and not s.text.endswith(":")))
        for s in line
    )


def _rule_between(y_a: float, y_b: float, x0: float, x1: float, hrules: Sequence[Rule]) -> bool:
    for r in hrules:
        if y_a < r.y < y_b and min(r.x1, x1) - max(r.x0, x0) >= 0.5 * max(x1 - x0, 1e-6):
            return True
    return False


def detect_tables(
    vlines: Sequence[Sequence[Segment]],
    u: float,
    page_w: float,
    *,
    ocr: bool = False,
    hrules: Sequence[Rule] = (),
    min_lines: int = 3,
) -> list[Run]:
    """Table regions as runs of visual lines (segments). Empty lines in *vlines*
    act as hard breaks (consumed by another detector)."""
    guard = 1.3 if ocr else 1.2
    tables: list[Run] = []
    n = len(vlines)
    pitches = [b[0].y0 - a[0].y0 for a, b in pairwise(vlines) if a and b]
    pitch = max(st.median(pitches), 1.0) if pitches else 12.0
    taken: set[int] = set()
    i = 0
    while i < n:
        if not (vlines[i] and _is_cand(vlines[i])):
            i += 1
            continue
        j = i
        run: Run = []
        pending: Run = []
        while j < n:
            line = list(vlines[j])
            if not line:
                break
            if run:
                prev_y1 = max(s.y1 for s in run[-1])
                x0 = min(s.x0 for s in run[-1])
                x1 = max(s.x1 for s in run[-1])
                limit = (
                    4.0 * pitch
                    if _rule_between(prev_y1, line[0].y0, x0, x1, hrules)
                    else 2.5 * pitch
                )
                if line[0].y0 - prev_y1 > limit:
                    break
                if len(run) >= 2 and _oversized(line, _label_em(run, ocr=ocr), guard, ocr=ocr):
                    break
                if (
                    len(line) >= 2
                    and not any(s.numeric() for s in line)
                    and any(s.datelike() for s in line[1:])
                    and any(any(s.numeric() for s in ln[1:]) for ln in run)
                ):
                    break  # header-like line after amount rows: a new table starts
                if (
                    len(line) >= 3
                    and all(len(ln) == 2 and _text_key(ln[0]) for ln in run)
                    and not any(s.numeric() for ln in run for s in ln[1:])
                ):
                    break  # key-value block followed by a wider table header
            if _is_cand(line):
                run += [*pending, line]
                pending = []
            elif (
                len(line) == 1
                and line[0].n_words <= 10
                and (line[0].x1 - line[0].x0) < 0.6 * page_w
                and len(pending) < 2
            ):
                pending.append(line)
            else:
                break
            j += 1
        amount_rows = [ln for ln in run if any(s.numeric() for s in ln[1:])]
        ok = False
        if len(amount_rows) >= 2:
            xs = sorted(s.x1 for ln in amount_rows for s in ln[1:] if s.numeric())
            ok = any(sum(1 for x in xs if abs(x - x0) <= 2 * u) >= 2 for x0 in xs)
        elif len(run) >= 3:
            lefts = sorted(s.x0 for ln in run for s in ln)
            anchors = [x0 for x0 in lefts if sum(1 for x in lefts if abs(x - x0) <= 2 * u) >= 3]
            ok = len({round(a / (4 * u)) for a in anchors}) >= 2
        if ok and len(run) >= min_lines:
            start = i - len(_header_above(vlines, i, run, u, pitch, ocr=ocr, taken=taken))
            tables.append([*(list(vlines[k]) for k in range(start, i)), *run])
            taken.update(range(start, j))
            i = j
        else:
            i += 1
    return tables


def _text_key(s: Segment) -> bool:
    return has_letters(s.text) and not s.datelike() and not s.numeric()


def _header_above(
    vlines: Sequence[Sequence[Segment]],
    i: int,
    run: Run,
    u: float,
    pitch: float,
    *,
    ocr: bool,
    taken: set[int],
) -> list[int]:
    """Indices of up to three lines directly above ``vlines[i]`` that read as the run's
    header: every segment fits one of the run's columns, no amounts, short cells, no
    heading-size text, within 2.5 × pitch of the line below. Returned bottom-up.

    Caption guard: when the run already *opens* amount-free (its own header
    ``Ertrag und Aufwand | 31.12.2023 | 31.12.2022``, or a key-value row
    ``Datum: | 05.04.2024``; a run line always has ≥ 2 cells, see :func:`_is_cand`),
    a line above it that is one cell over the label column is a caption, subtitle or
    stray key (``Erfolgsrechnung 2023`` at 11 pt, under the heading-size guard; a lone
    period ``2023``; ``Kunden-Nr.: 10482``; ``Kontoinhaber:``) and never a header cell.
    Taking it would put a label-only line at the top of the grid, which makes
    :func:`table_structure` find no header at all: the real header rows then render as
    data rows, no fold keys, nothing repeated into the packs, and the subtitle becomes
    a ``####`` section row instead of its own heading — or, on a kv grid, the caption
    is glued into the first key. The guard ends the upward scan (nothing above a
    caption is a header line). A lone label above a run that opens with an amount row
    is still taken as before (the grid's leading label-only line / section row);
    multi-cell lines above the run (stacked ``CHF | CHF`` lines, ``Soll | Haben``) and a
    lone stacked word over a *value* column (``Vorjahr`` over ``31.12.2022``) are
    unaffected.

    Wrapped-label exception (:func:`_label_fragment`): a header label wrapped over two
    lines (``Ertrag und`` over ``Aufwand | 31.12.2023 | 31.12.2022``) or a bare label
    over a label-less date header (``Aktiven`` over ``31.12.2023 | 31.12.2022``) has a
    caption's shape but belongs to the header. Freed from the table, a bold fragment
    becomes a ``###`` heading at the subtitle's level on a digital page and pops the
    real subtitle out of the heading context of every chunk below it. So a lone
    label-column cell that carries the header row's em and bold signature, sits within
    1.3 × pitch of it, is digit-free text and ends in no colon is **not** treated as a
    caption: the scan continues and the pre-guard rules decide, exactly as before the
    guard existed (the fragment is taken as a header line, the grid opens label-only
    and the header is lost — the pre-existing rendering, never a new heading). Folding
    the fragment into the header's label cell is deliberately not done here: a fold in
    :func:`table_structure` cannot see these cues and would also fold section titles
    and company names reached through the amount-row path into every pack's header.
    """
    cols = _columns(run, u)
    band = _label_em(run, ocr=ocr)
    guard = 1.3 if ocr else 1.2
    out: list[int] = []
    below_y0 = min(s.y0 for s in run[0])
    run_opens_with_header = not any(s.numeric() for s in run[0])
    header_row = run_opens_with_header and not _is_kv_row(run[0])
    k = i - 1
    while k >= 0 and len(out) < 3 and k not in taken:
        line = vlines[k]
        if not line or _is_kv_row(line):
            break
        y1 = max(s.y1 for s in line)
        if below_y0 - y1 > 2.5 * pitch:
            break
        if (
            run_opens_with_header
            and len(line) == 1
            and min(line[0].x1, cols[0][1]) - max(line[0].x0, cols[0][0]) > 0
            and not (header_row and k == i - 1 and _label_fragment(line[0], run[0], pitch, ocr=ocr))
        ):
            break  # caption / subtitle over a run that carries its own header
        fits = True
        for s in line:
            if (
                s.numeric()
                or s.n_words > 4
                or (band and s.em() >= guard * band and has_letters(s.text))
            ):
                fits = False
                break
            hit = [c for c in cols if min(s.x1, c[1]) - max(s.x0, c[0]) > 0.3 * (s.x1 - s.x0)]
            if len(hit) != 1:
                fits = False
                break
        if not fits:
            break
        out.append(k)
        below_y0 = min(s.y0 for s in line)
        k -= 1
    return out


def _label_fragment(seg: Segment, below: Sequence[Segment], pitch: float, *, ocr: bool) -> bool:
    """Does *seg* (one cell over the label column, directly above the header row
    *below*) look like a wrapped or stacked fragment of that header's label rather
    than a caption? Every cue is required: digit-free text (≥ 3 consecutive letters)
    without a trailing colon, a baseline within 1.3 × pitch of *below*, the same em as
    *below* (± 5 %; OCR ± 8 %, word heights there are noisy) and the same bold
    signature. Only the line directly above the run is ever asked (a fragment cannot
    sit above a caption)."""
    text = seg.text
    if not has_letters(text) or text.endswith(":") or any(ch.isdigit() for ch in text):
        return False
    if min(s.y0 for s in below) - seg.y0 > 1.3 * pitch:
        return False
    ref = st.median([s.em() for s in below])
    if ref <= 0 or abs(seg.em() - ref) > (0.08 if ocr else 0.05) * ref:
        return False
    n = sum(s.n_words for s in below)
    below_bold = sum(s.bold_ratio() * s.n_words for s in below) / n >= 0.5
    return (seg.bold_ratio() >= 0.5) == below_bold


def _is_kv_row(line: Sequence[Segment]) -> bool:
    return len(line) % 2 == 0 and all(line[k].text.endswith(":") for k in range(0, len(line), 2))


@dataclass(slots=True)
class TableRow:
    cells: dict[int, str]
    bold: bool = False
    y0: float = 0.0
    y1: float = 0.0
    x0: float = 0.0

    @property
    def has_values(self) -> bool:
        return any(k > 0 and v for k, v in self.cells.items())

    @property
    def label(self) -> str:
        return self.cells.get(0, "")


@dataclass(slots=True)
class TableGrid:
    header: dict[int, str]
    rows: list[TableRow]
    ncols: int
    raw: list[dict[int, str]] = field(default_factory=list)  # one entry per visual line


def _gap_classes(gaps: Sequence[float]) -> float | None:
    """Threshold between tight (same cell / wrapped) and loose (row break) gaps if bimodal."""
    if len(gaps) < 3:
        return None
    g = sorted(gaps)
    best, thr = 1.0, None
    for a, b in pairwise(g):
        if a > 0 and b / a > best:
            best, thr = b / a, (a + b) / 2
    return thr if best >= 1.25 else None


def _columns(run: Run, u: float) -> list[list[float]]:
    counts = Counter(len(ln) for ln in run)
    mode = max(counts, key=lambda k: (counts[k], k))
    cols: list[list[float]] = []
    for a, b in sorted((s.x0, s.x1) for ln in run if len(ln) == mode for s in ln):
        if cols and a <= cols[-1][1] + 0.5 * u:
            cols[-1][1] = max(cols[-1][1], b)
        else:
            cols.append([a, b])
    extra = sorted(
        (s.x0, s.x1)
        for ln in run
        for s in ln
        if not any(min(s.x1, c[1]) - max(s.x0, c[0]) > 0 for c in cols)
    )
    for a, b in extra:
        hit = [c for c in cols if min(b, c[1]) - max(a, c[0]) > -0.5 * u]
        if hit:
            hit[0][0] = min(hit[0][0], a)
            hit[0][1] = max(hit[0][1], b)
        else:
            cols.append([a, b])
            cols.sort()
    return cols


def table_structure(run: Run, u: float, hrules: Sequence[Rule] = ()) -> TableGrid:
    cols = _columns(run, u)

    def col_of(s: Segment) -> int:
        xc = (s.x0 + s.x1) / 2
        best = max(
            (min(s.x1, c[1]) - max(s.x0, c[0]), -abs(xc - (c[0] + c[1]) / 2), k)
            for k, c in enumerate(cols)
        )
        return best[2]

    grid: list[tuple[dict[int, str], list[Segment]]] = []
    for ln in run:
        buckets: dict[int, list[str]] = defaultdict(list)
        for s in ln:
            buckets[col_of(s)].append(clean_cell(s.text))
        grid.append(({k: " ".join(v) for k, v in sorted(buckets.items())}, ln))
    nh = 0
    for cells, _ln in grid:
        if nh >= 3 or not any(k > 0 for k in cells):
            break
        if any(is_amount(t) for k, v in cells.items() if k > 0 for t in v.split()):
            break
        if len(cells.get(0, "").split()) > 6:
            break
        nh += 1
    if nh >= len(grid) - 1:
        nh = 0
    header_parts: dict[int, list[str]] = defaultdict(list)
    for cells, _ln in grid[:nh]:
        for k, v in cells.items():
            header_parts[k].append(v)
    header = {k: " ".join(v) for k, v in header_parts.items()}
    body = grid[nh:]
    gaps = [b[1][0].y0 - a[1][0].y0 for a, b in pairwise(body)]
    thr = _gap_classes(gaps)
    rows: list[TableRow] = []
    cur: TableRow | None = None
    for idx, (cells, ln) in enumerate(body):
        y0 = min(s.y0 for s in ln)
        y1 = max(s.y1 for s in ln)
        x0 = min(s.x0 for s in ln)
        x1 = max(s.x1 for s in ln)
        bold = sum(s.bold_ratio() * s.n_words for s in ln) / sum(s.n_words for s in ln) >= 0.8
        has_val = any(k > 0 for k in cells)
        tight = False
        if cur is not None and idx > 0:
            if hrules:
                tight = not _rule_between(cur.y1, y0, min(cur.x0, x0), x1, hrules)
            else:
                tight = thr is not None and gaps[idx - 1] < thr
            if tight and has_val and cur.has_values:
                tight = False
            if tight and cur.bold != bold and not cur.has_values:
                tight = False  # bold section label over normal rows
            if tight and not cur.has_values and x0 - cur.x0 > 1.5 * u:
                tight = False  # indented rows under a label-only line
        if tight and cur is not None:
            for k, v in cells.items():
                cur.cells[k] = (cur.cells.get(k, "") + " " + v).strip()
            cur.y1 = y1
        else:
            cur = TableRow(dict(cells), bold, y0, y1, x0)
            rows.append(cur)
    return TableGrid(header, rows, len(cols), [c for c, _ in grid])


def compact_keys(header: dict[int, str], ncols: int) -> dict[int, str]:
    """Short per-column fold keys: the (last) year in the header cell, else its first
    three words without currency tokens; duplicates keep the distinguishing words."""
    keys: dict[int, str] = {}
    for k in range(1, ncols):
        h = header.get(k, "").strip()
        if not h:
            continue
        y = YEAR_IN_RE.findall(h)
        words = [w for w in h.split() if w not in _NO_KEY_WORDS]
        keys[k] = y[-1] if y else " ".join(words[:3])
    vals = list(keys.values())
    for k, v in list(keys.items()):
        if vals.count(v) > 1:
            words = [w for w in header[k].split() if w not in CURRENCY_TOKENS]
            keys[k] = " ".join(words[:3])
    return {k: v for k, v in keys.items() if v}


@dataclass(slots=True)
class TablePart:
    kind: str  # "table" | "heading"
    text: str
    level: int = 0


def _foldable(v: str, key: str) -> bool:
    return bool(YEAR_IN_RE.fullmatch(key)) or all(is_amount(t) or is_dateyear(t) for t in v.split())


def render_table_parts(
    grid: TableGrid,
    *,
    budget: int = 150,
    row_max_tokens: int = 200,
    row_max_chars: int = 1800,
    fold: bool = True,
    sentence_guard: bool = True,
    section_rows: bool = True,
) -> list[TablePart]:
    ncols = grid.ncols
    keys = compact_keys(grid.header, ncols) if fold else {}
    hcells = [clean_cell(grid.header.get(k, "")) for k in range(ncols)]
    hline = " | ".join(h for h in hcells if h)
    hline = lint_row_line(hline, sentence_guard=sentence_guard) if hline else ""
    htok = est_tokens(hline) if hline else 0
    parts: list[TablePart] = []
    pack: list[str] = []
    pack_t = htok

    def flush() -> None:
        nonlocal pack, pack_t
        if pack:
            parts.append(TablePart("table", "\n".join(([hline] if hline else []) + pack)))
        pack, pack_t = [], htok

    rows = grid.rows
    for ri, row in enumerate(rows):
        label = clean_cell(row.label)
        if not row.has_values:
            if not label:
                continue
            following = 0
            for nxt in rows[ri + 1 :]:
                if not nxt.has_values:
                    break
                following += 1
            if section_rows and following >= 2 and has_letters(label) and len(label.split()) <= 12:
                flush()
                parts.append(
                    TablePart("heading", "#### " + lint_row_line(label, sentence_guard=False), 4)
                )
                continue
            lines = [lint_row_line(label, sentence_guard=sentence_guard)]
        else:
            cells: list[str] = []
            last_nonempty = max((k for k, v in row.cells.items() if v and k > 0), default=0)
            for k in range(1, ncols):
                v = clean_cell(row.cells.get(k, ""))
                if not v:
                    if not keys and k < last_nonempty:
                        cells.append("-")
                    continue
                key = keys.get(k)
                cells.append(f"{key}: {v}" if key and _foldable(v, key) else v)
            lines = [
                lint_row_line(ln, sentence_guard=sentence_guard)
                for ln in split_long_row(
                    label, cells, max_tokens=row_max_tokens, max_chars=row_max_chars
                )
            ]
        for ln in lines:
            t = est_tokens(ln)
            if pack and pack_t + t > budget:
                flush()
            pack.append(ln)
            pack_t += t
    flush()
    return parts
