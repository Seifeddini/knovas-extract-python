"""Score OCR outputs against ground truth.

Alignment: every hypothesis word box is mapped OCR-frame -> scanned page (frame_to_page) -> original page
(inverse of the corpus page_to_scan) and assigned to the GT item (one visual line / table-cell line) whose box
contains its centre (2 pt tolerance, else nearest within 6 pt). Words inside ignore regions (stamps, signatures,
QR code) that hit no item are dropped; all other unassigned words are appended as insertions.
  cer / wer      : Levenshtein on the item-aligned serialisation (reading-order independent recognition accuracy)
  cer_native     : Levenshtein between GT reading-order text and the engine's own output order
  numeric_exact  : multiset share of GT number tokens (1'234.50, 31.12.2023, 8.1, -84'210.40 ...) found exactly
                   anywhere on the page (apostrophe/dash variants folded)
  numeric_item   : same, but the number must be on the right GT item (row/cell) - i.e. usable in a table row
Normalisation: NFC + whitespace collapse; case and punctuation kept. Debris tokens (|, ----, ....., ___) are
removed from hypotheses for all engines (table rules / dot leaders, not content).
"""
import json, re, sys, unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
import pymupdf
from rapidfuzz.distance import Levenshtein

ROOT = Path(__file__).resolve().parent.parent
NUM_RE = re.compile(r"[-−–]?\d+(?:['’ʼ´`.,/]\d+)*%?")
DEBRIS = re.compile(r"[|¦]+|[._·…,:;'\"`´~=\-–—_|]{3,}")
LEADER = re.compile(r"^[.…·_]{3,}|[.…·_]{3,}$")
FOLD = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'", "′": "'", "−": "-", "–": "-", "—": "-"})


def norm(s):
    return " ".join(unicodedata.normalize("NFC", s).split())


def numbers(s):
    return [m.group(0) for m in NUM_RE.finditer(s.translate(FOLD))]


@lru_cache(maxsize=None)
def load_gt(rel):
    return json.loads((ROOT / rel).read_text())


@lru_cache(maxsize=None)
def manifest_index():
    man = json.loads((ROOT / "corpus" / "manifest.json").read_text())
    return {(p["deg"], p["doc"], p["src_page"]): p for p in man["pages"]}


def _inside(c, b, pad=0.0):
    return b[0] - pad <= c[0] <= b[2] + pad and b[1] - pad <= c[1] <= b[3] + pad


def _dist(c, b):
    dx = max(b[0] - c[0], 0, c[0] - b[2])
    dy = max(b[1] - c[1], 0, c[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


def postprocess(words, min_conf=30.0):
    """'+pp' variant: strip dot/underscore leader runs glued to tokens and drop words with conf < min_conf
    (engines without confidences only get the leader strip)."""
    res = []
    for w in words:
        t = LEADER.sub("", w["text"].strip())
        if not t:
            continue
        if w.get("conf") is not None and 0 <= w["conf"] < min_conf:
            continue
        res.append(dict(w, text=t))
    return res


def align(gt, entry, out):
    f2p = pymupdf.Matrix(*out["frame_to_page"])
    s2o = ~pymupdf.Matrix(*entry["page_to_scan"])
    T = f2p * s2o
    items = gt["items"]
    ignore = [r["bbox"] for r in gt["ignore_regions"]] + [r["bbox"] for r in entry.get("extra_ignore", [])]
    assigned = {it["id"]: [] for it in items}
    extra, dropped = [], 0
    for w in out["words"]:
        t = unicodedata.normalize("NFC", w["text"]).strip()
        if not t or DEBRIS.fullmatch(t):
            continue
        r = pymupdf.Rect(w["bbox"]).quad.transform(T).rect
        c = ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        cands = [it for it in items if _inside(c, it["bbox"], 2.0)]
        if cands:
            it = min(cands, key=lambda i: ((i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1])))
            assigned[it["id"]].append((c[0], t))
            continue
        if any(_inside(c, b) for b in ignore):
            dropped += 1
            continue
        near = min(items, key=lambda i: _dist(c, i["bbox"]))
        if _dist(c, near["bbox"]) <= 6.0:
            assigned[near["id"]].append((c[0], t))
        else:
            extra.append((c[1], c[0], t))
    hyp_items = {k: " ".join(t for _, t in sorted(v)) for k, v in assigned.items()}
    extra_txt = " ".join(t for _, _, t in sorted(extra))
    return hyp_items, extra_txt, len(extra), dropped


def score(out, entry=None, pp=False):
    if entry is None:
        entry = manifest_index()[(out["deg"], out["doc"], out["src_page"])]
    if pp:
        out = dict(out, words=postprocess(out["words"]))
    gt = load_gt(entry["gt"])
    hyp_items, extra_txt, n_extra, n_dropped = align(gt, entry, out)
    gt_s = norm(" ".join(it["text"] for it in gt["items"]))
    hyp_s = norm(" ".join([hyp_items[it["id"]] for it in gt["items"]] + [extra_txt]))
    gt_w, hyp_w = gt_s.split(), hyp_s.split()
    native = norm(" ".join(t for t in (
        unicodedata.normalize("NFC", w["text"]).strip() for w in out["words"]) if t and not DEBRIS.fullmatch(t)))
    gnum = Counter(n for it in gt["items"] for n in numbers(it["text"]))
    hnum = Counter(numbers(hyp_s))
    num_hit = sum((gnum & hnum).values())
    item_hit = 0
    for it in gt["items"]:
        g = Counter(numbers(it["text"]))
        if g:
            item_hit += sum((g & Counter(numbers(hyp_items[it["id"]]))).values())
    # per role class (tables vs prose) for diagnosis
    by_role = {}
    for it in gt["items"]:
        cls = "table" if it["role"] in ("table_cell",) else ("form" if it["role"].startswith("kv") else "text")
        a = by_role.setdefault(cls, [0, 0])
        a[0] += Levenshtein.distance(norm(it["text"]), norm(hyp_items[it["id"]]))
        a[1] += len(norm(it["text"]))
    hyp_items_s = norm(" ".join(hyp_items[it["id"]] for it in gt["items"]))
    return dict(
        gt_chars=len(gt_s), char_edits=Levenshtein.distance(gt_s, hyp_s),
        item_char_edits=Levenshtein.distance(gt_s, hyp_items_s),
        gt_words=len(gt_w), word_edits=Levenshtein.distance(gt_w, hyp_w),
        native_edits=Levenshtein.distance(norm(" ".join(it["text"] for it in gt["items"])), native),
        gt_numbers=sum(gnum.values()), num_hit=num_hit, num_item_hit=item_hit,
        n_hyp_words=len(out["words"]), n_extra=n_extra, n_dropped=n_dropped,
        role_edits={k: v[0] for k, v in by_role.items()}, role_chars={k: v[1] for k, v in by_role.items()},
    )


if __name__ == "__main__":
    o = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(score(o), indent=1))
