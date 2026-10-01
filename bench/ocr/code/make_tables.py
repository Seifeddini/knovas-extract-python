"""Render the README result tables from results/summary.json + results/timing_*.json -> results/README_tables.md
and results/headline.json (machine-readable rows used for the final report)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "results"

ORDER = [
    ("mupdf_statusquo", "today: knovas-extract get_textpage_ocr(language) -> 72 dpi"),
    ("mupdf_300_full", "MuPDF built-in OCR, dpi=300 full=True"),
    ("rapidocr_bundled", "RapidOCR 1.4.4, bundled PP-OCRv4 ch/en models"),
    ("tfast_psm6", "Tesseract 5.3.4 CLI fast, psm 6"),
    ("tfast_psm4", "fast, psm 4 (prototype rows.py setting)"),
    ("tfast_psm11", "fast, psm 11 sparse text"),
    ("tfast_psm3", "fast, psm 3 (auto layout)"),
    ("tfast_psm4_400", "fast psm 4 @400 dpi"),
    ("tfast_psm4_native", "fast psm 4 @native image dpi"),
    ("tfast_psm3_native", "fast psm 3 @native dpi (<=300)"),
    ("tfast_psm4_sauvola", "fast psm 4 + Sauvola thresholding"),
    ("tfast_psm4_noinvert", "fast psm 4 + tessedit_do_invert=0"),
    ("tfast_psm4_deskew", "fast psm 4 + projection deskew"),
    ("tfast_psm4_dfe", "fast psm 4, deu+fra+eng"),
    ("tfast_psm4_osd", "fast psm 4 + OSD (psm 0) on every page"),
    ("tfast_psm4_autorot", "fast psm 4 + 14 ms sideways check, OSD only if sideways"),
    ("tfast_psm3_nolines", "fast psm 3 + erase rules/dot leaders"),
    ("tfast_pro3", "fast psm3, auto dpi, autorot, erase rules"),
    ("tfast_pro3_deskew", "tfast_pro3 + deskew"),
    ("tfast_pro3_dfe", "tfast_pro3, deu+fra+eng"),
    ("tfast_pro3f", "fast psm3, auto dpi, autorot, deskew, rule-ink word filter (no erase)"),
    ("tapi_fast_psm3", "tesserocr (Tesseract 5.5.1 in-process) fast psm 3"),
    ("tapi_fast_pro3f", "tesserocr in-process version of tfast_pro3f"),
    ("tapi_fast_pro3f_dfe", "tesserocr fast, deu+fra+eng, pro3f pipeline  <- RECOMMENDED DEFAULT"),
    ("tapi_best_pro3f_dfe", "tesserocr best, deu+fra+eng, pro3f pipeline  <- RECOMMENDED HIGH-ACCURACY"),
    ("tapi_hybrid85", "tesserocr fast + best re-read of lines with conf<85"),
    ("tbest_psm6", "tessdata_best psm 6"),
    ("tbest_psm4", "best psm 4"),
    ("tbest_psm4_400", "best psm 4 @400 dpi"),
    ("tbest_psm3", "best psm 3"),
    ("tbest_psm4_dfe", "best psm 4 deu+fra+eng"),
    ("tbest_pro3", "best psm3, auto dpi, autorot, erase rules"),
    ("tbest_pro3_dfe", "tbest_pro3, deu+fra+eng"),
    ("tbest_pro3f_dfe", "best psm3 deu+fra+eng, auto dpi, autorot, deskew, rule-ink word filter"),
]
DEGS = ["clean300", "office300", "gray200", "fax150", "color300", "skew15", "stamp", "rot90"]


def load(name):
    p = R / name
    return json.loads(p.read_text()) if p.exists() else {}


def main():
    S = load("summary.json")
    T = load("timing_single_office300.json")
    TL = load("timing_single_lowres.json")
    TP = load("timing_throughput.json")
    L, rows = [], []
    L.append("### A. Accuracy over 91 scanned pages (7 degradations x 13 pages; rot90 reported separately) + speed\n")
    L.append("| config | what | CER | CER in GT items | WER | numeric_exact | numeric_item | hallucinated words/page | "
             "CER +pp | numeric +pp | s/page 1 core (office300) | rot90 CER |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c, what in ORDER:
        if c not in S or "ALL_SCANS" not in S[c]:
            continue
        a = S[c]["ALL_SCANS"]
        pp = S.get(c + "+pp", {}).get("ALL_SCANS", {})
        t = T.get(c, {})
        ts = f"{t['s_per_page_median']:.2f}" + ("*" if t.get("system_busy", 0) > 0.40 else "") if t else "-"
        r90 = S[c].get("rot90", {}).get("cer")
        L.append(f"| {c} | {what} | {a['cer']:.4f} | {a['cer_items']:.4f} | {a['wer']:.4f} | {a['numeric_exact']:.4f} | "
                 f"{a['numeric_item']:.4f} | {a['extra_words_per_page']:.1f} | {pp.get('cer', float('nan')):.4f} | "
                 f"{pp.get('numeric_exact', float('nan')):.4f} | {ts} | {'-' if r90 is None else f'{r90:.3f}'} |")
        for d in DEGS + ["ALL_SCANS"]:
            if d in S[c]:
                x = S[c][d]
                rows.append(dict(engine_config=c, degradation=d, cer=round(x["cer"], 4), wer=round(x["wer"], 4),
                                 numeric_exact=round(x["numeric_exact"], 4), cer_items=round(x["cer_items"], 4),
                                 s_per_page=t.get("s_per_page_median"), what=what))
    L.append("\n`*` = timing taken while other agents' jobs loaded the machine (system busy > 40 %).\n")
    L.append("### B. CER / numeric_exact by degradation\n")
    L.append("| config | " + " | ".join(DEGS) + " |")
    L.append("|---|" + "---|" * len(DEGS))
    for c, _ in ORDER:
        if c not in S:
            continue
        L.append(f"| {c} | " + " | ".join(
            f"{S[c][d]['cer']:.3f} / {S[c][d]['numeric_exact']:.3f}" if d in S[c] else "-" for d in DEGS) + " |")
    if TL:
        L.append("\n### C. Low-resolution inputs (fax150 + gray200 subset, 4 pages): single-core s/page and accuracy\n")
        L.append("| config | s/page 1 core | fax150 CER / numeric | gray200 CER / numeric |")
        L.append("|---|---|---|---|")
        for c, v in TL.items():
            f, g = S.get(c, {}).get("fax150", {}), S.get(c, {}).get("gray200", {})
            L.append(f"| {c} | {v['s_per_page_median']:.2f} | {f.get('cer', float('nan')):.3f} / {f.get('numeric_exact', float('nan')):.3f} | "
                     f"{g.get('cer', float('nan')):.3f} / {g.get('numeric_exact', float('nan')):.3f} |")
    if TP:
        L.append("\n### D. Throughput on 4 cores (26 office300 pages = 13 pages x 2)\n")
        L.append("| config | mode | pages/min | seconds | CPU busy |")
        L.append("|---|---|---|---|---|")
        for k, v in TP.items():
            c, m = k.split("|")
            L.append(f"| {c} | {m} | {v['pages_per_min']:.1f} | {v['seconds']:.1f} | {v['cpu_busy']:.0%} |")
        L.append("\nmodes: seq_omp4 = one page at a time, Tesseract OpenMP up to 4 threads (its default); "
                 "pool4_omp1 = 4 worker processes, OMP_THREAD_LIMIT=1; pool2_omp2 = 2 workers x 2 threads.")
    M = load("timing_micro.json")
    if M:
        L.append("\n### E. Stage costs (single core, median of 5)\n")
        L.append("| stage | seconds |")
        L.append("|---|---|")
        for k, v in M.items():
            L.append(f"| {k} | {v} |")
    (R / "README_tables.md").write_text("\n".join(L) + "\n")
    (R / "headline.json").write_text(json.dumps(rows, indent=1))
    print("\n".join(L))


if __name__ == "__main__":
    main()
