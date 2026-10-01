"""Key-value forms (plan R4): horizontal grids, vertical boxes and inline pairs.

* **grid** — a table run with 2 (or 2k) columns whose even columns are text keys
  (``Name, Vorname:``) and odd columns short values. Rendered as
  ``key: value | key: value`` lines, never with fold keys or a header.
* **vertical** — a line of label boxes with the values on the *next* visual line
  (≤ 1.4 × pitch below) inside each label's box (Lohnausweis form fields
  ``C AHV-Nr.`` / ``756.9217.0769.85``). One value per box, ≤ 6 words, and no
  continuation line of the same em under the value (an address block is not a
  value).
* **inline** — ≥ 2 consecutive single-segment lines ``Key: value``; a line
  carrying two pairs (``Name: Meier Hans Geburtsdatum: 14.07.1968``) is split
  on the second label-colon.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .lint import clean_cell
from .segments import Segment
from .words import has_letters, is_amount, is_dateyear, is_numlike

_UPPER_START = re.compile(r"^[A-ZÄÖÜ0-9]")
_INLINE_KV_RE = re.compile(r"^(?P<key>[^\s:][^:]{0,40}?):\s+(?P<val>\S.*)$")
_SHORT_LOWER = re.compile(r"^[a-zäöü]{1,4}$")


# ── inline pairs ───────────────────────────────────────────────────────────


def split_kv_pairs(text: str) -> list[tuple[str, str]] | None:
    """Split ``"K1: v1 K2: v2"`` into pairs; ``None`` when there are fewer than 2 keys.

    The first key is everything before the first colon (≤ 4 words). A later key is
    the shortest run of words ending at a colon token that starts with an upper-case
    letter, extended backwards over words ending in a comma and over short
    lower-case function words (``Zahlbar bis:``, ``Ort, Datum:``).
    """
    toks = text.split()
    colon_idx = [i for i, t in enumerate(toks) if t.endswith(":") and len(t) > 1]
    if len(colon_idx) < 2 or colon_idx[0] > 3:
        return None
    pairs: list[tuple[str, str]] = []
    key_start = 0
    for n, ci in enumerate(colon_idx):
        if n > 0:
            start = ci
            while start - 1 > colon_idx[n - 1] and (
                toks[start - 1].endswith(",")
                or _SHORT_LOWER.match(toks[start - 1])
                or not _UPPER_START.match(toks[start])
            ):
                if ci - (start - 1) >= 4:
                    break
                start -= 1
            if start <= colon_idx[n - 1]:
                return None
            value = " ".join(toks[colon_idx[n - 1] + 1 : start])
            if not value:
                return None
            pairs.append((" ".join(toks[key_start : colon_idx[n - 1] + 1]).rstrip(":"), value))
            key_start = start
    last = colon_idx[-1]
    pairs.append((" ".join(toks[key_start : last + 1]).rstrip(":"), " ".join(toks[last + 1 :])))
    if any(not k.strip() or len(k.split()) > 4 for k, _ in pairs):
        return None
    return pairs


def _format_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return " | ".join(
        f"{clean_cell(k).rstrip(':').strip()}: {clean_cell(v) or '-'}" for k, v in pairs
    )


@dataclass(slots=True)
class InlineKv:
    first: int
    last: int
    lines: list[str]
    segs: list[Segment] = field(default_factory=list)


def detect_inline_kv(vlines: Sequence[Sequence[Segment]], used: set[int]) -> list[InlineKv]:
    """Runs of ≥ 2 consecutive single-segment ``Key: value`` lines (segment ids added to *used*)."""
    out: list[InlineKv] = []
    i = 0
    n = len(vlines)
    while i < n:
        j = i
        lines: list[str] = []
        segs: list[Segment] = []
        while j < n:
            live = [s for s in vlines[j] if id(s) not in used]
            if len(live) != 1:
                break
            s = live[0]
            m = _INLINE_KV_RE.match(s.text)
            if not m or len(m.group("key").split()) > 4 or not has_letters(m.group("key")):
                break
            pairs = split_kv_pairs(s.text) or [(m.group("key"), m.group("val"))]
            lines.append(_format_pairs(pairs))
            segs.append(s)
            j += 1
        if len(lines) >= 2:
            out.append(InlineKv(i, j - 1, lines, segs))
            used.update(id(s) for s in segs)
            i = j
        else:
            i += 1
    return out


# ── grids ──────────────────────────────────────────────────────────────────


def is_kv_grid(raw: Sequence[dict[int, str]], ncols: int) -> bool:
    """Key-value form block: even column count, no amounts aligned like a financial
    table, text keys (≥ half ending with ':' for 2k > 2 columns)."""
    if len(raw) < 2 or ncols % 2 or ncols == 0:
        return False
    if ncols > 2:
        lab = sum(1 for c in raw for k in range(0, ncols, 2) if c.get(k, "").rstrip().endswith(":"))
        return lab >= 0.5 * len(raw) * (ncols // 2)
    for c in raw:
        k, v = c.get(0, ""), c.get(1, "")
        if not k or not v or len(v.split()) > 6 or len(k.split()) > 8:
            return False
        if is_amount(k.split()[-1]) or not has_letters(k):
            return False
    keys_colon = sum(1 for c in raw if c.get(0, "").rstrip().endswith(":"))
    vals_num = sum(1 for c in raw if all(is_amount(t) for t in c.get(1, "").split()))
    return keys_colon >= len(raw) / 2 or vals_num < len(raw)


def render_kv_grid(raw: Sequence[dict[int, str]], ncols: int) -> list[str]:
    out: list[str] = []
    for c in raw:
        pairs: list[tuple[str, str]] = []
        for k in range(0, ncols, 2):
            key, val = c.get(k, ""), c.get(k + 1, "")
            if not key and not val:
                continue
            inner = split_kv_pairs(f"{key} {val}".strip()) if val else None
            if inner and len(inner) >= 2:
                pairs.extend(inner)
            else:
                pairs.append((key, val))
        if pairs:
            out.append(_format_pairs(pairs))
    return out


# ── vertical boxes ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class VerticalKv:
    label_line: int
    value_line: int
    text: str
    segs: list[Segment]


_LETTER_CODE = re.compile(r"^[A-Z]\s")


def _strong_label(s: Segment) -> bool:
    """A label that is visibly one: ends with a colon or carries a form letter code (``C AHV-Nr.``)."""
    return s.text.endswith(":") or bool(_LETTER_CODE.match(s.text))


def _label_like(s: Segment, body_em: float) -> bool:
    t = s.text
    if not has_letters(t) or s.numeric():
        return False
    if s.n_words > (10 if _strong_label(s) else 5):
        return False
    if s.em() > 1.15 * body_em and not t.endswith(":"):
        return False
    return not re.search(r"[.!?]$", t) or t.endswith(":") or bool(re.search(r"\b(?:Nr|No|N)\.$", t))


def _numeric_value(s: Segment) -> bool:
    return all(is_numlike(w.text) or is_dateyear(w.text) for w in s.words)


def _value_like(s: Segment) -> bool:
    if s.n_words > 6 or s.text.endswith(":") or s.bold_ratio() >= 0.8:
        return False
    return not re.search(r"[.!?]$", s.text) or _numeric_value(s) or s.datelike()


def _is_kv_row(line: Sequence[Segment]) -> bool:
    """A horizontal ``key: | value`` row (handled by the grid detector, never as vertical labels)."""
    return len(line) % 2 == 0 and all(line[k].text.endswith(":") for k in range(0, len(line), 2))


def _has_continuation(s: Segment, nxt: Sequence[Segment], pitch: float) -> bool:
    for c in nxt:
        if (
            min(c.x1, s.x1) - max(c.x0, s.x0) > 0
            and -0.3 * pitch <= c.y0 - s.y1 <= 1.4 * pitch
            and abs(c.em() - s.em()) <= 0.2 * max(c.em(), s.em())
        ):
            return True
    return False


def detect_vertical_kv(
    vlines: Sequence[Sequence[Segment]],
    u: float,
    pitch: float,
    *,
    body_em: float,
    used: set[int],
) -> list[VerticalKv]:
    """Vertical form boxes. Consumes the label segments and the matched value segments
    (ids added to *used*); unmatched segments of the value line stay for the other
    detectors (an address block under ``H Name und Adresse`` is not a value)."""
    out: list[VerticalKv] = []
    n = len(vlines)
    for i in range(n - 1):
        line = sorted((s for s in vlines[i] if id(s) not in used), key=lambda s: s.x0)
        values = sorted((s for s in vlines[i + 1] if id(s) not in used), key=lambda s: s.x0)
        if not line or not values or _is_kv_row(line):
            continue
        # non-label segments on the label line (an address line crossing the form) are ignored
        labels = [s for s in line if _label_like(s, body_em)]
        if not labels or (len(labels) < len(line) and not any(_strong_label(s) for s in labels)):
            continue
        gap = min(s.y0 for s in values) - max(s.y1 for s in labels)
        if gap < -0.2 * pitch or gap > 1.4 * pitch:
            continue
        boxes = [
            (lab.x0 - u, labels[k + 1].x0 - u if k + 1 < len(labels) else float("inf"))
            for k, lab in enumerate(labels)
        ]
        assigned: dict[int, Segment] = {}
        crowded: set[int] = set()
        for v in values:
            xc = (v.x0 + v.x1) / 2
            box = next((k for k, (b0, b1) in enumerate(boxes) if b0 <= xc < b1), None)
            if box is None:
                continue  # outside every box: not part of the form line
            if box in assigned or box in crowded:
                crowded.add(box)  # two values under one label: not a simple box
                assigned.pop(box, None)
                continue
            if _value_like(v) and labels[box].col == v.col:
                assigned[box] = v
        nxt = vlines[i + 2] if i + 2 < n else ()
        assigned = {k: v for k, v in assigned.items() if not _has_continuation(v, nxt, pitch)}
        if not assigned:
            continue
        strong = sum(1 for lab in labels if _strong_label(lab))
        numeric = sum(1 for v in assigned.values() if _numeric_value(v) or v.datelike())
        if strong < len(labels) / 2 and numeric < len(assigned) / 2:
            continue
        if len(labels) == 1:
            lab, val = labels[0], next(iter(assigned.values()))
            if not (_strong_label(lab) or _numeric_value(val) or val.datelike()):
                continue
            if lab.em() >= 0.95 * val.em() and not lab.text.endswith(":") and val.n_words > 3:
                continue
        pairs = [
            (lab.text, assigned[k].text if k in assigned else "") for k, lab in enumerate(labels)
        ]
        segs = list(labels) + [assigned[k] for k in sorted(assigned)]
        used.update(id(s) for s in segs)
        out.append(VerticalKv(i, i + 1, _format_pairs(pairs), segs))
    return out
