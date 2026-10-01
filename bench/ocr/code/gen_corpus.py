"""Generate the Swiss OCR benchmark corpus.

corpus/digital/<doc>.pdf          born-digital source (text layer = ground truth)
corpus/<deg>/<doc>.pdf            image-only degraded versions (one PDF per doc and degradation)
corpus/mixed/<doc>_mixed.pdf      digital cover letter + scanned body + scanner text stamp (for per-page OCR decision tests)
corpus/manifest.json              every (degradation, doc, page) with the affine page->scan transform
gt/<doc>_p<k>.json                per-page ground truth (items, words+boxes, tables, kvs, headings, blocks, text, markdown)
"""
import io, json, math, re, sys, unicodedata
from pathlib import Path
import numpy as np
import pymupdf
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from docs import ALL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS, GT = ROOT / "corpus", ROOT / "gt"
NUM_RE = re.compile(r"[-−–]?\d+(?:['’.,/]\d+)*%?")

DEGS = {
    "clean300": dict(dpi=300, mode="L", skew=0.0, blur=0.0, noise=0, jpeg=None, ink=0, paper=255,
                     desc="300 dpi gray PNG render, no noise (upper bound)"),
    "office300": dict(dpi=300, mode="L", skew=0.4, blur=0.6, noise=4, jpeg=75, ink=30, paper=246,
                      desc="office scanner: 300 dpi gray JPEG q75, 0.4 deg skew, blur 0.6 px, noise"),
    "gray200": dict(dpi=200, mode="L", skew=0.4, blur=0.5, noise=4, jpeg=75, ink=30, paper=246,
                    desc="200 dpi gray JPEG q75, 0.4 deg skew"),
    "fax150": dict(dpi=150, mode="1", skew=0.3, blur=0.4, noise=14, jpeg=None, ink=20, paper=250, threshold=150,
                   speckle=0.0008, desc="150 dpi bitonal fax-like (threshold + speckles)"),
    "color300": dict(dpi=300, mode="RGB", skew=-0.3, blur=0.5, noise=3, jpeg=85, ink=35, paper=250,
                     tint=(1.0, 0.975, 0.92), desc="300 dpi colour JPEG q85, cream paper tint, -0.3 deg skew"),
    "skew15": dict(dpi=300, mode="L", skew=1.5, blur=0.6, noise=4, jpeg=75, ink=30, paper=246,
                   desc="office300 but 1.5 deg skew"),
    "stamp": dict(dpi=300, mode="RGB", skew=0.4, blur=0.5, noise=3, jpeg=80, ink=30, paper=248, stamp=True,
                  desc="300 dpi colour JPEG with red rubber stamp overlay (-12 deg) over content"),
    "rot90": dict(dpi=300, mode="L", skew=0.4, blur=0.6, noise=4, jpeg=75, ink=30, paper=246, rot90=True,
                  docs={"jahresrechnung": [0], "bankauszug": [0], "mwst_abrechnung": [0], "brief": [0]},
                  desc="office300 page fed sideways: content rotated 90 deg CCW, landscape page"),
}


def nfc(s):
    return unicodedata.normalize("NFC", s)


def skew_matrix(angle_deg, w, h):
    """PIL Image.rotate(angle) (counter-clockwise, about the centre) expressed in page points, y down."""
    a = math.radians(angle_deg)
    cx, cy = w / 2, h / 2
    ca, sa = math.cos(a), math.sin(a)
    # x' = cx + (x-cx)ca + (y-cy)sa ; y' = cy - (x-cx)sa + (y-cy)ca
    return pymupdf.Matrix(ca, -sa, sa, ca, cx - cx * ca - cy * sa, cy + cx * sa - cy * ca)


def rot90_matrix(w):
    """Image.transpose(ROTATE_90) (CCW): x'' = y', y'' = W - x'."""
    return pymupdf.Matrix(0, -1, 1, 0, 0, w)


def mat_list(m):
    return [round(v, 6) for v in (m.a, m.b, m.c, m.d, m.e, m.f)]


# --------------------------------------------------------------------------- ground truth
def build_gt(builder, pdf_bytes):
    doc = pymupdf.open("pdf", pdf_bytes)
    pages = []
    for pno, (page, rec) in enumerate(zip(doc, builder.pages)):
        items = [dict(it) for it in rec["items"] if it["text"].strip()]
        for it in items:
            it["text"] = nfc(it["text"])
        words = []
        problems = []
        for w in page.get_text("words"):
            x0, y0, x1, y1, t = w[:5]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            cands = [it for it in items if it["bbox"][0] - 1.5 <= cx <= it["bbox"][2] + 1.5
                     and it["bbox"][1] - 1.5 <= cy <= it["bbox"][3] + 1.5]
            entry = dict(text=nfc(t), bbox=[round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])
            if cands:
                it = min(cands, key=lambda i: (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]))
                entry["item"] = it["id"]
                words.append(entry)
            else:
                ig = [r for r in rec["ignore"] if r["bbox"][0] <= cx <= r["bbox"][2] and r["bbox"][1] <= cy <= r["bbox"][3]]
                if ig:
                    entry["ignore"] = ig[0]["reason"]
                    words.append(entry)
                else:
                    problems.append(t)
        by_item = {}
        for w in words:
            if "item" in w:
                by_item.setdefault(w["item"], []).append(w)
        for it in items:
            ws = sorted(by_item.get(it["id"], []), key=lambda w: w["bbox"][0])
            got = " ".join(w["text"] for w in ws)
            if got != " ".join(it["text"].split()):
                problems.append(f"item {it['id']} {it['text']!r} != words {got!r}")
            if ws:
                it["bbox"] = [round(min(w["bbox"][0] for w in ws), 2), round(min(w["bbox"][1] for w in ws), 2),
                              round(max(w["bbox"][2] for w in ws), 2), round(max(w["bbox"][3] for w in ws), 2)]
            else:
                it["bbox"] = [round(v, 2) for v in it["bbox"]]
            it["text"] = " ".join(it["text"].split())
        if problems:
            raise SystemExit(f"{builder.doc_id} p{pno}: GT word/item mismatch: {problems[:6]}")
        # word order = reading order (item order, then x)
        order = {it["id"]: k for k, it in enumerate(items)}
        words.sort(key=lambda w: (order.get(w.get("item"), 10 ** 6), w["bbox"][0]))
        for k, it in enumerate(items):
            it["order"] = k
        blocks = []
        for b in rec["blocks"]:
            its = [it for it in items if it["block"] == b["id"]]
            if not its:
                continue
            bb = dict(b)
            bb["bbox"] = [min(i["bbox"][0] for i in its), min(i["bbox"][1] for i in its),
                          max(i["bbox"][2] for i in its), max(i["bbox"][3] for i in its)]
            bb["items"] = [i["id"] for i in its]
            blocks.append(bb)
        tables = []
        for tid, t in rec["tables"].items():
            t = dict(t)
            cells = []
            for cm in t["cells"]:
                its = [it for it in items if it.get("table") == tid and it.get("row") == cm["row"] and it.get("col") == cm["col"]]
                c = dict(cm)
                c["text"] = nfc(c["text"])
                if its:
                    c["bbox"] = [min(i["bbox"][0] for i in its), min(i["bbox"][1] for i in its),
                                 max(i["bbox"][2] for i in its), max(i["bbox"][3] for i in its)]
                cells.append(c)
            t["cells"] = cells
            t["rows"] = [[nfc(c) for c in r] for r in t["rows"]]
            t["bbox"] = [round(v, 2) for v in t["bbox"]]
            tables.append(t)
        kvs = [dict(k, label=nfc(k["label"]), value=nfc(k["value"])) for k in rec["kvs"]]
        headings = [dict(text=it["text"], level=it["heading_level"], item=it["id"]) for it in items if it["role"] == "heading"]
        g = dict(doc_id=builder.doc_id, doc_type=builder.doc_type, lang=builder.lang, title=builder.title,
                 page_index=pno, n_pages=len(builder.pages), page_size=[round(page.rect.width, 2), round(page.rect.height, 2)],
                 items=items, words=words, blocks=blocks, tables=tables, kvs=kvs, headings=headings,
                 ignore_regions=rec["ignore"])
        g["text"] = render_text(g, rows=False)
        g["text_rows"] = render_text(g, rows=True)
        g["markdown"] = render_markdown(g)
        g["numbers"] = [m.group(0) for w in words if "item" in w for m in NUM_RE.finditer(w["text"])]
        pages.append(g)
    return pages


def _block_items(g, b):
    ids = set(b["items"])
    return [it for it in g["items"] if it["id"] in ids]


def render_text(g, rows):
    """Plain reading-order text. rows=False: one item per line (like content.text);
    rows=True: table rows joined with ' | ' and kv pairs on one line (row-preserving target)."""
    out = []
    for b in g["blocks"]:
        its = _block_items(g, b)
        if rows and b["role"] == "table":
            t = next(t for t in g["tables"] if t["id"] == b["table"])
            out.append("\n".join(" | ".join(c for c in r if c) for r in t["rows"]))
        elif rows and any(it.get("kv") for it in its):
            lines, seen = [], set()
            for it in its:
                if it.get("kv"):
                    if it["kv"] in seen:
                        continue
                    seen.add(it["kv"])
                    lines.append(" ".join(i["text"] for i in its if i.get("kv") == it["kv"]))
                else:
                    lines.append(it["text"])
            out.append("\n".join(lines))
        else:
            out.append("\n".join(it["text"] for it in its))
    return "\n\n".join(out)


def render_markdown(g):
    out = []
    for b in g["blocks"]:
        its = _block_items(g, b)
        role = b["role"]
        if role == "heading":
            out.append("#" * b.get("heading_level", 1) + " " + " ".join(i["text"] for i in its))
        elif role == "para":
            txt = " ".join(i["text"] for i in its)
            if b.get("continues") and out and not out[-1].startswith(("#", "|")):
                out[-1] += " " + txt
            else:
                out.append(txt)
        elif role == "table":
            t = next(t for t in g["tables"] if t["id"] == b["table"])
            n = t["n_cols"]
            esc = lambda s: s.replace("|", "\\|")
            if t["header_rows"]:
                head, body = t["rows"][0], t["rows"][1:]
            else:
                head, body = [""] * n, t["rows"]
            lines = ["| " + " | ".join(esc(c) for c in head) + " |", "|" + "|".join(["---"] * n) + "|"]
            lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
            out.append("\n".join(lines))
        elif any(it.get("kv") for it in its):
            lines, seen = [], set()
            for it in its:
                if it.get("kv"):
                    if it["kv"] in seen:
                        continue
                    seen.add(it["kv"])
                    lines.append(" ".join(i["text"] for i in its if i.get("kv") == it["kv"]))
                else:
                    lines.append(it["text"])
            out.append("\n".join(lines))
        else:
            out.append("\n".join(i["text"] for i in its))
    return "\n\n".join(out)


# --------------------------------------------------------------------------- degradation
def add_stamp(page, rng):
    """Red rubber stamp, rotated -12 deg, drawn over content. Returns ignore bbox (page coords)."""
    W, H = page.rect.width, page.rect.height
    cx, cy = W * rng.uniform(0.5, 0.68), H * rng.uniform(0.2, 0.55)
    r = pymupdf.Rect(cx - 88, cy - 30, cx + 88, cy + 30)
    m = pymupdf.Matrix(-12)
    morph = (pymupdf.Point(cx, cy), m)
    red = (0.82, 0.12, 0.12)
    sh = page.new_shape()
    sh.draw_rect(r)
    sh.draw_rect(r + (4, 4, -4, -4))
    sh.finish(color=red, width=1.6, morph=morph, stroke_opacity=0.85)
    sh.insert_text((cx - 70, cy - 6), "EINGEGANGEN", fontname="hebo", fontsize=19, color=red, morph=morph, fill_opacity=0.85)
    sh.insert_text((cx - 42, cy + 12), "12. Feb. 2024", fontname="helv", fontsize=12, color=red, morph=morph, fill_opacity=0.85)
    sh.insert_text((cx - 38, cy + 24), "Erl.: ______", fontname="helv", fontsize=8, color=red, morph=morph, fill_opacity=0.85)
    sh.commit()
    q = r.morph(pymupdf.Point(cx, cy), m)
    return [round(v, 2) for v in q.rect], "EINGEGANGEN 12. Feb. 2024"


def degrade_page(src_doc, pno, spec, rng):
    tmp = pymupdf.open()
    tmp.insert_pdf(src_doc, from_page=pno, to_page=pno)
    page = tmp[0]
    W, H = page.rect.width, page.rect.height
    extra_ignore = []
    if spec.get("stamp"):
        bb, txt = add_stamp(page, rng)
        extra_ignore.append(dict(bbox=bb, reason="rubber_stamp", text=txt))
    dpi = spec["dpi"]
    cs = pymupdf.csRGB if spec["mode"] == "RGB" else pymupdf.csGRAY
    pix = page.get_pixmap(dpi=dpi, colorspace=cs)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).astype(np.float32)
    ink, paper = spec["ink"], spec["paper"]
    arr = ink + arr * (paper - ink) / 255.0
    if spec.get("tint"):
        arr = arr * np.array(spec["tint"], dtype=np.float32)
    if spec["noise"]:
        arr = arr + np.random.default_rng(rng.randint(0, 10 ** 6)).normal(0, spec["noise"], arr.shape).astype(np.float32)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr[:, :, 0] if pix.n == 1 else arr, "L" if pix.n == 1 else "RGB")
    fill = paper if pix.n == 1 else tuple(int(paper * t) for t in spec.get("tint", (1, 1, 1)))
    if spec["skew"]:
        img = img.rotate(spec["skew"], resample=Image.BICUBIC, fillcolor=fill)
    if spec["blur"]:
        img = img.filter(ImageFilter.GaussianBlur(spec["blur"]))
    M = skew_matrix(spec["skew"], W, H)
    out_w, out_h = W, H
    if spec.get("rot90"):
        img = img.transpose(Image.ROTATE_90)
        M = M * rot90_matrix(W)
        out_w, out_h = H, W
    buf = io.BytesIO()
    if spec["mode"] == "1":
        a = np.array(img.convert("L"))
        a = np.where(a < spec["threshold"], 0, 255).astype(np.uint8)
        sp = np.random.default_rng(rng.randint(0, 10 ** 6)).random(a.shape) < spec["speckle"]
        a[sp] = 0
        img = Image.fromarray(a, "L").convert("1")
        img.save(buf, "PNG", optimize=True)
    elif spec["jpeg"]:
        img.save(buf, "JPEG", quality=spec["jpeg"])
    else:
        img.save(buf, "PNG")
    tmp.close()
    return buf.getvalue(), (out_w, out_h), M, extra_ignore, img.size


def main():
    import random
    for d in [CORPUS / "digital", GT] + [CORPUS / k for k in DEGS] + [CORPUS / "mixed"]:
        d.mkdir(parents=True, exist_ok=True)
    manifest = dict(degradations={k: {kk: vv for kk, vv in v.items() if kk != "docs"} | ({"docs": v["docs"]} if "docs" in v else {})
                                  for k, v in DEGS.items()}, docs={}, pages=[])
    for fn in ALL:
        b = fn()
        pdf = b.save()
        (CORPUS / "digital" / f"{b.doc_id}.pdf").write_bytes(pdf)
        gts = build_gt(b, pdf)
        for g in gts:
            (GT / f"{b.doc_id}_p{g['page_index']}.json").write_text(json.dumps(g, ensure_ascii=False, indent=1))
        manifest["docs"][b.doc_id] = dict(doc_type=b.doc_type, lang=b.lang, title=b.title, n_pages=len(gts),
                                          digital_pdf=f"corpus/digital/{b.doc_id}.pdf")
        for g in gts:
            manifest["pages"].append(dict(deg="digital", doc=b.doc_id, pdf=f"corpus/digital/{b.doc_id}.pdf",
                                          page_index=g["page_index"], src_page=g["page_index"],
                                          gt=f"gt/{b.doc_id}_p{g['page_index']}.json",
                                          page_to_scan=[1, 0, 0, 1, 0, 0], scan_size=g["page_size"], image_dpi=None,
                                          extra_ignore=[]))
        src = pymupdf.open("pdf", pdf)
        for deg, spec in DEGS.items():
            sel = spec.get("docs")
            if sel is not None and b.doc_id not in sel:
                continue
            pnos = sel[b.doc_id] if sel is not None else list(range(len(src)))
            rng = random.Random(hash((b.doc_id, deg)) & 0xFFFF if False else sum(map(ord, b.doc_id + deg)))
            out = pymupdf.open()
            for k, pno in enumerate(pnos):
                data, (ow, oh), M, extra, px = degrade_page(src, pno, spec, rng)
                p = out.new_page(width=ow, height=oh)
                p.insert_image(p.rect, stream=data)
                manifest["pages"].append(dict(deg=deg, doc=b.doc_id, pdf=f"corpus/{deg}/{b.doc_id}.pdf", page_index=k,
                                              src_page=pno, gt=f"gt/{b.doc_id}_p{pno}.json", page_to_scan=mat_list(M),
                                              scan_size=[round(ow, 2), round(oh, 2)], image_px=list(px),
                                              image_dpi=spec["dpi"], extra_ignore=extra))
            out.save(CORPUS / deg / f"{b.doc_id}.pdf", deflate=True)
            out.close()
        print(f"{b.doc_id:28s} pages={len(gts)} items={sum(len(g['items']) for g in gts)} "
              f"words={sum(len(g['words']) for g in gts)} numbers={sum(len(g['numbers']) for g in gts)}")
    # mixed: digital cover letter + office-scanned Jahresrechnung + tiny scanner text layer
    rng = random.Random(99)
    out = pymupdf.open()
    out.insert_pdf(pymupdf.open(CORPUS / "digital" / "brief.pdf"))
    manifest["pages"].append(dict(deg="mixed", doc="brief", pdf="corpus/mixed/jahresrechnung_mixed.pdf", page_index=0,
                                  src_page=0, gt="gt/brief_p0.json", page_to_scan=[1, 0, 0, 1, 0, 0],
                                  scan_size=[595.28, 841.89], image_dpi=None, extra_ignore=[]))
    src = pymupdf.open(CORPUS / "digital" / "jahresrechnung.pdf")
    for pno in range(len(src)):
        data, (ow, oh), M, extra, px = degrade_page(src, pno, DEGS["office300"], rng)
        p = out.new_page(width=ow, height=oh)
        p.insert_image(p.rect, stream=data)
        p.insert_text((40, oh - 12), "Scan 12.03.2024 iR-ADV C5535 Seite %d" % (pno + 1), fontsize=6)
        manifest["pages"].append(dict(deg="mixed", doc="jahresrechnung", pdf="corpus/mixed/jahresrechnung_mixed.pdf",
                                      page_index=pno + 1, src_page=pno, gt=f"gt/jahresrechnung_p{pno}.json",
                                      page_to_scan=mat_list(M), scan_size=[round(ow, 2), round(oh, 2)], image_px=list(px),
                                      image_dpi=300, extra_ignore=[dict(bbox=[30, oh - 22, 300, oh - 4], reason="scanner_text_layer",
                                                                        text="Scan 12.03.2024 iR-ADV C5535")]))
    out.save(CORPUS / "mixed" / "jahresrechnung_mixed.pdf", deflate=True)
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    n = {}
    for p in manifest["pages"]:
        n[p["deg"]] = n.get(p["deg"], 0) + 1
    print("pages per degradation:", n)


if __name__ == "__main__":
    main()
