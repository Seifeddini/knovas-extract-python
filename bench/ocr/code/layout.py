"""Tiny layout engine on top of reportlab that records semantic ground truth while drawing.

Coordinates are PDF points with a TOP-LEFT origin (y grows downwards) - the same convention
PyMuPDF uses - so recorded boxes can be compared 1:1 with PyMuPDF / OCR word boxes.
Every drawn string becomes an *item* (one visual text line or one table-cell line) with a role,
a block id, reading order (= creation order) and optional table/kv/heading metadata.
"""
import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import black, Color

W, H = A4

BOLD = {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold", "Courier": "Courier-Bold",
        "Helvetica-Bold": "Helvetica-Bold", "Times-Bold": "Times-Bold", "Courier-Bold": "Courier-Bold"}


class Builder:
    def __init__(self, doc_id, doc_type, lang, title):
        self.doc_id, self.doc_type, self.lang, self.title = doc_id, doc_type, lang, title
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=A4, invariant=1)
        self.pages = []
        self._new()

    # ---------------------------------------------------------------- structure
    def _new(self):
        self.pages.append(dict(items=[], tables={}, kvs=[], blocks=[], ignore=[]))
        self.blk = None

    def page_break(self):
        self.c.showPage()
        self._new()

    @property
    def P(self):
        return self.pages[-1]

    def block(self, role, column=0, **meta):
        b = dict(id=f"b{len(self.P['blocks'])}", role=role, column=column,
                 order=len(self.P['blocks']), **meta)
        self.P["blocks"].append(b)
        self.blk = b["id"]
        return b["id"]

    def ignore(self, bbox, reason, text=""):
        self.P["ignore"].append(dict(bbox=[round(v, 2) for v in bbox], reason=reason, text=text))

    # ---------------------------------------------------------------- primitives
    def text(self, x, y, s, font="Helvetica", size=9, align="left", role="text", color=black, **meta):
        """Draw one line. y = baseline (top-origin)."""
        w = stringWidth(s, font, size)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        c = self.c
        c.setFillColor(color)
        c.setFont(font, size)
        c.drawString(x0, H - y, s)
        it = dict(id=f"i{len(self.P['items'])}", text=s, bbox=[x0, y - 0.85 * size, x0 + w, y + 0.3 * size],
                  role=role, block=self.blk, font=font, size=size, **meta)
        self.P["items"].append(it)
        return it

    def text_justified(self, x, y, words, width, font, size, role="para", **meta):
        tot = sum(stringWidth(t, font, size) for t in words)
        gap = (width - tot) / max(1, len(words) - 1)
        self.c.setFillColor(black)
        self.c.setFont(font, size)
        xx = x
        for t in words:
            self.c.drawString(xx, H - y, t)
            xx += stringWidth(t, font, size) + gap
        it = dict(id=f"i{len(self.P['items'])}", text=" ".join(words), bbox=[x, y - 0.85 * size, x + width, y + 0.3 * size],
                  role=role, block=self.blk, font=font, size=size, **meta)
        self.P["items"].append(it)
        return it

    @staticmethod
    def wrap(text, width, font, size):
        lines, cur = [], []
        for w_ in text.split():
            trial = cur + [w_]
            if cur and stringWidth(" ".join(trial), font, size) > width:
                lines.append(cur)
                cur = [w_]
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def para(self, x, y, width, text, font="Helvetica", size=10, leading=None, justify=False, role="para",
             new_block=True, color=black, **meta):
        """Wrap + draw a paragraph; y = top of first line. Returns y below the paragraph."""
        leading = leading or size * 1.3
        if new_block:
            self.block(role, **{k: v for k, v in meta.items() if k in ("column", "heading_level")})
        lines = self.wrap(text, width, font, size)
        for li, lw in enumerate(lines):
            base = y + size
            if justify and li < len(lines) - 1 and len(lw) > 1:
                self.text_justified(x, base, lw, width, font, size, role=role, **meta)
            else:
                self.text(x, base, " ".join(lw), font=font, size=size, role=role, color=color, **meta)
            y += leading
        return y

    def heading(self, x, y, s, level, size, font="Helvetica-Bold", align="left", color=black, **meta):
        self.block("heading", heading_level=level, **{k: v for k, v in meta.items() if k == "column"})
        self.text(x, y + size, s, font=font, size=size, align=align, role="heading", heading_level=level, color=color, **meta)
        return y + size * 1.6

    def lines_block(self, x, y, lines, role, font="Helvetica", size=9, leading=None, align="left", color=black):
        leading = leading or size * 1.3
        self.block(role)
        for s in lines:
            self.text(x, y + size, s, font=font, size=size, align=align, role=role, color=color)
            y += leading
        return y

    def kv(self, x, y, label, value, value_x, lfont="Helvetica", lsize=8, vfont="Courier", vsize=9, value_align="left"):
        """Label/value pair on one line (form field)."""
        kid = f"k{len(self.P['kvs'])}"
        a = self.text(x, y, label, font=lfont, size=lsize, role="kv_label", kv=kid)
        b = self.text(value_x, y, value, font=vfont, size=vsize, align=value_align, role="kv_value", kv=kid)
        self.P["kvs"].append(dict(id=kid, label=label, value=value, label_item=a["id"], value_item=b["id"]))
        return a, b

    def dotted(self, x0, x1, y):
        c = self.c
        c.saveState()
        c.setLineWidth(0.8)
        c.setLineCap(1)
        c.setDash([0.01, 2.2])
        c.line(x0, H - y, x1, H - y)
        c.restoreState()

    def hline(self, x0, x1, y, w=0.5, color=black):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setLineWidth(w)
        self.c.line(x0, H - y, x1, H - y)
        self.c.restoreState()

    def rect(self, x, y, w, h, lw=0.5, fill=None, stroke=True):
        c = self.c
        c.saveState()
        c.setLineWidth(lw)
        if fill is not None:
            c.setFillColor(fill)
        c.rect(x, H - y - h, w, h, stroke=1 if stroke else 0, fill=1 if fill is not None else 0)
        c.restoreState()

    # ---------------------------------------------------------------- tables
    def table(self, x, y, cols, rows, font="Helvetica", size=9, row_h=None, ruled=False, hrules=False,
              header_rows=1, bold_rows=(), rules_above=(), shade=False, kind="table", leader_col=None,
              pad=3, vfont=None, cell_font=None):
        """cols: [(width, align)]. rows: list of lists (cell text may contain '\\n').
        ruled=True -> full grid; hrules=True -> horizontal rules only; leader_col -> dot leaders after text.
        vfont: font for non-first columns (e.g. Courier for typed values)."""
        tid = f"t{len(self.P['tables'])}"
        self.block("table", table=tid)
        tw = sum(cw for cw, _ in cols)
        row_h = row_h or size * 1.75
        ys = [y]
        cells_meta = []
        for r, row in enumerate(rows):
            nl = max(len(str(cell).split("\n")) for cell in row) if row else 1
            h = row_h + (nl - 1) * size * 1.15
            if shade and r >= header_rows and (r - header_rows) % 2 == 1:
                self.rect(x, y, tw, h, fill=Color(0.9, 0.9, 0.9), stroke=False)
            bold = r < header_rows or r in bold_rows
            xx = x
            for ci, (cw, al) in enumerate(cols):
                txt = str(row[ci]) if ci < len(row) else ""
                f = (vfont if (vfont and ci > 0 and r >= header_rows) else (cell_font or font))
                f = BOLD.get(f, f) if bold else f
                last_end = None
                for li, part in enumerate(txt.split("\n")):
                    if not part:
                        continue
                    base = y + pad + size * 0.95 + li * size * 1.15
                    tx = xx + pad if al == "left" else (xx + cw - pad if al == "right" else xx + cw / 2)
                    it = self.text(tx, base, part, font=f, size=size, align=al, role="table_cell",
                                   table=tid, row=r, col=ci)
                    last_end = (it["bbox"][2], base)
                if leader_col == ci and r >= header_rows and last_end:
                    if xx + cw - 4 - (last_end[0] + 3) > 8:
                        self.dotted(last_end[0] + 3, xx + cw - 4, last_end[1])
                cells_meta.append(dict(row=r, col=ci, text=" ".join(txt.split())))
                xx += cw
            if r in rules_above:
                self.hline(x, x + tw, y, w=0.6)
            y += h
            ys.append(y)
        if ruled:
            for yy in ys:
                self.hline(x, x + tw, yy, w=0.5)
            xx = x
            for cw, _ in cols:
                self.c.setLineWidth(0.5)
                self.c.line(xx, H - ys[0], xx, H - ys[-1])
                xx += cw
            self.c.line(xx, H - ys[0], xx, H - ys[-1])
        elif hrules:
            for yy in ys:
                self.hline(x, x + tw, yy, w=0.4)
        self.P["tables"][tid] = dict(id=tid, kind=kind, ruled=bool(ruled), hrules=bool(hrules), header_rows=header_rows,
                                     n_cols=len(cols), bbox=[x, ys[0], x + tw, ys[-1]],
                                     rows=[[" ".join(str(c).split()) for c in row] + [""] * (len(cols) - len(row)) for row in rows],
                                     cells=cells_meta)
        return y

    def save(self):
        self.c.save()
        return self.buf.getvalue()
