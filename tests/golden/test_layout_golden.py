"""Golden-layout tests over saved word boxes (plan §7 "Golden-layout from saved words").

No PDF and no Tesseract are needed: ``tests/fixtures/treuhand/pages/<doc>_p<k>.<source>.json``
holds the word boxes (born-digital via PyMuPDF, or Tesseract words for 6 scan
degradations), the plain-mode page text and the rulings; ``gt/`` holds the
ground truth; ``MANIFEST.yaml`` the per-source gates (thresholds set at or just
below the measured prototype numbers — decision D8).

Update mode (rewrites ``expected/*.md`` and the ``baseline`` block of the manifest):

    KNOVAS_LAYOUT_UPDATE_GOLDEN=1 pytest tests/golden/test_layout_golden.py

(``--update-golden`` is honoured too when a conftest registers that option.)
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from knovas_extract import _layout as L
from knovas_extract._layout import check_invariants
from tests.eval import metrics as M

pytestmark = [pytest.mark.golden]

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "treuhand"
MANIFEST: dict[str, Any] = yaml.safe_load((FIX / "MANIFEST.yaml").read_text(encoding="utf-8"))
SOURCES: list[str] = list(MANIFEST["sources"])
DOCS: dict[str, int] = dict(MANIFEST["docs"])
CONTRACT: str = MANIFEST["contract_doc"]
PAGE_IDS = [(src, doc, k) for src in SOURCES for doc, n in DOCS.items() for k in range(n)]


def _update_mode(config: pytest.Config) -> bool:
    if os.environ.get("KNOVAS_LAYOUT_UPDATE_GOLDEN") == "1":
        return True
    try:
        return bool(config.getoption("--update-golden"))
    except ValueError:
        return False


def _affine(x: float, y: float, m: list[float]) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _load_gt(doc: str, k: int) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads((FIX / "gt" / f"{doc}_p{k}.json").read_text(encoding="utf-8"))
    )


def _load_words(rec: dict[str, Any], gt: dict[str, Any]) -> list[L.Word]:
    if rec["ocr"]:
        rows = [
            {"text": t, "bbox": [x0, y0, x1, y1], "conf": conf, "block": b, "par": p, "line": ln}
            for x0, y0, x1, y1, t, conf, b, p, ln in rec["words"]
        ]
        words = L.words_from_ocr_rows(rows)
    else:
        words = [
            L.Word(x0, y0, x1, y1, t, size, bool(bold), conf, b, p, ln)
            for x0, y0, x1, y1, t, size, bold, conf, b, p, ln in rec["words"]
        ]
    regions = [r["bbox"] for r in gt["ignore_regions"]] + [
        it["bbox"] for it in gt["items"] if it["role"] == "qr_bill"
    ]
    if not regions:
        return words
    keep: list[L.Word] = []
    for w in words:
        cx, cy = _affine((w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2, rec["to_gt"])
        if not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in regions):
            keep.append(w)
    return keep


@dataclass(slots=True)
class PageResult:
    source: str
    doc: str
    page: int
    text: str
    plain: str
    structured: bool
    parts: list[L.Part]
    furniture_words: list[str]
    line_words: list[str]  # one entry per visual line (input words, furniture excluded)
    ms: float
    metrics: dict[str, Any] = field(default_factory=dict)


def _render_doc(source: str, doc: str, n: int) -> list[PageResult]:
    recs = [
        json.loads((FIX / "pages" / f"{doc}_p{k}.{source}.json").read_text(encoding="utf-8"))
        for k in range(n)
    ]
    gts = [_load_gt(doc, k) for k in range(n)]
    word_lists = [_load_words(r, g) for r, g in zip(recs, gts, strict=True)]
    t0 = time.perf_counter()
    layouts = [
        L.build_page_text(
            ws,
            r["page_size"][0],
            r["page_size"][1],
            ocr=r["ocr"],
            rules=[tuple(x) for x in r["rules"]],
            page_index=k,
        )
        for k, (ws, r) in enumerate(zip(word_lists, recs, strict=True))
    ]
    doc_pass = L.DocumentLayoutPass()
    doc_pass.fit(layouts)
    rendered = [
        L.render_page_detailed(lay, plain_text=r["plain_text"])
        for lay, r in zip(layouts, recs, strict=True)
    ]
    ms = (time.perf_counter() - t0) * 1000 / n
    out: list[PageResult] = []
    for k, (lay, rp, rec) in enumerate(zip(layouts, rendered, recs, strict=True)):
        furn = [w.text for s in lay.segs if s.furniture for w in s.words]
        lines = [
            " ".join(w.text for s in line for w in s.words if not s.furniture)
            for line in lay.vlines
        ]
        out.append(
            PageResult(
                source, doc, k, rp.text, rec["plain_text"], rp.structured, rp.parts, furn, lines, ms
            )
        )
    return out


def _page_metrics(res: PageResult) -> dict[str, Any]:
    gt = _load_gt(res.doc, res.page)
    lines = M.nonblank_lines(res.text)
    prose_lines = [
        M.strip_md(ln)
        for p in res.parts
        if p.kind != "table"
        for ln in p.text.split("\n")
        if ln.strip()
    ]
    tm, fails = M.table_metrics(gt, lines)
    kv_n, kv_ok = M.kv_metrics(gt, lines)
    h_n, h_rec, out_h, h_prec = M.heading_metrics(gt, lines)
    units = M.prose_units(gt)
    u_n, intact, tau = M.order_metrics(units, "\n".join(prose_lines))
    _p, _r, f1 = M.numeric_prf(res.text, gt)
    plain_len = len(res.plain)
    lint = check_invariants(res.text, plain_len=plain_len)
    words_text = "\n".join(res.line_words)
    missing, extra = M.bag_of_words_diff(res.text, words_text)
    return {
        **tm,
        "kv_n": kv_n,
        "kv_ok": kv_ok,
        "head_n": h_n,
        "head_rec": h_rec,
        "out_heads": out_h,
        "head_prec": h_prec,
        "units": u_n,
        "intact": intact,
        "tau": tau,
        "numeric_f1": f1,
        "lint": lint,
        "bow_missing": sum(missing.values()),
        "bow_extra": sum(extra.values()),
        "bow_missing_sample": sorted(missing.items())[:6],
        "structured": res.structured,
        "chars": len(res.text),
        "plain_chars": plain_len,
        "fails": fails,
    }


@pytest.fixture(scope="module")
def results() -> dict[tuple[str, str, int], PageResult]:
    out: dict[tuple[str, str, int], PageResult] = {}
    for src in SOURCES:
        for doc, n in DOCS.items():
            for res in _render_doc(src, doc, n):
                res.metrics = _page_metrics(res)
                out[(src, doc, res.page)] = res
    return out


def _agg(rs: list[PageResult]) -> dict[str, float]:
    def s(f: str) -> float:
        return float(sum(r.metrics[f] for r in rs))

    structured = [r for r in rs if r.structured]
    return {
        "pages": len(rs),
        "row_integrity": s("rows_ok") / max(1, s("rows_n")),
        "row_cond": s("rows_cond") / max(1, s("cond_n")),
        "cell_acc": s("cells_f") / max(1, s("cells_n")),
        "header_recall": s("hdr_f") / max(1, s("hdr_n")),
        "kv_ok": s("kv_ok") / max(1, s("kv_n")),
        # D4: headings are emitted on structured pages only — measured there
        "heading_recall": sum(r.metrics["head_rec"] for r in structured)
        / max(1, sum(r.metrics["head_n"] for r in structured)),
        "heading_precision": sum(r.metrics["head_prec"] for r in structured)
        / max(1, sum(r.metrics["out_heads"] for r in structured)),
        "numeric_f1": min(r.metrics["numeric_f1"] for r in rs),
        "numeric_f1_mean": statistics.fmean(r.metrics["numeric_f1"] for r in rs),
        "intact": s("intact") / max(1, s("units")),
        "tau_mean": statistics.fmean(r.metrics["tau"] for r in rs),
        "ms_per_page": statistics.median(r.ms for r in rs),
    }


def _contract(rs: list[PageResult]) -> dict[str, float]:
    c = [r for r in rs if r.doc == CONTRACT]
    return {
        "tau": min(r.metrics["tau"] for r in c),
        "intact": sum(r.metrics["intact"] for r in c) / max(1, sum(r.metrics["units"] for r in c)),
        "bow_missing": sum(r.metrics["bow_missing"] for r in c if r.structured),
        "bow_extra": sum(r.metrics["bow_extra"] for r in c if r.structured),
    }


# ── tests ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("source", "doc", "page"), PAGE_IDS)
def test_expected_output(
    results: dict[tuple[str, str, int], PageResult],
    request: pytest.FixtureRequest,
    source: str,
    doc: str,
    page: int,
) -> None:
    """Byte-identical to the committed rendering (determinism + regression guard)."""
    res = results[(source, doc, page)]
    exp = FIX / "expected" / f"{doc}_p{page}.{source}.md"
    if _update_mode(request.config) or not exp.exists():
        exp.parent.mkdir(parents=True, exist_ok=True)
        exp.write_text(res.text, encoding="utf-8")
        if not _update_mode(request.config):
            pytest.fail(f"expected output was missing and has been written: {exp.name}; re-run")
    assert res.text == exp.read_text(encoding="utf-8")


@pytest.mark.parametrize(("source", "doc", "page"), PAGE_IDS)
def test_invariants_r10(
    results: dict[tuple[str, str, int], PageResult], source: str, doc: str, page: int
) -> None:
    res = results[(source, doc, page)]
    assert res.metrics["lint"] == [], res.metrics["lint"]
    assert "\f" not in res.text
    assert len(res.text) <= 1.5 * len(res.plain) + 4096
    if not res.structured:
        assert res.text == res.plain  # GI-EXTRACT-03 passthrough


@pytest.mark.parametrize("source", SOURCES)
def test_table_and_form_gates(results: dict[tuple[str, str, int], PageResult], source: str) -> None:
    rs = [r for r in results.values() if r.source == source]
    agg = _agg(rs)
    gates: dict[str, float] = MANIFEST["gates"][source]
    failures = []
    for key in (
        "row_integrity",
        "row_cond",
        "cell_acc",
        "header_recall",
        "kv_ok",
        "heading_recall",
        "heading_precision",
        "numeric_f1",
    ):
        if key in gates and agg[key] < gates[key]:
            failures.append(f"{key}={agg[key]:.3f} < {gates[key]}")
    print(f"\n[{source}] " + " ".join(f"{k}={v:.3f}" for k, v in agg.items()))
    assert not failures, f"{source}: " + "; ".join(failures)


@pytest.mark.parametrize("source", SOURCES)
def test_two_column_contract(results: dict[tuple[str, str, int], PageResult], source: str) -> None:
    """Law-firm gate: reading order (Kendall tau), prose units intact, bag of words equal to the
    input words minus furniture (hard where the manifest says so, advisory otherwise)."""
    rs = [r for r in results.values() if r.source == source]
    c = _contract(rs)
    gates: dict[str, Any] = MANIFEST["gates"][source]
    print(
        f"\n[{source}] contract tau={c['tau']:.3f} intact={c['intact']:.3f} bow -{c['bow_missing']} +{c['bow_extra']}"
    )
    if "contract_tau" in gates:
        assert c["tau"] >= gates["contract_tau"], f"tau {c['tau']:.3f} < {gates['contract_tau']}"
    if "contract_intact" in gates:
        assert (
            c["intact"] >= gates["contract_intact"]
        ), f"intact {c['intact']:.3f} < {gates['contract_intact']}"
    if gates.get("contract_bow") == "hard":
        assert c["bow_missing"] == 0 and c["bow_extra"] == 0, [
            r.metrics["bow_missing_sample"] for r in rs if r.doc == CONTRACT
        ]


def test_layout_time_per_page(results: dict[tuple[str, str, int], PageResult]) -> None:
    if os.environ.get("KNOVAS_LAYOUT_SKIP_TIMING") == "1" or sys.gettrace() is not None:
        pytest.skip("timing disabled or a tracer (coverage) is active")
    med = statistics.median(r.ms for r in results.values())
    print(f"\nlayout median ms/page = {med:.2f}")
    assert med <= MANIFEST["timing"]["max_ms_per_page"]


def test_write_baseline(
    results: dict[tuple[str, str, int], PageResult], request: pytest.FixtureRequest
) -> None:
    """In update mode, record the measured numbers in ``baseline.yaml`` (next to the manifest)."""
    if not _update_mode(request.config):
        pytest.skip("not in update mode")
    baseline: dict[str, Any] = {}
    for src in SOURCES:
        rs = [r for r in results.values() if r.source == src]
        agg: dict[str, Any] = {k: round(v, 4) for k, v in _agg(rs).items()}
        agg["contract"] = {k: round(float(v), 4) for k, v in _contract(rs).items()}
        baseline[src] = agg
    (FIX / "baseline.yaml").write_text(
        "# Measured by tests/golden/test_layout_golden.py in update mode — not thresholds.\n"
        + yaml.safe_dump(baseline, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
