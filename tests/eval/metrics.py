"""Search-oriented layout metrics for the Treuhand golden fixtures.

Ported from the benchmark harness (``ocrbench/mdlite/eval_swiss.py`` and
``ocrbench/evalm/metrics.py``). Every metric takes the rendered page text (and
the ground-truth page record) and returns plain floats / counts so pytest can
gate on them and a nightly can aggregate them.

Ground-truth page record (``tests/fixtures/treuhand/gt/<doc>_p<k>.json``)::

    tables:   [{"header_rows": int, "rows": [[cell, …], …]}, …]
    kvs:      [{"label": str, "value": str}, …]
    headings: [{"text": str, "level": int}, …]
    items:    [{"role": str, "text": str, "order": int, "bbox": [x0,y0,x1,y1]}, …]
    ignore_regions: [{"bbox": [...]}, …]
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from rapidfuzz import fuzz

PROSE_ROLES = frozenset(
    {
        "heading",
        "para",
        "text",
        "letterhead",
        "address",
        "date",
        "signature",
        "checklist",
        "kv_label",
        "kv_value",
    }
)
_AMOUNT_RE = re.compile(
    r"^[(\-–−+]?(?:CHF|Fr\.|EUR|€|\$)?\s?[(\-–−]?\d{1,3}(?:[’'`´ ]\d{3})*(?:[.,]\d{1,2})?[)%]?$"  # noqa: RUF001
    r"|^[(\-–−]?\d+(?:[.,]\d{1,2})[)%]?$"  # noqa: RUF001
)
_NUMLIKE_RE = re.compile(r"^[(\-–−+]?[\d'’.,/%]+[)%]?$")  # noqa: RUF001
_TOK_RE = re.compile(r"[\wäöüÄÖÜéèàçâêôûÀ'.,%/-]+")
_NUM_RE = re.compile(
    r"(?<![\w'.])(?:-?\d{1,3}(?:'\d{3})+(?:\.\d{2})?|-?\d+\.\d{2}|\d{2}\.\d{2}\.\d{4})(?![\w'.]?\d)"
)
_APOS = dict.fromkeys(map(ord, "’‘ʼ´`′"), "'")  # noqa: RUF001
_SPACES = dict.fromkeys(map(ord, "    "), " ")  # noqa: RUF001
_DASH = dict.fromkeys(map(ord, "–—−"), "-")  # noqa: RUF001
_SENT_SPLIT = re.compile(r"\s+|(?<=[.!?])(?=[A-ZÜÖÄ\d\"'(])")


# ── normalisation ──────────────────────────────────────────────────────────


_SENT_WS = re.compile(r"(?<=[.!?])\s+(?=[A-ZÜÖÄ\d\"'(])")


def norm(s: str) -> str:
    """NFC, ASCII apostrophes/spaces, whitespace collapsed, lower-cased."""
    s = unicodedata.normalize("NFC", s).replace("­", "")
    s = s.translate(_APOS).translate(_SPACES)
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_row(s: str) -> str:
    """``norm`` plus the whitespace the row lint removes after an abbreviation
    (``Ziff. 220`` → ``Ziff.220``), applied to both sides of a row/cell/kv comparison."""
    return norm(_SENT_WS.sub("", unicodedata.normalize("NFC", s)))


def toks(s: str) -> list[str]:
    return _TOK_RE.findall(norm(s))


def strip_md(line: str) -> str:
    ln = line.strip()
    if ln.startswith("#"):
        return ln.lstrip("#").strip()
    if ln.startswith("- "):
        return ln[2:]
    return ln


def numeric_cell(t: str) -> bool:
    return bool(t) and (bool(_AMOUNT_RE.match(t)) or _NUMLIKE_RE.match(t) is not None)


def nonblank_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln.strip()]


# ── tables ─────────────────────────────────────────────────────────────────


def table_metrics(
    gt: dict[str, Any], lines: Sequence[str]
) -> tuple[dict[str, int], list[tuple[str, Any, str]]]:
    """row_ok: GT body rows (≥ 1 numeric cell) whose label (≥ 80 % tokens) and ALL
    numeric cells share one output line; row_cond: same, restricted to rows whose
    numeric cells all occur somewhere (layout quality net of OCR errors);
    cells: non-empty body cells found on the row's best line; hdr: header cells
    found on any line."""
    nl = [norm_row(ln) for ln in lines]
    rows_ok = rows_cond = rows_n = cond_n = 0
    cells_f = cells_n = hdr_f = hdr_n = 0
    fails: list[tuple[str, Any, str]] = []
    for t in gt["tables"]:
        hr = t["header_rows"]
        for r in t["rows"][:hr]:
            for c in r:
                if c:
                    hdr_n += 1
                    hdr_f += any(norm_row(c) in ln for ln in nl)
        for r in t["rows"][hr:]:
            cells = [c for c in r if c]
            if not cells:
                continue
            best, best_line = -1, ""
            for ln in nl:
                sc = sum(1 for c in cells if norm_row(c) in ln)
                if sc > best:
                    best, best_line = sc, ln
            cells_n += len(cells)
            cells_f += max(best, 0)
            nums = [c for k, c in enumerate(r) if k > 0 and numeric_cell(c)] or (
                [r[0]] if numeric_cell(r[0]) and len(cells) > 1 else []
            )
            labels = [c for c in cells if not numeric_cell(c)]
            if not nums or not labels:
                continue
            rows_n += 1
            lab = toks(labels[0])
            rt = len(toks(" ".join(cells)))
            present = all(any(norm_row(a) in ln for ln in nl) for a in nums)
            cond_n += present
            ok = False
            for ln in nl:
                if (
                    all(norm_row(a) in ln for a in nums)
                    and sum(1 for w in lab if w in ln) >= 0.8 * max(1, len(lab))
                    and len(toks(ln)) <= 3 * rt + 8
                ):
                    ok = True
                    break
            rows_ok += ok
            rows_cond += ok and present
            if not ok and len(fails) < 3:
                fails.append(("row", r, best_line[:120]))
    return (
        {
            "rows_n": rows_n,
            "rows_ok": rows_ok,
            "cond_n": cond_n,
            "rows_cond": rows_cond,
            "cells_n": cells_n,
            "cells_f": cells_f,
            "hdr_n": hdr_n,
            "hdr_f": hdr_f,
        },
        fails,
    )


def kv_metrics(gt: dict[str, Any], lines: Sequence[str]) -> tuple[int, int]:
    """GT key-value pairs whose value and ≥ 80 % of the key tokens share one line."""
    nl = [norm_row(ln) for ln in lines]
    n = ok = 0
    for kv in gt["kvs"]:
        n += 1
        key = toks(kv["label"].rstrip(":"))
        val = norm_row(kv["value"])
        ok += any(
            val in ln and sum(1 for w in key if w in ln) >= 0.8 * max(1, len(key)) for ln in nl
        )
    return n, ok


# ── headings ───────────────────────────────────────────────────────────────


def heading_metrics(
    gt: dict[str, Any], lines: Sequence[str], *, ratio: int = 85
) -> tuple[int, int, int, int]:
    """``(gt_n, recalled, out_n, precise)`` with rapidfuzz ratio ≥ *ratio* after normalisation."""
    hl = [norm(ln.lstrip("#")) for ln in lines if ln.startswith("#")]
    gh = [norm(h["text"]) for h in gt["headings"]]
    rec = sum(1 for h in gh if any(fuzz.ratio(h, x) >= ratio for x in hl))
    prec = sum(1 for x in hl if any(fuzz.ratio(h, x) >= ratio for h in gh))
    return len(gh), rec, len(hl), prec


# ── prose: intactness, order, bag of words ─────────────────────────────────


def _in_ignore(it: dict[str, Any], regions: Sequence[Sequence[float]]) -> bool:
    cx, cy = (it["bbox"][0] + it["bbox"][2]) / 2, (it["bbox"][1] + it["bbox"][3]) / 2
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in regions)


def prose_units(gt: dict[str, Any]) -> list[dict[str, Any]]:
    ign = [r["bbox"] for r in gt.get("ignore_regions", [])]
    return [
        it
        for it in sorted(gt["items"], key=lambda i: i["order"])
        if it["role"] in PROSE_ROLES and not _in_ignore(it, ign)
    ]


def kendall_tau(seq: Sequence[int]) -> float:
    c = d = 0
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            c += seq[i] < seq[j]
            d += seq[i] > seq[j]
    return (c - d) / max(1, c + d)


def order_metrics(units: Sequence[dict[str, Any]], out_text: str) -> tuple[int, int, float]:
    """``(units, intact, tau)``: a unit is intact when ≥ 90 % of its tokens occur inside a
    1.15× window of the output token stream; tau = Kendall tau of the recovered unit
    positions against GT order."""
    ow = toks(out_text)
    pos: list[int] = []
    intact = 0
    for u in units:
        pw = toks(u["text"])
        n = len(pw)
        if n == 0:
            continue
        sm = difflib.SequenceMatcher(None, pw, ow, autojunk=False)
        idx = sorted(j + k for _i, j, sz in sm.get_matching_blocks() for k in range(sz))
        best, bstart, win, a = 0, None, int(1.15 * n) + 1, 0
        for b in range(len(idx)):
            while idx[b] - idx[a] > win:
                a += 1
            if b - a + 1 > best:
                best, bstart = b - a + 1, idx[a]
        if best >= 0.9 * n and bstart is not None:
            intact += 1
            pos.append(bstart)
    return len(units), intact, kendall_tau(pos)


def word_recall(units: Sequence[dict[str, Any]], prose_lines: Sequence[str]) -> float:
    gc = Counter(toks(" ".join(u["text"] for u in units)))
    oc = Counter(toks(" ".join(prose_lines)))
    return sum(min(v, oc[k]) for k, v in gc.items()) / max(1, sum(gc.values()))


# ── numeric tokens (what fiduciaries search for) ───────────────────────────


def numeric_tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFC", s).translate(_APOS).translate(_SPACES).translate(_DASH)
    out: list[str] = []
    for m in _NUM_RE.finditer(s):
        t = m.group(0)
        out.append(t if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", t) else re.sub(r"['\s]", "", t))
    return out


def gt_numeric_tokens(gt: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for t in gt["tables"]:
        for row in t["rows"][t["header_rows"] :]:
            for cell in row[1:]:
                out += numeric_tokens(cell)
    return out


def numeric_prf(out: str, gt: dict[str, Any]) -> tuple[float, float, float]:
    """Multiset precision/recall/F1 of canonical numeric tokens: recall over table-row
    amounts, precision allowing any numeric token that legitimately occurs on the page."""
    g, o = Counter(gt_numeric_tokens(gt)), Counter(numeric_tokens(out))
    allowed = set(g) | set(numeric_tokens("\n".join(it["text"] for it in gt["items"])))
    if not g:
        return 1.0, 1.0, 1.0
    tp = sum((g & o).values())
    # repeated header lines / fold keys legitimately repeat page numbers: set-based
    p = sum(c for t, c in o.items() if t in allowed) / max(1, sum(o.values()))
    r = tp / sum(g.values())
    f = 0.0 if tp == 0 else 2 * p * r / (p + r)
    return p, r, f


# ── bag of words vs plain mode (R10) ───────────────────────────────────────


def _dehyphen_lines(text: str) -> str:
    """Join ``xxx-\\nyyy`` the way the renderer does, so plain and layout compare equal."""
    out: list[str] = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if (
            out
            and re.search(r"\w[-­]$", out[-1])
            and ln
            and ln[0].islower()
            and ln.split(" ")[0].lower()
            not in (
                "und",
                "oder",
                "bzw.",
                "sowie",
                "als",
                "wie",
                "bis",
                "u.",
                "resp.",
                "et",
                "ou",
                "e",
                "o",
            )
            or out
            and out[-1].endswith("­")
        ):
            out[-1] = out[-1][:-1] + ln
        else:
            out.append(ln)
    return "\n".join(out)


def bow(text: str) -> Counter[str]:
    """Bag of alphanumeric tokens; sentence-boundary whitespace the row lint removed is
    re-split so ``Fr.1'200`` and ``Fr. 1'200`` tokenise alike."""
    c: Counter[str] = Counter()
    for raw in _SENT_SPLIT.split(_dehyphen_lines(text)):
        t = norm(raw).strip("|-•·#()[]\"'«»,;:.")
        if t and re.search(r"[\w]", t):
            c[t] += 1
    return c


def bag_of_words_diff(
    layout: str, plain: str, *, furniture: Iterable[str] = ()
) -> tuple[Counter[str], Counter[str]]:
    """``(missing, extra)``: tokens of plain (minus furniture lines) absent from layout, and
    tokens in layout that plain does not have."""
    p = bow(plain)
    for f in furniture:
        p -= bow(f)
    lay = bow(layout)
    return p - lay, lay - p


def row_integrity_simple(out: str, rows: Sequence[Sequence[str]], label_thresh: int = 88) -> float:
    """Fraction of rows whose label and every amount appear on ONE line, amounts in order."""
    if not rows:
        return 1.0
    lines = [norm(ln) for ln in out.splitlines() if ln.strip()]
    ok = 0
    for row in rows:
        label = norm(row[0])
        want = [t for c in row[1:] for t in numeric_tokens(c)]
        for ln in lines:
            if fuzz.partial_ratio(label, ln) < label_thresh:
                continue
            it = iter(numeric_tokens(ln))
            if all(any(w == g for g in it) for w in want):
                ok += 1
                break
    return ok / len(rows)
