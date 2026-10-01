"""Self-tests for the benchmark harness (run: python code/selftest.py). Exits non-zero on failure.

1. GT consistency: every GT page's words join to its items; text layer of the digital PDF scores CER 0.
2. Transform correctness: GT word boxes mapped through page_to_scan land on dark pixels of every degraded scan.
3. Evaluator behaviour under controlled perturbations of a perfect hypothesis:
   - word order shuffled            -> aligned CER stays 0, native-order CER > 0
   - one amount altered             -> numeric_exact drops by exactly 1/N, CER > 0
   - words moved into a stamp area  -> ignored (no insertion)
   - garbage word in empty margin   -> counted as insertion (n_extra = 1)
   - dot-leader debris '.......'    -> ignored
"""
import json, random, sys
from pathlib import Path
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import engines, evaluate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FAIL = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def perfect_output(entry):
    """Hypothesis = GT words mapped into the scanned page frame (what a perfect OCR would return)."""
    g = evaluate.load_gt(entry["gt"])
    M = pymupdf.Matrix(*entry["page_to_scan"])
    words = []
    for i, w in enumerate(g["words"]):
        r = pymupdf.Rect(w["bbox"]).quad.transform(M).rect
        words.append(dict(text=w["text"], bbox=[r.x0, r.y0, r.x1, r.y1], conf=95.0, block=1, par=1, line=i, word=1))
    return dict(words=words, frame_to_page=[1, 0, 0, 1, 0, 0], deg=entry["deg"], doc=entry["doc"], src_page=entry["src_page"])


def main():
    man = json.loads((ROOT / "corpus" / "manifest.json").read_text())
    # 1. digital text layer
    for e in [p for p in man["pages"] if p["deg"] == "digital"]:
        o = engines.run_page("pymupdf_textlayer", ROOT / e["pdf"], e["page_index"])
        o.update(deg="digital", doc=e["doc"], src_page=e["src_page"])
        s = evaluate.score(o, e)
        check(s["char_edits"] == 0 and s["num_hit"] == s["gt_numbers"], f"digital text layer exact: {e['doc']} p{e['src_page']}")
    # 2. transforms: ink share under the mapped GT word boxes must be >= 3x the page average and higher than
    #    under the same boxes shifted by (4, 6) pt (i.e. a slightly wrong transform would be detected)
    for e in [p for p in man["pages"] if p["deg"] not in ("digital", "mixed")]:
        g = evaluate.load_gt(e["gt"])
        page = pymupdf.open(ROOT / e["pdf"])[e["page_index"]]
        arr = engines.render_gray(page, 100)
        ink = arr < 160
        M = pymupdf.Matrix(*e["page_to_scan"]) * pymupdf.Matrix(100 / 72, 0, 0, 100 / 72, 0, 0)
        clip = pymupdf.Rect(0, 0, arr.shape[1], arr.shape[0])

        def share(dx, dy):
            tot = hit = 0
            for w in g["words"]:
                r = (pymupdf.Rect(w["bbox"]) + (dx, dy, dx, dy)).quad.transform(M).rect & clip
                if r.is_empty:
                    continue
                sub = ink[int(r.y0):int(r.y1) + 1, int(r.x0):int(r.x1) + 1]
                tot += sub.size
                hit += int(sub.sum())
            return hit / max(1, tot)
        good, avg = share(0, 0), ink.mean()
        check(good >= 1.9 * avg, f"transform puts GT words on ink: {e['deg']}/{e['doc']} p{e['src_page']} "
                                f"(ink share {good:.3f} vs page {avg:.3f})")
    # 2b. precision, with independent evidence: real OCR words (tfast_psm4_osd, which also uprights rot90 pages)
    #     mapped back through frame_to_page and the inverse page_to_scan must land inside GT item boxes (2 pt pad)
    for e in [p for p in man["pages"] if p["deg"] not in ("digital", "mixed")
              and p["doc"] in ("jahresrechnung", "bankauszug", "mwst_abrechnung", "protokoll_gv")]:
        f = ROOT / "ocr_out" / "tfast_psm4_osd" / e["deg"] / f"{e['doc']}_p{e['src_page']}.json"
        if not f.exists():
            continue
        o = json.loads(f.read_text())
        g = evaluate.load_gt(e["gt"])
        T = pymupdf.Matrix(*o["frame_to_page"]) * ~pymupdf.Matrix(*e["page_to_scan"])
        n = hit = 0
        for w in o["words"]:
            t = w["text"].strip()
            if w["conf"] < 50 or sum(ch.isalnum() for ch in t) < 2:  # speckles read as '.', '-', '7'
                continue
            r = pymupdf.Rect(w["bbox"]).quad.transform(T).rect
            c = ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
            n += 1
            hit += any(evaluate._inside(c, it["bbox"], 2.0) for it in g["items"])
        check(hit / max(1, n) >= 0.97, f"OCR words land in GT items: {e['deg']}/{e['doc']} p{e['src_page']} ({hit}/{n})")
    # 3. evaluator perturbations
    idx = evaluate.manifest_index()
    e = idx[("skew15", "jahresrechnung", 0)]
    base = perfect_output(e)
    s = evaluate.score(base, e)
    check(s["char_edits"] == 0 and s["num_hit"] == s["gt_numbers"], "perfect hypothesis on skewed scan scores 0 CER")
    sh = dict(base, words=random.Random(1).sample(base["words"], len(base["words"])))
    s2 = evaluate.score(sh, e)
    check(s2["char_edits"] == 0 and s2["native_edits"] > 0, "shuffled order: aligned CER 0, native CER > 0")
    alt = json.loads(json.dumps(base))
    k = next(i for i, w in enumerate(alt["words"]) if w["text"] == "1'234'567.80")
    alt["words"][k]["text"] = "1'234'567.30"
    s3 = evaluate.score(alt, e)
    check(s3["num_hit"] == s3["gt_numbers"] - 1 and s3["char_edits"] == 1, "one altered amount: numeric -1, CER = 1 edit")
    junk = json.loads(json.dumps(base))
    junk["words"].append(dict(text="Xyzzy", bbox=[5, 5, 20, 12], conf=90, block=9, par=1, line=1, word=1))
    junk["words"].append(dict(text="..........", bbox=[300, 300, 340, 305], conf=90, block=9, par=1, line=2, word=1))
    s4 = evaluate.score(junk, e)
    check(s4["n_extra"] == 1 and s4["char_edits"] == len(" Xyzzy"), "margin garbage = 1 insertion, leader debris ignored")
    e2 = idx[("stamp", "mwst_abrechnung", 0)]
    b2 = perfect_output(e2)
    sb = e2["extra_ignore"][0]["bbox"]
    # place a stamp word in the part of the stamp box that is not covered by any GT item
    g2 = evaluate.load_gt(e2["gt"])
    M2 = pymupdf.Matrix(*e2["page_to_scan"])
    spot = None
    for yy in np.linspace(sb[1] + 2, sb[3] - 2, 30):
        for xx in np.linspace(sb[0] + 2, sb[2] - 2, 30):
            if not any(evaluate._inside((xx, yy), it["bbox"], 6.5) for it in g2["items"]):
                spot = (xx, yy)
                break
        if spot:
            break
    if spot:
        p = pymupdf.Point(*spot) * M2
        b2["words"].append(dict(text="EINGEGANGEN", bbox=[p.x - 20, p.y - 4, p.x + 20, p.y + 4], conf=90, block=9, par=1, line=1, word=1))
        s5 = evaluate.score(b2, e2)
        check(s5["n_extra"] == 0 and s5["n_dropped"] == 1 and s5["char_edits"] == 0, "stamp text in ignore region is dropped")
    print(f"\n{len(FAIL)} failures")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
