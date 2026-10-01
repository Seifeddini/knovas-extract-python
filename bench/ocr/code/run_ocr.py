"""Run OCR configs over the corpus (accuracy pass). Page-level process pool, every worker single-threaded
(OMP_THREAD_LIMIT=1). Resumable: existing outputs are skipped.
usage: run_ocr.py [--configs a,b] [--degs x,y] [--docs d1,d2] [--jobs 4]
Writes ocr_out/<config>/<deg>/<doc>_p<k>.json (words with boxes + timings + metadata)."""
import argparse, json, os, sys, time, traceback
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).resolve().parent.parent
OCR_DEGS = ["clean300", "office300", "gray200", "fax150", "color300", "skew15", "stamp", "rot90"]


def _init():
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"


def job(args):
    cfg, entry = args
    import engines
    dst = ROOT / "ocr_out" / cfg / entry["deg"] / f"{entry['doc']}_p{entry['src_page']}.json"
    if dst.exists():
        return cfg, entry["deg"], entry["doc"], "skip", 0
    try:
        t = time.perf_counter()
        out = engines.run_page(cfg, ROOT / entry["pdf"], entry["page_index"], env=dict(os.environ))
        out.update(deg=entry["deg"], doc=entry["doc"], src_page=entry["src_page"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(out, ensure_ascii=False))
        return cfg, entry["deg"], entry["doc"], "ok", time.perf_counter() - t
    except Exception:
        return cfg, entry["deg"], entry["doc"], "ERR " + traceback.format_exc()[-400:], 0


def main():
    import engines
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default=",".join(c for c in engines.CONFIGS if c != "pymupdf_textlayer"))
    ap.add_argument("--degs", default=",".join(OCR_DEGS))
    ap.add_argument("--docs", default="")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    man = json.loads((ROOT / "corpus" / "manifest.json").read_text())
    degs, docs = a.degs.split(","), [d for d in a.docs.split(",") if d]
    jobs = []
    for cfg in a.configs.split(","):
        for e in man["pages"]:
            if e["deg"] in degs and (not docs or e["doc"] in docs):
                jobs.append((cfg, e))
    # longest first for better packing
    jobs.sort(key=lambda j: ("best" in j[0] or "rapid" in j[0], "400" in j[0]), reverse=True)
    t = time.time()
    n_ok = 0
    with Pool(a.jobs, initializer=_init) as pool:
        for cfg, deg, doc, st, secs in pool.imap_unordered(job, jobs):
            if st == "ok":
                n_ok += 1
            elif st != "skip":
                print(cfg, deg, doc, st, flush=True)
    print(f"done {n_ok} new outputs / {len(jobs)} jobs in {time.time() - t:.0f}s", flush=True)


if __name__ == "__main__":
    main()
