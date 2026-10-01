"""Timing pass (run on an otherwise idle machine; waits for idle and records system CPU load per measurement).

single : per-page wall seconds pinned to ONE core (taskset), OMP_THREAD_LIMIT=1, median of 3 runs per page
         (after 1 warm-up), then median across the timing subset. Includes render + preprocessing + OCR + parse.
through: pages/minute on 4 cores for a batch: (a) sequential with Tesseract internal OpenMP threads,
         (b) 4-process page pool single-threaded, (c) 2 procs x 2 threads.
micro  : stage costs (render, PNG vs PGM encode, process spawn + model load, tesserocr init).
usage: timing.py single [--configs ...] | through [--configs ...] | micro
"""
import argparse, json, os, statistics, subprocess, sys, time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).resolve().parent.parent
SUBSET = [("office300", "jahresrechnung", 0), ("office300", "jahresrechnung", 1), ("office300", "bankauszug", 0),
          ("office300", "lohnausweis", 0), ("office300", "aktionaerbindungsvertrag", 0), ("office300", "protokoll_gv", 0)]
LOWRES = [("fax150", "jahresrechnung", 0), ("fax150", "bankauszug", 0), ("gray200", "jahresrechnung", 0),
          ("gray200", "bankauszug", 0)]


def cpu_sample():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v), idle


def busy_between(a, b):
    tot, idle = b[0] - a[0], b[1] - a[1]
    return 1 - idle / max(1, tot)


def wait_idle(max_busy=0.30, window=3.0, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        a = cpu_sample()
        time.sleep(window)
        b = cpu_sample()
        if busy_between(a, b) <= max_busy:
            return True
        print(f"  machine busy ({busy_between(a, b):.0%}), waiting ...", flush=True)
    return False


def entries(sel):
    man = json.loads((ROOT / "corpus" / "manifest.json").read_text())
    idx = {(p["deg"], p["doc"], p["src_page"]): p for p in man["pages"]}
    return [idx[k] for k in sel]


def _one(args):
    cfg, e = args
    import engines
    t = time.perf_counter()
    engines.run_page(cfg, ROOT / e["pdf"], e["page_index"], env=dict(os.environ))
    return time.perf_counter() - t


def single(configs, sel, tag):
    os.sched_setaffinity(0, {3})
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    import engines
    res = {}
    out_path = ROOT / "results" / f"timing_single_{tag}.json"
    if out_path.exists():
        res = json.loads(out_path.read_text())
    for cfg in configs:
        if cfg in res:
            continue
        wait_idle()
        a = cpu_sample()
        per_page = {}
        es = entries(sel)
        engines.run_page(cfg, ROOT / es[0]["pdf"], es[0]["page_index"])  # warm-up (file cache, in-process model load)
        for e in es:
            runs = []
            for _ in range(3):
                t = time.perf_counter()
                engines.run_page(cfg, ROOT / e["pdf"], e["page_index"])
                runs.append(time.perf_counter() - t)
            per_page[f"{e['deg']}/{e['doc']}_p{e['src_page']}"] = dict(runs=[round(r, 3) for r in runs], median=round(statistics.median(runs), 3))
        busy = busy_between(a, cpu_sample())
        meds = [v["median"] for v in per_page.values()]
        res[cfg] = dict(s_per_page_median=round(statistics.median(meds), 3), s_per_page_mean=round(statistics.mean(meds), 3),
                        system_busy=round(busy, 3), pages=per_page)
        print(f"{cfg:22s} median {res[cfg]['s_per_page_median']:.2f}s mean {res[cfg]['s_per_page_mean']:.2f}s busy {busy:.0%}", flush=True)
        out_path.write_text(json.dumps(res, indent=1))


def _init(threads):
    os.environ["OMP_THREAD_LIMIT"] = str(threads)
    os.environ["OMP_NUM_THREADS"] = str(threads)


def through(configs, batch_reps=2):
    man = json.loads((ROOT / "corpus" / "manifest.json").read_text())
    batch = [p for p in man["pages"] if p["deg"] == "office300"] * batch_reps
    res = {}
    out_path = ROOT / "results" / "timing_throughput.json"
    if out_path.exists():
        res = json.loads(out_path.read_text())
    for cfg in configs:
        for mode, procs, threads in [("seq_omp4", 1, 4), ("pool4_omp1", 4, 1), ("pool2_omp2", 2, 2)]:
            key = f"{cfg}|{mode}"
            if key in res:
                continue
            wait_idle()
            a = cpu_sample()
            t = time.perf_counter()
            with Pool(procs, initializer=_init, initargs=(threads,)) as pool:
                pool.map(_one, [(cfg, e) for e in batch], chunksize=1)
            dt = time.perf_counter() - t
            busy = busy_between(a, cpu_sample())
            res[key] = dict(pages=len(batch), seconds=round(dt, 2), pages_per_min=round(len(batch) / dt * 60, 1),
                            cpu_busy=round(busy, 3))
            print(f"{key:36s} {len(batch)} pages {dt:6.1f}s -> {res[key]['pages_per_min']:.1f} pages/min (cpu {busy:.0%})", flush=True)
            out_path.write_text(json.dumps(res, indent=1))


def micro():
    os.sched_setaffinity(0, {3})
    os.environ["OMP_THREAD_LIMIT"] = "1"
    import numpy as np, pymupdf, engines
    wait_idle()
    e = entries([("office300", "jahresrechnung", 0)])[0]
    page = pymupdf.open(ROOT / e["pdf"])[e["page_index"]]
    r = {}

    def med(fn, n=5):
        ts = []
        for _ in range(n):
            t = time.perf_counter()
            fn()
            ts.append(time.perf_counter() - t)
        return round(statistics.median(ts), 4)

    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
    r["render_300dpi_gray_s"] = med(lambda: page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY))
    r["render_400dpi_gray_s"] = med(lambda: page.get_pixmap(dpi=400, colorspace=pymupdf.csGRAY))
    r["encode_png_pymupdf_s"] = med(lambda: pix.tobytes("png"), 3)
    arr = engines.render_gray(page, 300)
    r["encode_pgm_s"] = med(lambda: engines.pgm_bytes(arr))
    r["deskew_estimate_s"] = med(lambda: engines.estimate_skew(arr))
    r["sideways_check_s"] = med(lambda: engines.sideways_ratio(arr))
    from PIL import Image
    import io
    b = io.BytesIO()
    Image.new("L", (64, 32), 255).save(b, "PNG")
    for td in ("fast", "best"):
        for lang in ("deu+eng", "deu+fra+eng"):
            r[f"spawn_and_model_load_{td}_{lang}_s"] = med(lambda: subprocess.run(
                ["tesseract", "stdin", "stdout", "--tessdata-dir", engines.TESSDATA[td], "-l", lang, "--psm", "4", "tsv"],
                input=b.getvalue(), capture_output=True))
    import tesserocr
    for td in ("fast", "best"):
        r[f"tesserocr_init_{td}_s"] = med(lambda: tesserocr.PyTessBaseAPI(path=engines.TESSDATA[td], lang="deu+eng").End(), 3)
    # digital-page per-page OCR decision (text length + image coverage) on a born-digital page
    d = pymupdf.open(ROOT / "corpus" / "digital" / "jahresrechnung.pdf")[0]

    def decide(p=d):
        if len(p.get_text().strip()) >= 50:
            return False
        area = abs(p.rect)
        img = sum(abs(pymupdf.Rect(i["bbox"]) & p.rect) for i in p.get_image_info())
        return img > 0.3 * area
    r["per_page_ocr_decision_digital_s"] = med(decide)
    r["per_page_ocr_decision_scan_s"] = med(lambda: decide(page))
    (ROOT / "results" / "timing_micro.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--configs", default="")
    ap.add_argument("--lowres", action="store_true")
    a = ap.parse_args()
    import engines
    cfgs = [c for c in a.configs.split(",") if c] or [c for c in engines.CONFIGS if c != "pymupdf_textlayer"]
    if a.mode == "single":
        single(cfgs, LOWRES if a.lowres else SUBSET, "lowres" if a.lowres else "office300")
    elif a.mode == "through":
        through(cfgs)
    elif a.mode == "micro":
        micro()
