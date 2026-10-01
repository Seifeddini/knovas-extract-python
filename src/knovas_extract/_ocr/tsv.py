"""Tesseract TSV → `OcrWord` list, text rendering and frame→page mapping.

Both Tesseract backends (in-process `tesserocr` and the CLI) produce the
same TSV table (``level … conf text``); the CLI renderer adds the header
row, `PyTessBaseAPI.GetTSVText` does not. Level-5 rows are words; their
boxes are in pixels of the OCR *frame* (the possibly rotated/deskewed
render) and are mapped to page points through `frame_to_page`.

Nothing here touches numpy or Tesseract — it is pure string/number work
and is unit-tested from a committed TSV fixture.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field

TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"

MatrixT = tuple[float, float, float, float, float, float]
IDENTITY: MatrixT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One recognised word with its box (points), confidence and position
    in Tesseract's block / paragraph / line hierarchy.

    `line_h` is the height (same units as the box) of the text line the
    word belongs to — the layout renderer uses it as the font-size proxy
    on OCR pages.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float
    block: int
    par: int
    line: int
    line_h: float


@dataclass(slots=True)
class OcrPageResult:
    """What one OCR'd page yields: the plain text, the words with boxes in
    page points (may be empty for backends that only return text) and the
    mean word confidence (``None`` when the backend reports none)."""

    text: str
    words: list[OcrWord] = field(default_factory=list)
    mean_conf: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "mean_conf": self.mean_conf,
                "words": [
                    [w.text, w.x0, w.y0, w.x1, w.y1, w.conf, w.block, w.par, w.line, w.line_h]
                    for w in self.words
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str) -> OcrPageResult:
        d = json.loads(payload)
        words = [
            OcrWord(
                text=str(t),
                x0=float(x0),
                y0=float(y0),
                x1=float(x1),
                y1=float(y1),
                conf=float(conf),
                block=int(b),
                par=int(p),
                line=int(ln),
                line_h=float(lh),
            )
            for t, x0, y0, x1, y1, conf, b, p, ln, lh in d.get("words", [])
        ]
        mc = d.get("mean_conf")
        return cls(
            text=str(d.get("text", "")), words=words, mean_conf=None if mc is None else float(mc)
        )


def parse_tsv(tsv: str, px_to_pt: float = 1.0) -> list[OcrWord]:
    """Parse Tesseract TSV output into level-5 words, scaling boxes by `px_to_pt`.

    Rows with an empty / whitespace-only ``text`` are dropped (Tesseract
    emits them for empty cells). Malformed rows are skipped, never raised —
    a backend producing odd output must not fail the page.
    """
    if not tsv.startswith("level"):
        tsv = TSV_HEADER + tsv
    words: list[OcrWord] = []
    line_h = 0.0
    for r in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        level = r.get("level")
        try:
            if level == "4":
                line_h = float(r["height"]) * px_to_pt
                continue
            if level != "5":
                continue
            text = (r.get("text") or "").strip()
            if not text:
                continue
            x, y, w, h = (float(r[k]) for k in ("left", "top", "width", "height"))
            words.append(
                OcrWord(
                    text=text,
                    x0=round(x * px_to_pt, 2),
                    y0=round(y * px_to_pt, 2),
                    x1=round((x + w) * px_to_pt, 2),
                    y1=round((y + h) * px_to_pt, 2),
                    conf=float(r["conf"]),
                    block=int(r["block_num"]),
                    par=int(r["par_num"]),
                    line=int(r["line_num"]),
                    line_h=round(line_h if line_h > 0 else h * px_to_pt, 2),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return words


def words_to_text(words: list[OcrWord]) -> str:
    """Engine reading order: words joined by single spaces, a newline on
    every line change, a blank line on every block change."""
    lines: list[tuple[int, str]] = []
    cur: list[str] = []
    key: tuple[int, int, int] | None = None
    for w in words:
        k = (w.block, w.par, w.line)
        if key is not None and k != key:
            lines.append((key[0], " ".join(cur)))
            cur = []
        cur.append(w.text)
        key = k
    if cur and key is not None:
        lines.append((key[0], " ".join(cur)))
    out: list[str] = []
    prev: int | None = None
    for block, line in lines:
        if prev is not None and block != prev:
            out.append("")
        out.append(line)
        prev = block
    return "\n".join(out)


def mean_confidence(words: list[OcrWord]) -> float | None:
    confs = [w.conf for w in words if w.conf >= 0]
    if not confs:
        return None
    return round(sum(confs) / len(confs), 2)


def mat_mul(m1: MatrixT, m2: MatrixT) -> MatrixT:
    """Compose two affine matrices (PyMuPDF convention: apply `m1`, then `m2`)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def mat_invert(m: MatrixT) -> MatrixT:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return IDENTITY
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def mat_apply(m: MatrixT, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def mat_scale(s: float) -> MatrixT:
    return (s, 0.0, 0.0, s, 0.0, 0.0)


def frame_to_page(words: list[OcrWord], matrix: MatrixT) -> list[OcrWord]:
    """Map word boxes from the OCR frame to page points through `matrix`,
    re-normalising each box so x0 <= x1 and y0 <= y1 after a rotation."""
    if matrix == IDENTITY:
        return words
    out: list[OcrWord] = []
    for w in words:
        xa, ya = mat_apply(matrix, w.x0, w.y0)
        xb, yb = mat_apply(matrix, w.x1, w.y1)
        xc, yc = mat_apply(matrix, w.x0, w.y1)
        xd, yd = mat_apply(matrix, w.x1, w.y0)
        xs = (xa, xb, xc, xd)
        ys = (ya, yb, yc, yd)
        # The line height is a length: scale it by the matrix' linear part.
        a, b, c, d, _, _ = matrix
        scale = ((a * a + b * b) ** 0.5 + (c * c + d * d) ** 0.5) / 2.0
        out.append(
            OcrWord(
                text=w.text,
                x0=round(min(xs), 2),
                y0=round(min(ys), 2),
                x1=round(max(xs), 2),
                y1=round(max(ys), 2),
                conf=w.conf,
                block=w.block,
                par=w.par,
                line=w.line,
                line_h=round(w.line_h * scale, 2),
            )
        )
    return out
