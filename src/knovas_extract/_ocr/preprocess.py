"""Render + preprocess one PDF page for OCR (numpy + Pillow only, no OpenCV).

Ported from the measured benchmark pipeline (`bench/ocr/code/engines.py`,
configuration ``tfast_pro3f``):

- `choose_render_dpi`: ``min(300, native dpi)`` rounded to 10 — never
  upsample (a 150 dpi fax OCR'd at 300 dpi: CER 0.145; at 150 dpi: 0.028).
- `render_gray`: gray, no alpha, RAW samples straight from the pixmap —
  never PNG (encoding costs 1.3 s/page, the raw copy 1 ms).
  `fitz.TOOLS.store_shrink(100)` after each render keeps MuPDF's store
  from holding every rendered page.
- `sideways_ratio` → OSD only when the page is sideways → `rot90k`.
- `estimate_skew` (projection profile, ±3°) → `rotate_arr` when |angle| ≥ 0.15°.
- `detect_rules`: ruling lines and dot leaders found by 1-D run-length
  opening; the image is left intact and words lying on rule ink are
  dropped after OCR (`filter_rule_words`). Binarisation is left to Tesseract.
- `Limits.max_ocr_image_megapixels` is enforced by the pipeline from
  `get_image_info()` BEFORE any render (`image_megapixels`,
  `render_megapixels`): a 12000×12000 1-bit image that is 20 KB on disk is
  never decoded.

Every matrix follows the PyMuPDF convention ``(a, b, c, d, e, f)``:
``x' = a·x + c·y + e``, ``y' = b·x + d·y + f``; ``mat_mul(m1, m2)`` applies
``m1`` first.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from knovas_extract._ocr.tsv import (
    IDENTITY,
    MatrixT,
    OcrWord,
    mat_invert,
    mat_mul,
    mat_scale,
)

MAX_RENDER_DPI = 300
MIN_RENDER_DPI = 72
SIDEWAYS_RATIO_THRESHOLD = 1.0
DESKEW_MIN_DEG = 0.15
DESKEW_MAX_DEG = 3.0
_MIN_INK_FRACTION = 0.001  # below this a page is blank: no autorot / deskew
_LEADER_ONLY = re.compile(r"[.…·_\-–—=|:,;'`´~]+")  # noqa: RUF001
_MAX_RUNS = 1_500_000  # more ink runs than this = halftone, skip rule detection
_MAX_BAND_FACTOR = 4  # a band taller than 4 x `thin` rows is a filled box, not rules

NDArray = Any  # numpy arrays are typed loosely: the lint env may not have numpy


# ────────────────────────────────────────────────────────────────────────────
# Page / image inspection (no numpy needed)
# ────────────────────────────────────────────────────────────────────────────


def image_infos(page: Any) -> list[dict[str, Any]]:
    """`page.get_image_info(xrefs=True)`; `[]` when PyMuPDF cannot enumerate."""
    try:
        return [dict(i) for i in (page.get_image_info(xrefs=True) or [])]
    except Exception:
        return []


def largest_image(infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_px = -1
    for info in infos:
        try:
            px = int(info.get("width", 0)) * int(info.get("height", 0))
        except (TypeError, ValueError):
            continue
        if px > best_px:
            best, best_px = info, px
    return best


def native_dpi(page: Any, infos: list[dict[str, Any]] | None = None) -> int:
    """Resolution of the largest raster on the page as placed on the page
    (pixels per inch of its bbox width); 300 when the page has no image."""
    if infos is None:
        infos = image_infos(page)
    info = largest_image(infos)
    if info is None:
        return MAX_RENDER_DPI
    try:
        width_px = float(info["width"])
        bbox = info.get("bbox")
        width_pt = float(bbox[2]) - float(bbox[0]) if bbox else float(page.rect.width)
        if width_pt <= 1.0:
            width_pt = float(page.rect.width)
        return max(MIN_RENDER_DPI, round(width_px / (width_pt / 72.0)))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return MAX_RENDER_DPI


def choose_render_dpi(page: Any, infos: list[dict[str, Any]], requested: int | None) -> int:
    """`requested` when given, else ``min(300, native)`` rounded to 10 dpi."""
    if requested is not None:
        return int(requested)
    dpi = min(MAX_RENDER_DPI, native_dpi(page, infos))
    dpi = int(round(dpi / 10.0) * 10)
    return max(MIN_RENDER_DPI, dpi)


def image_megapixels(infos: list[dict[str, Any]]) -> float:
    """Pixel count of the largest embedded image, in megapixels."""
    info = largest_image(infos)
    if info is None:
        return 0.0
    try:
        return int(info["width"]) * int(info["height"]) / 1e6
    except (KeyError, TypeError, ValueError):
        return 0.0


def render_megapixels(page: Any, dpi: int) -> float:
    """Pixel count of the page rendered at `dpi`, in megapixels."""
    try:
        w = float(page.rect.width) * dpi / 72.0
        h = float(page.rect.height) * dpi / 72.0
    except Exception:
        return 0.0
    return max(0.0, w * h / 1e6)


def render_gray(page: Any, dpi: int, *, colour_dropout: bool = False) -> tuple[bytes, int, int]:
    """Render the page to 8-bit gray. Returns ``(raw samples, width, height)``.

    With `colour_dropout` the page is rendered RGB and the brightest
    channel per pixel is kept (coloured stamps and highlighter vanish,
    black text stays). Never encodes PNG.
    """
    import fitz

    try:
        if colour_dropout:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            samples = np.ascontiguousarray(rgb.max(axis=2)).tobytes()
        else:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
            samples = bytes(pix.samples)
        width, height = int(pix.width), int(pix.height)
        del pix
    finally:
        # Drop MuPDF's cached page resources (decoded images) so a long
        # scan does not accumulate every rendered page in the store.
        with contextlib.suppress(Exception):
            fitz.TOOLS.store_shrink(100)
    return samples, width, height


# ────────────────────────────────────────────────────────────────────────────
# numpy helpers
# ────────────────────────────────────────────────────────────────────────────


def to_array(samples: bytes, width: int, height: int) -> NDArray:
    return np.frombuffer(samples, dtype=np.uint8).reshape(height, width).copy()


def pgm_bytes(arr: NDArray) -> bytes:
    h, w = arr.shape
    data: bytes = np.ascontiguousarray(arr).tobytes()
    return b"P5\n%d %d\n255\n" % (w, h) + data


def otsu_threshold(arr: NDArray) -> int:
    """Otsu's threshold on an 8-bit gray array (ink = pixels <= threshold)."""
    # Histogram on a 2x subsample for large pages: the threshold is a global
    # statistic and the 4x cheaper histogram shifts it by < 1 level.
    src = arr[::2, ::2] if arr.size > 2_000_000 else arr
    hist = np.bincount(np.ascontiguousarray(src).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 128
    levels = np.arange(256, dtype=np.float64)
    w0 = np.cumsum(hist)
    w1 = total - w0
    sum0 = np.cumsum(hist * levels)
    sum_all = sum0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        mu0 = np.where(w0 > 0, sum0 / np.maximum(w0, 1), 0.0)
        mu1 = np.where(w1 > 0, (sum_all - sum0) / np.maximum(w1, 1), 0.0)
        between = w0 * w1 * (mu0 - mu1) ** 2
    between[(w0 == 0) | (w1 == 0)] = 0.0
    return int(np.argmax(between))


def ink_mask(arr: NDArray) -> NDArray:
    return arr <= otsu_threshold(arr)


def _run_bounds_rows(mask: NDArray) -> tuple[NDArray, NDArray]:
    """For every pixel of a 2-D bool mask: the first and last column index
    of the True-run it belongs to along axis 1 (meaningless where False)."""
    h, w = mask.shape
    xs = np.broadcast_to(np.arange(w, dtype=np.int32), (h, w))
    last0 = np.maximum.accumulate(np.where(mask, -1, xs), axis=1)
    next0 = np.minimum.accumulate(np.where(mask, w, xs)[:, ::-1], axis=1)[:, ::-1]
    return last0 + 1, next0 - 1


def run_lengths(mask: NDArray, axis: int) -> NDArray:
    """Length of the True-run every pixel belongs to along `axis` (0 where False)."""
    if axis == 0:
        return run_lengths(mask.T, 1).T
    start, end = _run_bounds_rows(mask)
    return np.where(mask, end - start + 1, 0)


def _thumbnail(arr: NDArray, max_side: int) -> NDArray:
    from PIL import Image

    h, w = arr.shape
    f = max_side / float(max(h, w))
    if f >= 1.0:
        return arr
    nw, nh = max(1, int(round(w * f))), max(1, int(round(h * f)))
    im = Image.fromarray(np.ascontiguousarray(arr)).resize((nw, nh), Image.Resampling.BOX)
    return np.asarray(im, dtype=np.uint8)


def sideways_ratio(arr: NDArray) -> float:
    """Row-profile vs column-profile "line-ness" on a 600 px thumbnail with
    long rules removed. Upright text pages score >= 3, pages rotated by
    90/270° < 0.2 (measured on the benchmark corpus); blank pages return a
    large value so they are never rotated."""
    small = _thumbnail(arr, 600)
    ink = ink_mask(small)
    if ink.mean() < _MIN_INK_FRACTION:
        return 10.0
    hl = run_lengths(ink, 1) >= 40
    vl = run_lengths(ink, 0) >= 40
    bw = ink & ~hl & ~vl
    r = bw.sum(axis=1).astype(np.float64)
    c = bw.sum(axis=0).astype(np.float64)
    sr = float(np.mean(np.diff(r) ** 2)) / max(1e-9, float(np.mean(r)) ** 2)
    sc = float(np.mean(np.diff(c) ** 2)) / max(1e-9, float(np.mean(c)) ** 2)
    return float(sr / max(sc, 1e-9))


def rot90k(arr: NDArray, k: int) -> tuple[NDArray, MatrixT]:
    """`np.rot90` (CCW by k·90°) and the pixel matrix src → dst."""
    h, w = arr.shape
    k %= 4
    out = np.ascontiguousarray(np.rot90(arr, k))
    if k == 0:
        m: MatrixT = IDENTITY
    elif k == 1:  # x' = y, y' = w - x
        m = (0.0, -1.0, 1.0, 0.0, 0.0, float(w))
    elif k == 2:  # x' = w - x, y' = h - y
        m = (-1.0, 0.0, 0.0, -1.0, float(w), float(h))
    else:  # x' = h - y, y' = x
        m = (0.0, 1.0, -1.0, 0.0, float(h), 0.0)
    return out, m


def estimate_skew(arr: NDArray, max_deg: float = DESKEW_MAX_DEG) -> float:
    """Projection-profile skew estimate on a ~700 px Otsu thumbnail.

    Returns the angle in degrees (CCW, PIL's convention) by which the
    content is rotated; 0.0 for blank pages.
    """
    from PIL import Image

    small = _thumbnail(arr, 700)
    ink = ink_mask(small)
    if ink.mean() < _MIN_INK_FRACTION:
        return 0.0
    im = Image.fromarray(ink.astype(np.uint8) * 255)

    def score(a: float) -> float:
        r = np.asarray(im.rotate(-a, resample=Image.Resampling.NEAREST, fillcolor=0))
        s = r.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(s) ** 2))

    base = score(0.0)
    if base <= 0.0:
        return 0.0
    # Ties resolve toward 0 (the grids are sorted by |angle|), and a tiny
    # score gain is noise: never rotate a clean page by the grid step.
    grid = sorted(np.arange(-max_deg, max_deg + 1e-9, 0.25), key=abs)
    best = float(max(grid, key=score))
    fine = sorted(np.arange(best - 0.25, best + 0.25 + 1e-9, 0.05), key=abs)
    best = float(max(fine, key=score))
    if score(best) < 1.02 * base:
        return 0.0
    return round(best, 3)


def rotate_arr(arr: NDArray, angle_deg: float) -> tuple[NDArray, MatrixT]:
    """Undo a CCW rotation of `angle_deg` about the centre (white fill).
    Returns the rotated image and the pixel matrix src → dst."""
    from PIL import Image

    h, w = arr.shape
    im = Image.fromarray(np.ascontiguousarray(arr)).rotate(
        -angle_deg, resample=Image.Resampling.BILINEAR, fillcolor=255
    )
    out = np.array(im, dtype=np.uint8)
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    cx, cy = w / 2.0, h / 2.0
    m: MatrixT = (
        cos_a,
        sin_a,
        -sin_a,
        cos_a,
        cx - cos_a * cx + sin_a * cy,
        cy - sin_a * cx - cos_a * cy,
    )
    return out, m


# ────────────────────────────────────────────────────────────────────────────
# Ruling lines / dot leaders
# ────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RuleMask:
    rule_ink: NDArray  # bool HxW — ink pixels that belong to rules / leaders
    ink: NDArray  # bool HxW — all ink
    h_rules: int = 0
    v_rules: int = 0
    boxes: list[tuple[str, int, int, int, int]] = field(default_factory=list)

    @property
    def ink_fraction(self) -> float:
        return float(self.ink.mean()) if self.ink.size else 0.0


def _long_runs(sub: NDArray, gap: int, length: int) -> NDArray:
    """Pixels of `sub` (bool, C-contiguous) lying in a horizontal run of ink
    whose gaps of ≤ `gap` px are closed and whose closed length is ≥
    `length` px — a 1-D closing followed by a 1-D opening, computed on the
    run-length encoding of the rows (O(runs), ~10 ms on a 300 dpi page)
    instead of on every pixel."""
    n, w = sub.shape
    mask: NDArray = np.zeros((n, w), dtype=bool)
    if n == 0 or w == 0:
        return mask
    padded = np.zeros((n, w + 1), dtype=bool)
    padded[:, :w] = sub
    flat = padded.ravel()
    d = np.diff(flat.view(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1  # exclusive
    if flat[0]:
        starts = np.concatenate(([0], starts))
    if starts.size == 0 or starts.size != ends.size or starts.size > _MAX_RUNS:
        return mask  # halftone / noise: no rule search on a page made of runs
    stride = w + 1
    rows = starts // stride
    merge = ((starts[1:] - ends[:-1]) <= gap) & (rows[1:] == rows[:-1])
    group = np.concatenate(([0], np.cumsum(~merge)))
    first = np.flatnonzero(np.concatenate(([True], group[1:] != group[:-1])))
    last = np.concatenate((first[1:] - 1, [group.size - 1]))
    g_start = starts[first]
    g_end = ends[last]
    keep = (g_end - g_start) >= max(1, length)
    for s, e in zip(g_start[keep].tolist(), g_end[keep].tolist(), strict=True):
        r = s // stride
        mask[r, s - r * stride : e - r * stride] = True
    return mask


def _bands(rows: NDArray) -> list[tuple[int, int]]:
    """Group sorted row indices into contiguous inclusive ranges."""
    if rows.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(rows) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [rows.size - 1]))
    return [(int(rows[a]), int(rows[b])) for a, b in zip(starts, ends, strict=True)]


def _rules_along_rows(
    ink: NDArray, length: int, gap: int, thin: int, d: int
) -> tuple[NDArray, list[tuple[int, int, int, int]]]:
    """Horizontal rule pixels of `ink` (call with ``ink.T`` for vertical).

    A rule is a run of ink (gaps ≤ `gap` px closed, so dotted leaders
    join) at least `length` px long whose thickness is ≤ `thin` px and
    whose `d` px side strips are almost ink-free on BOTH sides — text that
    touches a rule (serifs, underlines) always has letter bodies on one
    side, so it is never mistaken for one.

    The long-run search works on the run-length encoding of the inked
    rows, the thickness and strip tests only on the thin horizontal bands
    that contain long runs — ~50 ms for a 300 dpi page.
    """
    h, w = ink.shape
    mask: NDArray = np.zeros_like(ink)
    rows = np.flatnonzero(ink.any(axis=1))
    if rows.size == 0:
        return mask, []
    # Every other pixel along the run direction: a 1 px rule still appears
    # in every row it occupies, runs and gaps halve, the pass is 2x cheaper.
    sub = np.ascontiguousarray(ink[rows][:, ::2])
    long_half = _long_runs(sub, max(1, gap // 2), max(1, length // 2))
    long_sub = np.repeat(long_half, 2, axis=1)[:, :w]
    has_long = long_sub.any(axis=1)
    if not has_long.any():
        return mask, []
    long: NDArray = np.zeros_like(ink)
    long[rows] = long_sub
    boxes: list[tuple[int, int, int, int]] = []
    for y0, y1 in _bands(rows[has_long]):
        if y1 - y0 + 1 > _MAX_BAND_FACTOR * thin:
            continue  # a filled region (box, image), not a ruling line
        band = np.ascontiguousarray(long[y0 : y1 + 1])
        band_t = np.ascontiguousarray(band.T)
        t_start, t_end = _run_bounds_rows(band_t)  # vertical runs, per column
        thickness = t_end - t_start + 1
        cand = np.ascontiguousarray((band_t & (thickness <= thin)).T)
        if not cand.any():
            continue
        ys, xs = np.nonzero(cand)
        top = t_start.T[ys, xs] + y0
        bot = t_end.T[ys, xs] + y0
        above = np.zeros(ys.size, dtype=bool)
        below = np.zeros(ys.size, dtype=bool)
        for off in range(2, d + 2):
            ra = top - off
            ok = ra >= 0
            above[ok] |= ink[ra[ok], xs[ok]]
            rb = bot + off
            ok = rb < h
            below[ok] |= ink[rb[ok], xs[ok]]
        row_start, _ = _run_bounds_rows(cand)
        run_key = ys.astype(np.int64) * w + row_start[ys, xs]
        _, inverse = np.unique(run_key, return_inverse=True)
        n = np.bincount(inverse).astype(np.float64)
        fa = np.bincount(inverse, weights=above) / n
        fb = np.bincount(inverse, weights=below) / n
        kept = ((fa <= 0.10) & (fb <= 0.10))[inverse]
        if not kept.any():
            continue
        mask[ys[kept] + y0, xs[kept]] = True
        tk, xk, bk = top[kept], xs[kept], bot[kept]
        uniq, inv = np.unique(tk, return_inverse=True)
        bx0 = np.full(uniq.size, w, dtype=np.int64)
        bx1 = np.zeros(uniq.size, dtype=np.int64)
        by1 = np.zeros(uniq.size, dtype=np.int64)
        np.minimum.at(bx0, inv, xk)
        np.maximum.at(bx1, inv, xk)
        np.maximum.at(by1, inv, bk)
        boxes += [
            (int(bx0[i]), int(uniq[i]), int(bx1[i]) + 1, int(by1[i]) + 1) for i in range(uniq.size)
        ]
    return mask, boxes


def _dilate3(mask: NDArray) -> NDArray:
    out = mask.copy()
    h, w = mask.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            src_y = slice(max(0, -dy), h - max(0, dy))
            dst_y = slice(max(0, dy), h - max(0, -dy))
            src_x = slice(max(0, -dx), w - max(0, dx))
            dst_x = slice(max(0, dx), w - max(0, -dx))
            out[dst_y, dst_x] |= mask[src_y, src_x]
    return out


def detect_rules(arr: NDArray, dpi: int) -> RuleMask:
    """Find ruling lines and dot leaders; the image itself is left intact."""
    ink = ink_mask(arr)
    k = dpi / 72.0
    gap = max(3, int(round(3.0 * k)))
    hlen, vlen = int(36 * k), int(30 * k)
    thin = max(3, int(round(1.6 * k)) + 2)
    d = max(2, int(round(1.2 * k)))
    hmask, hboxes = _rules_along_rows(ink, hlen, gap, thin, d)
    vmask_t, vboxes_t = _rules_along_rows(np.ascontiguousarray(ink.T), vlen, gap, thin, d)
    vmask = vmask_t.T
    rule = _dilate3(hmask | vmask) & ink
    boxes: list[tuple[str, int, int, int, int]] = [("h", *b) for b in hboxes]
    boxes += [("v", y0, x0, y1, x1) for (x0, y0, x1, y1) in vboxes_t]
    return RuleMask(rule_ink=rule, ink=ink, h_rules=len(hboxes), v_rules=len(vboxes_t), boxes=boxes)


def filter_rule_words(
    words_px: list[OcrWord], rules: RuleMask, min_cover: float = 0.6
) -> tuple[list[OcrWord], int]:
    """Drop words (boxes in frame pixels) whose ink is mostly rule / leader
    ink, or that are pure leader punctuation lying on a rule."""
    keep: list[OcrWord] = []
    dropped = 0
    h, w = rules.ink.shape
    for word in words_px:
        x0 = max(0, int(round(word.x0)))
        y0 = max(0, int(round(word.y0)))
        x1 = min(w, int(round(word.x1)) + 1)
        y1 = min(h, int(round(word.y1)) + 1)
        if x1 <= x0 or y1 <= y0:
            keep.append(word)
            continue
        sub_ink = rules.ink[y0:y1, x0:x1]
        n = int(sub_ink.sum())
        cover = (float(rules.rule_ink[y0:y1, x0:x1].sum()) / n) if n else 0.0
        if cover >= min_cover or (cover >= 0.2 and _LEADER_ONLY.fullmatch(word.text.strip())):
            dropped += 1
            continue
        keep.append(word)
    return keep, dropped


# ────────────────────────────────────────────────────────────────────────────
# The page image handed to a backend
# ────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PageImage:
    """What an `IOcrBackend.recognize` call receives.

    `gray` is the preprocessed OCR frame (H×W uint8, may be rotated /
    deskewed); `frame_to_page` maps frame PIXELS to page POINTS. `page` is
    set only for the MuPDF backend (which renders itself and must run on
    the calling thread). `rules`, when present, is consulted after OCR.
    """

    page_index: int
    dpi: int
    width: int
    height: int
    page_rect: tuple[float, float, float, float]
    language: str
    psm: int
    gray: Any = None
    frame_to_page: MatrixT = IDENTITY
    rotation: int = 0
    rot90: int = 0
    skew: float = 0.0
    rules: RuleMask | None = None
    page: Any = None
    tessdata_dir: str | None = None
    cache_key: str | None = None


OsdFn = Callable[[Any, int], int | None]


def preprocess(
    pi: PageImage,
    *,
    autorot: bool = True,
    deskew: bool = True,
    rules: bool = True,
    osd: OsdFn | None = None,
) -> PageImage:
    """Apply autorotation, deskew and rule detection to `pi.gray` in place
    and set `frame_to_page` accordingly."""
    arr = pi.gray
    if arr is None:
        return pi
    frame: MatrixT = IDENTITY
    if autorot and sideways_ratio(arr) < SIDEWAYS_RATIO_THRESHOLD:
        rot: int | None = None
        if osd is not None:
            try:
                rot = osd(arr, pi.dpi)
            except Exception:
                rot = None
        if rot not in (90, 270):
            rot = 90  # sideways for sure, direction unknown: a coin toss beats nothing
        arr, m = rot90k(arr, -(rot // 90))
        frame = mat_mul(frame, m)
        pi.rot90 = rot
    if deskew:
        ang = estimate_skew(arr)
        if abs(ang) >= DESKEW_MIN_DEG:
            arr, m = rotate_arr(arr, ang)
            frame = mat_mul(frame, m)
            pi.skew = round(ang, 3)
    if rules:
        pi.rules = detect_rules(arr, pi.dpi)
    pi.gray = arr
    pi.height, pi.width = int(arr.shape[0]), int(arr.shape[1])
    pi.frame_to_page = mat_mul(mat_invert(frame), mat_scale(72.0 / pi.dpi))
    return pi


def count_text_lines(words: list[OcrWord]) -> int:
    return len({(w.block, w.par, w.line) for w in words})
