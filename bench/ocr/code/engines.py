"""OCR engine configurations. Every engine returns words with boxes in the *OCR frame* (points),
plus `frame_to_page` (PyMuPDF matrix a,b,c,d,e,f) mapping OCR-frame points -> PDF page points.
The frame differs from the page only when the pipeline rotates the image (deskew / OSD)."""
import csv, io, math, os, subprocess, time
from pathlib import Path
import numpy as np
import pymupdf

OCRBENCH = Path(__file__).resolve().parents[2]
TESSDATA = {"fast": str(OCRBENCH / "tessdata_fast"), "best": str(OCRBENCH / "tessdata_best")}

# name -> spec
CONFIGS = {
    # --- baselines: MuPDF's built-in Tesseract via PyMuPDF (what knovas-extract uses today)
    "mupdf_statusquo": dict(engine="mupdf", lang="deu+eng", td="fast", dpi=None, full=False,
                            note="knovas-extract today: page.get_textpage_ocr(language='deu+eng') -> dpi 72, partial"),
    "mupdf_300_full": dict(engine="mupdf", lang="deu+eng", td="fast", dpi=300, full=True,
                           note="get_textpage_ocr(dpi=300, full=True)"),
    # --- Tesseract 5.3.4 CLI, gray render, PGM via stdin, TSV word boxes
    "tfast_psm3": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi=300),
    "tfast_psm4": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300),
    "tfast_psm6": dict(engine="tess", lang="deu+eng", td="fast", psm=6, dpi=300),
    "tfast_psm11": dict(engine="tess", lang="deu+eng", td="fast", psm=11, dpi=300),
    "tbest_psm3": dict(engine="tess", lang="deu+eng", td="best", psm=3, dpi=300),
    "tbest_psm4": dict(engine="tess", lang="deu+eng", td="best", psm=4, dpi=300),
    "tbest_psm6": dict(engine="tess", lang="deu+eng", td="best", psm=6, dpi=300),
    "tfast_psm4_400": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=400),
    "tbest_psm4_400": dict(engine="tess", lang="deu+eng", td="best", psm=4, dpi=400),
    "tfast_psm4_native": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi="native"),
    "tfast_psm4_sauvola": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300, params={"thresholding_method": "2"}),
    "tfast_psm4_deskew": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300, deskew=True),
    # psm 3 based pipelines (psm 3 won on CER, numbers and reading order in batch 1)
    # dpi "auto" = native image resolution capped at 300 (no upsampling of 150/200 dpi scans)
    "tfast_psm3_native": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi="auto"),
    "tfast_psm3_nolines": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi=300, erase_lines=True,
                               note="erase table rules + dot leaders (morphology) before OCR; rules kept as metadata"),
    "tfast_pro3": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi="auto", autorot=True, erase_lines=True),
    "tfast_pro3_deskew": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi="auto", autorot=True, deskew=True,
                              erase_lines=True),
    "tbest_pro3": dict(engine="tess", lang="deu+eng", td="best", psm=3, dpi="auto", autorot=True, erase_lines=True),
    "tbest_pro3_dfe": dict(engine="tess", lang="deu+fra+eng", td="best", psm=3, dpi="auto", autorot=True, erase_lines=True),
    "tfast_pro3_dfe": dict(engine="tess", lang="deu+fra+eng", td="fast", psm=3, dpi="auto", autorot=True, erase_lines=True),
    "tfast_psm4_noinvert": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300, params={"tessedit_do_invert": "0"}),
    "tfast_psm4_dfe": dict(engine="tess", lang="deu+fra+eng", td="fast", psm=4, dpi=300),
    "tbest_psm4_dfe": dict(engine="tess", lang="deu+fra+eng", td="best", psm=4, dpi=300),
    "tfast_psm4_osd": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300, osd=True),
    "tfast_psm4_autorot": dict(engine="tess", lang="deu+eng", td="fast", psm=4, dpi=300, autorot=True,
                               note="4 ms projection-profile sideways check; OSD only for sideways pages"),
    # --- in-process API (tesserocr 2.11 wheel bundles Tesseract 5.5.1): no process spawn / model reload per page
    # detect rules/leaders but do NOT erase: OCR the untouched image, then drop words lying on rule/leader ink
    "tfast_pro3f": dict(engine="tess", lang="deu+eng", td="fast", psm=3, dpi="auto", autorot=True, deskew=True,
                        filter_rules=True),
    "tbest_pro3f_dfe": dict(engine="tess", lang="deu+fra+eng", td="best", psm=3, dpi="auto", autorot=True, deskew=True,
                            filter_rules=True),
    "tapi_fast_pro3f": dict(engine="tesserocr", lang="deu+eng", td="fast", psm=3, dpi="auto", autorot=True, deskew=True,
                            filter_rules=True),
    "tapi_fast_pro3f_dfe": dict(engine="tesserocr", lang="deu+fra+eng", td="fast", psm=3, dpi="auto", autorot=True,
                                deskew=True, filter_rules=True),
    "tapi_best_pro3f_dfe": dict(engine="tesserocr", lang="deu+fra+eng", td="best", psm=3, dpi="auto", autorot=True,
                                deskew=True, filter_rules=True),
    "tapi_fast_psm3": dict(engine="tesserocr", lang="deu+eng", td="fast", psm=3, dpi=300),
    # confidence-gated hybrid: fast model on the page, best model (psm 7) re-reads only lines with a word conf < thr
    "tapi_hybrid85": dict(engine="tesserocr_hybrid", lang="deu+eng", td="fast", psm=3, dpi=300, thr=85),
    # --- RapidOCR 1.4.4 with the models bundled in the wheel (PP-OCRv4 ch/en, no Latin-extended model)
    "rapidocr_bundled": dict(engine="rapidocr", dpi=300),
    # --- sanity check on born-digital pages: PyMuPDF text layer
    "pymupdf_textlayer": dict(engine="textlayer"),
}

DEFAULT_PARAMS = {"preserve_interword_spaces": "1"}


# A hung engine must not block a nightly job until the job timeout (critique 2026-10-01).
OCR_SUBPROCESS_TIMEOUT_S = 120

def M(*v):
    return pymupdf.Matrix(*v)


def mat_list(m):
    return [round(float(x), 6) for x in (m.a, m.b, m.c, m.d, m.e, m.f)]


def native_dpi(page):
    infos = page.get_image_info()
    if not infos:
        return 300
    i = max(infos, key=lambda d: d["width"] * d["height"])
    return max(72, round(i["width"] / (page.rect.width / 72)))


def render_gray(page, dpi):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def pgm_bytes(arr):
    h, w = arr.shape
    return b"P5\n%d %d\n255\n" % (w, h) + np.ascontiguousarray(arr).tobytes()


# ------------------------------------------------------------------ preprocessing
def estimate_skew(arr, max_deg=3.0):
    """Projection-profile skew estimate on a ~75 dpi Otsu-binarised thumbnail. Returns degrees CCW
    that the *content* is rotated by (same sign convention as PIL Image.rotate)."""
    import cv2
    f = 900.0 / max(arr.shape)
    small = cv2.resize(arr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = bw.shape
    c = (w / 2, h / 2)

    def score(a):
        R = cv2.getRotationMatrix2D(c, -a, 1.0)  # undo a CCW rotation of a degrees
        r = cv2.warpAffine(bw, R, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        s = r.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(s) ** 2))

    grid = np.arange(-max_deg, max_deg + 1e-9, 0.25)
    best = max(grid, key=score)
    fine = np.arange(best - 0.25, best + 0.25 + 1e-9, 0.05)
    return float(max(fine, key=score))


def _strip_ink(ink, comp, horiz, d):
    """Ink share in the d-px strips that hug a (possibly skewed) line component on both sides, per column/row."""
    m = comp if horiz else comp.T
    I = ink if horiz else ink.T
    cols = np.where(m.any(axis=0))[0]
    if cols.size == 0:
        return 1.0, 1.0
    top = m[:, cols].argmax(axis=0)
    bot = m.shape[0] - 1 - m[::-1, cols].argmax(axis=0)
    a, b = [], []
    for off in range(2, d + 2):
        ya, yb = top - off, bot + off
        ok_a, ok_b = ya >= 0, yb < I.shape[0]
        a.append(I[ya[ok_a], cols[ok_a]] > 0)
        b.append(I[yb[ok_b], cols[ok_b]] > 0)
    fa = float(np.concatenate(a).mean()) if a and sum(x.size for x in a) else 0.0
    fb = float(np.concatenate(b).mean()) if b and sum(x.size for x in b) else 0.0
    return fa, fb


def erase_lines(arr, dpi, return_mask=False):
    """Remove ruling lines and dotted leaders before OCR. Returns (cleaned image, list of line boxes in px).
    Horizontal: Otsu ink -> close (gap ~3 pt, joins leader dots) -> open with a 36 pt x 1 px kernel -> keep
    components whose MEAN thickness is <= ~1.6 pt+2 px (skew-tolerant) and whose 1.2 pt side strips (following
    the line column by column) are almost ink-free on BOTH sides - joined text (serifs, x-height tops of long
    words) always has letter bodies on one side. Vertical: open with a 30 pt vertical kernel, same tests."""
    import cv2
    _, ink = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    k = dpi / 72.0
    gap = max(3, int(round(3.0 * k)))
    hlen, vlen = int(36 * k), int(30 * k)
    thin = max(3, int(round(1.6 * k)) + 2)
    d = max(2, int(round(1.2 * k)))
    joined = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 1)))
    hmask = cv2.morphologyEx(joined, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hlen, 1)))
    vmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vlen)))
    erase = np.zeros_like(ink)
    boxes = []
    for mask, horiz in ((hmask, True), (vmask, False)):
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            length = w if horiz else h
            if a / max(1, length) > thin:
                continue
            x0, y0 = max(0, x - d - 2), max(0, y - d - 2)
            x1, y1 = min(ink.shape[1], x + w + d + 2), min(ink.shape[0], y + h + d + 2)
            comp = lab[y0:y1, x0:x1] == i
            fa, fb = _strip_ink(ink[y0:y1, x0:x1], comp, horiz, d)
            if fa > 0.10 or fb > 0.10:
                continue
            erase[y0:y1, x0:x1][comp] = 255
            boxes.append(dict(kind="h" if horiz else "v", bbox=[int(x), int(y), int(x + w), int(y + h)]))
    erase = cv2.dilate(erase, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    if return_mask:
        return (erase > 0) & (ink > 0), ink > 0, boxes
    paper = int(np.median(arr[::8, ::8]))
    out = arr.copy()
    out[(erase > 0) & (ink > 0)] = paper
    return out, boxes


LEADER_ONLY = None


def filter_rule_words(words_px, rule_ink, ink, min_cover=0.6):
    """Drop OCR words (px boxes) whose ink is mostly rule/leader ink, or that are pure leader punctuation."""
    import re
    global LEADER_ONLY
    if LEADER_ONLY is None:
        LEADER_ONLY = re.compile(r"[.…·_\-–—=|:,;'`´~]+")
    keep, dropped = [], 0
    H_, W_ = ink.shape
    for w in words_px:
        x0, y0, x1, y1 = (int(round(v)) for v in w["bbox"])
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W_, x1 + 1), min(H_, y1 + 1)
        sub_ink = ink[y0:y1, x0:x1]
        n = int(sub_ink.sum())
        cover = (rule_ink[y0:y1, x0:x1].sum() / n) if n else 0.0
        t = w["text"].strip()
        if cover >= min_cover or (cover >= 0.2 and LEADER_ONLY.fullmatch(t)):
            dropped += 1
            continue
        keep.append(w)
    return keep, dropped


def rotate_arr(arr, angle_deg):
    """Rotate image content by -angle (i.e. undo a CCW rotation of angle) about the centre.
    Returns rotated image and the pixel matrix src->dst."""
    import cv2
    h, w = arr.shape
    R = cv2.getRotationMatrix2D((w / 2, h / 2), -angle_deg, 1.0)
    out = cv2.warpAffine(arr, R, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    return out, M(R[0, 0], R[1, 0], R[0, 1], R[1, 1], R[0, 2], R[1, 2])


def rot90k(arr, k):
    """np.rot90 (CCW k*90) and the pixel matrix src->dst."""
    h, w = arr.shape
    k %= 4
    out = np.ascontiguousarray(np.rot90(arr, k))
    if k == 0:
        m = M(1, 0, 0, 1, 0, 0)
    elif k == 1:   # x' = y, y' = w - x
        m = M(0, -1, 1, 0, 0, w)
    elif k == 2:   # x' = w - x, y' = h - y
        m = M(-1, 0, 0, -1, w, h)
    else:          # x' = h - y, y' = x
        m = M(0, 1, -1, 0, h, 0)
    return out, m


def sideways_ratio(arr):
    """Row-profile vs column-profile 'line-ness' on a 600 px Otsu thumbnail with long rules removed.
    Upright text pages give >= 3, pages rotated by 90/270 give < 0.2 (measured on this corpus)."""
    import cv2
    f = 600.0 / max(arr.shape)
    small = cv2.resize(arr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40)))
    bw = np.clip(bw.astype(np.int32) - hl - vl, 0, 1)
    r, c = bw.sum(1).astype(float), bw.sum(0).astype(float)
    sr = np.mean(np.diff(r) ** 2) / max(1e-9, np.mean(r) ** 2)
    sc = np.mean(np.diff(c) ** 2) / max(1e-9, np.mean(c) ** 2)
    return float(sr / max(sc, 1e-9))


def tess_osd(arr, td, scale=0.5, env=None):
    """Orientation detection with `tesseract --psm 0` on a downscaled copy. Returns (rotate_cw_deg, conf, secs)."""
    import cv2
    t = time.perf_counter()
    small = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else arr
    r = subprocess.run(["tesseract", "stdin", "stdout", "--dpi", str(int(300 * scale)), "--tessdata-dir", TESSDATA[td],
                        "--psm", "0"], input=pgm_bytes(small), capture_output=True, env=env, timeout=OCR_SUBPROCESS_TIMEOUT_S)
    out = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
    rot, conf = 0, 0.0
    for line in out.splitlines():
        if line.startswith("Rotate:"):
            rot = int(line.split(":")[1])
        elif line.startswith("Orientation confidence:"):
            conf = float(line.split(":")[1])
    return rot, conf, time.perf_counter() - t


# ------------------------------------------------------------------ engines
TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def parse_tsv(tsv, px_to_pt):
    if not tsv.startswith("level"):  # TessBaseAPI.GetTSVText has no header row (the CLI renderer adds one)
        tsv = TSV_HEADER + tsv
    words = []
    for r in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if r.get("level") != "5" or not (r.get("text") or "").strip():
            continue
        x, y, w, h = (float(r[k]) for k in ("left", "top", "width", "height"))
        words.append(dict(text=r["text"], bbox=[round(x * px_to_pt, 2), round(y * px_to_pt, 2), round((x + w) * px_to_pt, 2),
                                                 round((y + h) * px_to_pt, 2)],
                          conf=float(r["conf"]), block=int(r["block_num"]), par=int(r["par_num"]), line=int(r["line_num"]),
                          word=int(r["word_num"])))
    return words


def tess_cli(arr, spec, dpi, env):
    params = dict(DEFAULT_PARAMS, **spec.get("params", {}))
    cmd = ["tesseract", "stdin", "stdout", "--dpi", str(dpi), "--tessdata-dir", TESSDATA[spec["td"]], "-l", spec["lang"],
           "--psm", str(spec["psm"])]
    for k, v in params.items():
        cmd += ["-c", f"{k}={v}"]
    r = subprocess.run(cmd + ["tsv"], input=pgm_bytes(arr), capture_output=True, env=env, timeout=OCR_SUBPROCESS_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:500])
    return r.stdout.decode("utf-8", errors="replace")


_API = {}


def _api(td, lang, psm, params=None):
    import tesserocr
    key = (td, lang, psm, tuple(sorted((params or {}).items())))
    api = _API.get(key)
    if api is None:
        api = tesserocr.PyTessBaseAPI(path=TESSDATA[td], lang=lang, psm=psm)
        for k, v in dict(DEFAULT_PARAMS, **(params or {})).items():
            api.SetVariable(k, v)
        _API[key] = api
    return api


def tess_hybrid(arr, spec, dpi, info):
    """Fast model on the full page; lines containing a word with conf < thr are re-recognised by the best model
    (psm 7, SetRectangle on the same image, both models resident in-process). Returns TSV-like word list in px."""
    tsv = tess_api(arr, dict(spec, td="fast"), dpi)
    words = parse_tsv(tsv, 1.0)
    lines = {}
    for w in words:
        lines.setdefault((w["block"], w["par"], w["line"]), []).append(w)
    flagged = [k for k, ws in lines.items() if min(w["conf"] for w in ws) < spec["thr"]]
    info["lines"], info["relines"] = len(lines), len(flagged)
    if not flagged:
        return words
    h, w_ = arr.shape
    best = _api("best", spec["lang"], 7, spec.get("params"))
    best.SetImageBytes(np.ascontiguousarray(arr).tobytes(), w_, h, 1, w_)
    best.SetSourceResolution(dpi)
    out = []
    for k, ws in lines.items():
        if k not in flagged:
            out.extend(ws)
            continue
        x0 = max(0, int(min(w["bbox"][0] for w in ws)) - 6)
        y0 = max(0, int(min(w["bbox"][1] for w in ws)) - 4)
        x1 = min(w_, int(max(w["bbox"][2] for w in ws)) + 6)
        y1 = min(h, int(max(w["bbox"][3] for w in ws)) + 4)
        best.SetRectangle(x0, y0, x1 - x0, y1 - y0)
        best.Recognize()
        nw = parse_tsv(best.GetTSVText(0), 1.0)
        if not nw:
            out.extend(ws)
            continue
        for i, w in enumerate(nw):
            w.update(block=k[0], par=k[1], line=k[2], word=i + 1)
        out.extend(nw)
    return out


def tess_api(arr, spec, dpi):
    api = _api(spec["td"], spec["lang"], spec["psm"], spec.get("params"))
    h, w = arr.shape
    api.SetImageBytes(np.ascontiguousarray(arr).tobytes(), w, h, 1, w)
    api.SetSourceResolution(dpi)
    api.Recognize()
    return api.GetTSVText(0)


_RAPID = {}


def rapid(page, dpi):
    from rapidocr_onnxruntime import RapidOCR
    eng = _RAPID.get("e")
    if eng is None:
        eng = _RAPID["e"] = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
    res, _ = eng(img)
    s = 72.0 / dpi
    words = []
    for li, (box, txt, conf) in enumerate(res or []):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        toks = txt.split()
        n = max(1, len(txt))
        pos = 0
        for wi, t in enumerate(toks):  # split line box into word boxes proportionally to characters
            st = txt.index(t, pos)
            pos = st + len(t)
            words.append(dict(text=t, bbox=[round((x0 + (x1 - x0) * st / n) * s, 2), round(y0 * s, 2),
                                            round((x0 + (x1 - x0) * pos / n) * s, 2), round(y1 * s, 2)],
                              conf=round(float(conf) * 100, 1), block=1, par=1, line=li + 1, word=wi + 1))
    return words


def run_page(name, pdf_path, page_index, env=None):
    spec = CONFIGS[name]
    env = env or dict(os.environ)
    t0 = time.perf_counter()
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    out = dict(config=name, spec={k: v for k, v in spec.items() if k != "note"}, pdf=str(pdf_path), page_index=page_index,
               page_size=[round(page.rect.width, 2), round(page.rect.height, 2)], frame_to_page=[1, 0, 0, 1, 0, 0])
    tm = {}
    eng = spec["engine"]
    if eng == "textlayer":
        words = [dict(text=w[4], bbox=[round(v, 2) for v in w[:4]], conf=100.0, block=w[5], par=0, line=w[6], word=w[7])
                 for w in page.get_text("words")]
    elif eng == "mupdf":
        kw = dict(language=spec["lang"], tessdata=TESSDATA[spec["td"]])
        if spec["dpi"]:
            kw.update(dpi=spec["dpi"], full=spec["full"])
        t = time.perf_counter()
        tp = page.get_textpage_ocr(**kw)
        words = [dict(text=w[4], bbox=[round(v, 2) for v in w[:4]], conf=None, block=w[5], par=0, line=w[6], word=w[7])
                 for w in page.get_text("words", textpage=tp)]
        tm["ocr"] = time.perf_counter() - t
        out["render_dpi"] = spec["dpi"] or 72
    elif eng == "rapidocr":
        t = time.perf_counter()
        words = rapid(page, spec["dpi"])
        tm["ocr"] = time.perf_counter() - t
        out["render_dpi"] = spec["dpi"]
    else:
        dpi = (native_dpi(page) if spec["dpi"] == "native" else min(300, native_dpi(page)) if spec["dpi"] == "auto"
               else spec["dpi"])
        t = time.perf_counter()
        arr = render_gray(page, dpi)
        tm["render"] = time.perf_counter() - t
        page_to_px = M(dpi / 72, 0, 0, dpi / 72, 0, 0)
        frame = M(1, 0, 0, 1, 0, 0)  # px(page) -> px(frame)
        if spec.get("osd"):
            rot, conf, secs = tess_osd(arr, spec["td"], env=env)
            tm["osd"] = secs
            out["osd"] = dict(rotate_cw=rot, conf=conf)
            if rot and conf >= 2.0:
                arr, m = rot90k(arr, -(rot // 90))  # tesseract 'Rotate: N' = rotate N deg clockwise to make upright
                frame = frame * m
        if spec.get("autorot"):
            t = time.perf_counter()
            ratio = sideways_ratio(arr)
            out["sideways_ratio"] = round(ratio, 3)
            tm["autorot"] = time.perf_counter() - t
            if ratio < 1.0:
                rot, conf, secs = tess_osd(arr, spec["td"], env=env)
                tm["osd"] = secs
                out["osd"] = dict(rotate_cw=rot, conf=conf)
                if rot not in (90, 270):
                    rot = 90
                arr, m = rot90k(arr, -(rot // 90))
                frame = frame * m
        if spec.get("deskew"):
            t = time.perf_counter()
            ang = estimate_skew(arr)
            out["deskew_angle"] = round(ang, 3)
            if abs(ang) >= 0.15:
                arr, m = rotate_arr(arr, ang)
                frame = frame * m
            tm["deskew"] = time.perf_counter() - t
        rule_mask = None
        if spec.get("filter_rules"):
            t = time.perf_counter()
            rule_ink, ink_all, rules = erase_lines(arr, dpi, return_mask=True)
            rule_mask = (rule_ink, ink_all)
            s_ = 72.0 / dpi
            out["rules"] = [dict(kind=r["kind"], bbox=[round(v * s_, 2) for v in r["bbox"]]) for r in rules]
            tm["rules"] = time.perf_counter() - t
        if spec.get("erase_lines"):
            t = time.perf_counter()
            arr, rules = erase_lines(arr, dpi)
            s_ = 72.0 / dpi
            out["rules"] = [dict(kind=r["kind"], bbox=[round(v * s_, 2) for v in r["bbox"]]) for r in rules]
            tm["erase_lines"] = time.perf_counter() - t
        t = time.perf_counter()
        if eng == "tesserocr_hybrid":
            info = {}
            words = tess_hybrid(arr, spec, dpi, info)
            out["hybrid"] = info
            for w in words:
                w["bbox"] = [round(v * 72.0 / dpi, 2) for v in w["bbox"]]
        else:
            tsv = tess_cli(arr, spec, dpi, env) if eng == "tess" else tess_api(arr, spec, dpi)
            words = parse_tsv(tsv, 1.0 if rule_mask is not None else 72.0 / dpi)
        tm["ocr"] = time.perf_counter() - t
        if rule_mask is not None and eng != "tesserocr_hybrid":
            t = time.perf_counter()
            words, nd = filter_rule_words(words, *rule_mask)
            out["rule_filtered_words"] = nd
            for w in words:
                w["bbox"] = [round(v * 72.0 / dpi, 2) for v in w["bbox"]]
            tm["rules"] = tm.get("rules", 0) + time.perf_counter() - t
        # frame points -> page points: page_pts -> px -> frame px -> frame pts
        f2p = ~(page_to_px * frame * M(72 / dpi, 0, 0, 72 / dpi, 0, 0))
        out["frame_to_page"] = mat_list(f2p)
        out["render_dpi"] = dpi
    tm["total"] = time.perf_counter() - t0
    out["timings"] = {k: round(v, 4) for k, v in tm.items()}
    out["words"] = words
    out["text"] = native_text(words)
    return out


def native_text(words):
    """Engine reading order: words in output order, newline on line change, blank line on block change."""
    lines, cur, key = [], [], None
    for w in words:
        k = (w["block"], w["par"], w["line"])
        if key is not None and k != key:
            lines.append((key[0], " ".join(cur)))
            cur = []
        cur.append(w["text"])
        key = k
    if cur:
        lines.append((key[0], " ".join(cur)))
    out, prev = [], None
    for b, l in lines:
        if prev is not None and b != prev:
            out.append("")
        out.append(l)
        prev = b
    return "\n".join(out)
