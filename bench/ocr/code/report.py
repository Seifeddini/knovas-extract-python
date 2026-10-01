"""Aggregate scores of all ocr_out/**.json -> results/page_scores.csv, results/summary.json, results/tables.md"""
import csv, json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import evaluate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCAN_DEGS = ["clean300", "office300", "gray200", "fax150", "color300", "skew15", "stamp"]
ALL_DEGS = SCAN_DEGS + ["rot90"]


def load_rows():
    rows = []
    for f in sorted((ROOT / "ocr_out").glob("*/*/*.json")):
        o = json.loads(f.read_text())
        g = evaluate.load_gt(f"gt/{o['doc']}_p{o['src_page']}.json")
        for pp in (False, True):
            if pp and o["config"] == "pymupdf_textlayer":
                continue
            s = evaluate.score(o, pp=pp)
            rows.append(dict(config=o["config"] + ("+pp" if pp else ""), deg=o["deg"], doc=o["doc"], page=o["src_page"], doc_type=g["doc_type"],
                         t_total=o["timings"].get("total"), t_ocr=o["timings"].get("ocr"), **{k: v for k, v in s.items() if not isinstance(v, dict)},
                         table_edits=s["role_edits"].get("table", 0), table_chars=s["role_chars"].get("table", 0),
                         text_edits=s["role_edits"].get("text", 0) + s["role_edits"].get("form", 0),
                         text_chars=s["role_chars"].get("text", 0) + s["role_chars"].get("form", 0)))
    return rows


def agg(rs):
    S = lambda k: sum(r[k] for r in rs)
    return dict(n=len(rs), cer=S("char_edits") / max(1, S("gt_chars")), wer=S("word_edits") / max(1, S("gt_words")),
                cer_items=S("item_char_edits") / max(1, S("gt_chars")), extra_words_per_page=S("n_extra") / max(1, len(rs)),
                s_page_parallel=sum(r["t_total"] or 0 for r in rs) / max(1, len(rs)),
                cer_native=S("native_edits") / max(1, S("gt_chars")),
                numeric_exact=S("num_hit") / max(1, S("gt_numbers")), numeric_item=S("num_item_hit") / max(1, S("gt_numbers")),
                cer_tables=S("table_edits") / max(1, S("table_chars")), cer_text=S("text_edits") / max(1, S("text_chars")),
                pages_numeric_perfect=sum(1 for r in rs if r["num_hit"] == r["gt_numbers"]) / max(1, len(rs)))


def main():
    rows = load_rows()
    with open(ROOT / "results" / "page_scores.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    by = defaultdict(list)
    for r in rows:
        by[(r["config"], r["deg"])].append(r)
    configs = sorted({r["config"] for r in rows})
    summary = {}
    for c in configs:
        summary[c] = {d: agg(by[(c, d)]) for d in ALL_DEGS + ["digital"] if by.get((c, d))}
        scan = [r for d in SCAN_DEGS for r in by.get((c, d), [])]
        if scan:
            summary[c]["ALL_SCANS"] = agg(scan)
            summary[c]["ALL_SCANS"]["complete"] = len(scan) == 91
    (ROOT / "results" / "summary.json").write_text(json.dumps(summary, indent=1))
    L = []
    L.append("## Overall over 7 scan degradations x 13 pages (rot90 excluded)\n")
    L.append("| config | pages | CER | CER in-items | WER | numeric_exact | numeric_item | CER tables | CER text/forms | CER native order | hallucinated words/page |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    order = sorted([c for c in configs if "ALL_SCANS" in summary[c]], key=lambda c: summary[c]["ALL_SCANS"]["cer"])
    for c in order:
        a = summary[c]["ALL_SCANS"]
        L.append(f"| {c} | {a['n']} | {a['cer']:.4f} | {a['cer_items']:.4f} | {a['wer']:.4f} | {a['numeric_exact']:.4f} | "
                 f"{a['numeric_item']:.4f} | {a['cer_tables']:.4f} | {a['cer_text']:.4f} | {a['cer_native']:.4f} | "
                 f"{a['extra_words_per_page']:.1f} |")
    L.append("\n## CER by degradation\n")
    L.append("| config | " + " | ".join(ALL_DEGS) + " |")
    L.append("|---|" + "---|" * len(ALL_DEGS))
    for c in order:
        L.append(f"| {c} | " + " | ".join(f"{summary[c][d]['cer']:.4f}" if d in summary[c] else "-" for d in ALL_DEGS) + " |")
    L.append("\n## numeric_exact by degradation\n")
    L.append("| config | " + " | ".join(ALL_DEGS) + " |")
    L.append("|---|" + "---|" * len(ALL_DEGS))
    for c in order:
        L.append(f"| {c} | " + " | ".join(f"{summary[c][d]['numeric_exact']:.4f}" if d in summary[c] else "-" for d in ALL_DEGS) + " |")
    L.append("\n## CER per document (office300)\n")
    docs = sorted({r["doc"] for r in rows})
    L.append("| config | " + " | ".join(docs) + " |")
    L.append("|---|" + "---|" * len(docs))
    for c in order:
        cells = []
        for d in docs:
            rs = [r for r in by.get((c, "office300"), []) if r["doc"] == d]
            cells.append(f"{agg(rs)['cer']:.4f}" if rs else "-")
        L.append(f"| {c} | " + " | ".join(cells) + " |")
    (ROOT / "results" / "tables.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
