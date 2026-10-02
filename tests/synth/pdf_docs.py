"""Synthetic PDF documents for the layout-mode tests (PyMuPDF renders, no corpus).

Everything here is deterministic: a seeded `random.Random`, fixed word lists,
fixed geometry. Builders return PDF bytes; `digital_words_as_ocr` turns a
born-digital page into the "perfect OCR" word rows a fake backend returns, so
the OCR → layout path runs on every CI leg without Tesseract.
"""

from __future__ import annotations

import io
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import fitz  # type: ignore[import-untyped]

from knovas_extract._ocr.tsv import OcrPageResult, OcrWord, mean_confidence, words_to_text

HELV = "helv"
HELV_BOLD = "hebo"

# ── low-level drawing ──────────────────────────────────────────────────────


def right_text(
    page: Any, x1: float, y: float, text: str, fontsize: float = 10, fontname: str = HELV
) -> None:
    """Right-align *text* so its right edge sits at ``x1`` (amount columns)."""
    w = fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    page.insert_text((x1 - w, y), text, fontsize=fontsize, fontname=fontname)


def pdf_bytes(doc: Any) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ── fiduciary pages ────────────────────────────────────────────────────────

BILANZ_ROWS: list[tuple[str, str, str]] = [
    ("Flüssige Mittel", "1'234'567.80", "987'654.30"),
    ("Forderungen aus Lieferungen und Leistungen", "456'789.00", "400'120.00"),
    ("Übrige kurzfristige Forderungen", "23'450.15", "19'870.00"),
    ("Vorräte und nicht fakturierte Dienstleistungen", "78'300.00", "81'250.00"),
    ("Aktive Rechnungsabgrenzungen", "12'000.00", "9'500.00"),
    ("Sachanlagen", "345'000.00", "360'000.00"),
]
BILANZ_TOTAL: tuple[str, str, str] = ("Total Aktiven", "2'150'106.95", "1'858'394.30")

ERFOLG_ROWS: list[tuple[str, str, str]] = [
    ("Nettoerlös aus Lieferungen und Leistungen", "3'456'000.00", "3'120'500.00"),
    ("Materialaufwand", "-1'234'000.00", "-1'100'250.00"),
    ("Personalaufwand", "-987'000.00", "-920'000.00"),
    ("Übriger betrieblicher Aufwand", "-345'600.00", "-330'100.00"),
    ("Abschreibungen", "-120'000.00", "-115'000.00"),
    ("Finanzertrag", "4'500.00", "3'900.00"),
]
ERFOLG_TOTAL: tuple[str, str, str] = ("Jahresgewinn", "773'900.00", "659'050.00")


def add_statement_page(
    doc: Any,
    *,
    company: str = "Müller AG",
    title: str = "Jahresrechnung 2023",
    subtitle: str = "Bilanz per 31. Dezember 2023",
    label_header: str = "Aktiven",
    rows: Sequence[tuple[str, str, str]] = BILANZ_ROWS,
    total: tuple[str, str, str] = BILANZ_TOTAL,
) -> Any:
    """A Swiss balance-sheet style page: title, 2-line column header, right-aligned
    amounts, a bold totals row."""
    page = doc.new_page()
    page.insert_text((72, 80), company, fontsize=16, fontname=HELV_BOLD)
    page.insert_text((72, 108), title, fontsize=13, fontname=HELV_BOLD)
    page.insert_text((72, 126), subtitle, fontsize=11, fontname=HELV_BOLD)
    y = 170.0
    page.insert_text((72, y), label_header, fontsize=10, fontname=HELV_BOLD)
    right_text(page, 400, y, "31.12.2023", 10, HELV_BOLD)
    right_text(page, 500, y, "31.12.2022", 10, HELV_BOLD)
    y += 14
    right_text(page, 400, y, "CHF", 10, HELV_BOLD)
    right_text(page, 500, y, "CHF", 10, HELV_BOLD)
    y += 20
    for label, a, b in rows:
        page.insert_text((72, y), label, fontsize=10)
        right_text(page, 400, y, a, 10)
        right_text(page, 500, y, b, 10)
        y += 16
    y += 6
    page.insert_text((72, y), total[0], fontsize=10, fontname=HELV_BOLD)
    right_text(page, 400, y, total[1], 10, HELV_BOLD)
    right_text(page, 500, y, total[2], 10, HELV_BOLD)
    return page


def bilanz_pdf() -> bytes:
    doc = fitz.open()
    add_statement_page(doc)
    return pdf_bytes(doc)


def three_statements_pdf() -> bytes:
    doc = fitz.open()
    add_statement_page(doc)
    add_statement_page(
        doc,
        subtitle="Erfolgsrechnung 2023",
        label_header="Ertrag und Aufwand",
        rows=ERFOLG_ROWS,
        total=ERFOLG_TOTAL,
    )
    add_statement_page(
        doc,
        company="Meier GmbH",
        subtitle="Bilanz per 31. Dezember 2023",
        rows=BILANZ_ROWS[::-1],
    )
    return pdf_bytes(doc)


# ── prose pages ────────────────────────────────────────────────────────────

_PROSE = (
    "Dieser Vertrag bezweckt die einheitliche Ausübung der Aktionärsrechte, die "
    "Regelung der Übertragung von Aktien und die Sicherstellung der Kontinuität der "
    "Gesellschaft. Die Parteien verpflichten sich, ihre Stimmrechte gemäss den "
    "Bestimmungen dieses Vertrages auszuüben. "
)


def add_prose_page(doc: Any, *, heading: str = "Art. 1 Zweck", repeat: int = 3) -> Any:
    page = doc.new_page()
    page.insert_text((72, 80), heading, fontsize=11, fontname=HELV_BOLD)
    rc = page.insert_textbox(fitz.Rect(72, 100, 520, 760), _PROSE * repeat, fontsize=10)
    assert rc >= 0, "prose did not fit the frame"
    return page


def prose_pdf(pages: int = 2) -> bytes:
    doc = fitz.open()
    for k in range(pages):
        add_prose_page(doc, heading=f"Art. {k + 1} Zweck", repeat=3 + k)
    return pdf_bytes(doc)


# ── raster pages (scans) and the fake OCR backend ──────────────────────────


def rasterize(src: bytes, page_index: int, *, dpi: int = 150) -> Any:
    """Pixmap of one page of a PDF (a "scan" of a born-digital page)."""
    doc = fitz.open(stream=src, filetype="pdf")
    pix = doc.load_page(page_index).get_pixmap(dpi=dpi)
    doc.close()
    return pix


def digital_words_as_ocr(src: bytes, page_index: int, *, conf: float = 96.0) -> list[OcrWord]:
    """The word boxes of a born-digital page in page points as `OcrWord` records —
    what a perfect OCR engine would return for its scan."""
    doc = fitz.open(stream=src, filetype="pdf")
    page = doc.load_page(page_index)
    words: list[OcrWord] = []
    for x0, y0, x1, y1, t, b, line_no, _wno in page.get_text("words"):
        if not t.strip():
            continue
        words.append(
            OcrWord(
                text=t,
                x0=round(float(x0), 2),
                y0=round(float(y0), 2),
                x1=round(float(x1), 2),
                y1=round(float(y1), 2),
                conf=conf,
                block=int(b),
                par=0,
                line=int(line_no),
                line_h=round(float(y1 - y0), 2),
            )
        )
    doc.close()
    return words


@dataclass
class FakeWordsOcrBackend:
    """An `IOcrBackend` returning canned word rows (page points) per page index.

    ``needs_image=True`` keeps the real render / preprocess / worker-pool path
    of the pipeline (the image is ignored); the result is deterministic and
    independent of the worker count, which is what the determinism tests pin.
    """

    pages: dict[int, list[OcrWord]]
    name: str = "fake-words"
    version: str = "0"
    needs_image: bool = True
    calls: list[int] = field(default_factory=list)

    def recognize(self, page_image: Any) -> OcrPageResult:
        self.calls.append(page_image.page_index)
        words = list(self.pages.get(page_image.page_index, []))
        return OcrPageResult(
            text=words_to_text(words), words=words, mean_conf=mean_confidence(words)
        )


def scanned_pdf(src: bytes, *, dpi: int = 150, cover_pages: int = 0) -> bytes:
    """Every page of *src* as a full-page raster (the first ``cover_pages`` pages are
    kept born-digital — a digital cover letter in front of a scanned body)."""
    source = fitz.open(stream=src, filetype="pdf")
    out = fitz.open()
    for i in range(source.page_count):
        if i < cover_pages:
            out.insert_pdf(source, from_page=i, to_page=i)
            continue
        src_page = source.load_page(i)
        page = out.new_page(width=src_page.rect.width, height=src_page.rect.height)
        page.insert_image(page.rect, pixmap=src_page.get_pixmap(dpi=dpi))
    source.close()
    return pdf_bytes(out)


def stamped_scan_pdf(src: bytes, page_index: int = 0, *, dpi: int = 150) -> bytes:
    """One full-page raster of ``src[page_index]`` with a short scanner stamp as the
    only text layer (decision rule 4: layer kept, OCR text appended)."""
    source = fitz.open(stream=src, filetype="pdf")
    src_page = source.load_page(page_index)
    out = fitz.open()
    page = out.new_page(width=src_page.rect.width, height=src_page.rect.height)
    page.insert_image(page.rect, pixmap=src_page.get_pixmap(dpi=dpi))
    page.insert_text((400, 20), "Gescannt am 12.03.2024 14:33", fontsize=7)
    source.close()
    return pdf_bytes(out)


def fake_backend_for(src: bytes, page_indices: Sequence[int] | None = None) -> FakeWordsOcrBackend:
    """A fake backend whose canned words are the digital words of *src*'s pages."""
    doc = fitz.open(stream=src, filetype="pdf")
    n = doc.page_count
    doc.close()
    idx = list(range(n)) if page_indices is None else list(page_indices)
    return FakeWordsOcrBackend({i: digital_words_as_ocr(src, i) for i in idx})


# ── law-firm prose generator (deterministic) ───────────────────────────────

_SUBJECTS = [
    "Die Parteien",
    "Der Verkäufer",
    "Die Käuferin",
    "Die Gesellschaft",
    "Der Verwaltungsrat",
    "Die Aktionäre",
    "Der Mieter",
    "Die Vermieterin",
    "Der Auftragnehmer",
    "Die Auftraggeberin",
]
_VERBS = [
    "verpflichten sich",
    "verpflichtet sich",
    "anerkennt",
    "gewährleistet",
    "übernimmt",
    "trägt",
    "bestätigt",
    "verzichtet auf",
    "haftet für",
    "informiert über",
]
_OBJECTS = [
    "die vertragsgemässe Erfüllung sämtlicher Pflichten",
    "die Einhaltung der gesetzlichen Vorschriften",
    "die vereinbarte Vertraulichkeit aller Geschäftsgeheimnisse",
    "die rechtzeitige Zahlung des vereinbarten Entgelts",
    "die Übertragung der Aktien zum vereinbarten Zeitpunkt",
    "die Mängelfreiheit des Kaufgegenstandes",
    "die Zustimmung der Generalversammlung",
    "die Kosten des Verfahrens vor der zuständigen Behörde",
    "die Wahrung der Interessen der Gegenpartei",
    "die Pflicht zur Rechenschaftsablage gegenüber dem Verwaltungsrat",
]
_TAILS = [
    "gemäss den Bestimmungen dieses Vertrages",
    "unter Vorbehalt zwingenden Rechts",
    "soweit nichts anderes vereinbart wurde",
    "nach Massgabe der anwendbaren Gesetzgebung",
    "im Rahmen der üblichen Geschäftspraxis",
    "innerhalb von dreissig Tagen nach Erhalt der Mitteilung",
    "vorbehältlich der Genehmigung durch die Behörden",
    "ohne weitere Entschädigung",
    "unter Ausschluss jeder weitergehenden Haftung",
    "in schriftlicher Form",
]
_CLAUSE_TITLES = [
    "Zweck",
    "Vertragsgegenstand",
    "Übertragung der Aktien",
    "Vorkaufsrecht",
    "Stimmbindung",
    "Vertraulichkeit",
    "Haftung",
    "Dauer und Kündigung",
    "Salvatorische Klausel",
    "Anwendbares Recht und Gerichtsstand",
    "Mitwirkungspflichten",
    "Vergütung",
]
_PARTY_NAMES = [
    ("Müller AG", "Bahnhofstrasse 12", "8001 Zürich"),
    ("Meier GmbH", "Seestrasse 45", "6300 Zug"),
    ("Keller Immobilien AG", "Marktgasse 7", "3011 Bern"),
    ("Brunner Architektur AG", "Rue du Rhône 3", "1204 Genève"),
    ("Steiner Holzbau GmbH", "Dorfstrasse 88", "9000 St. Gallen"),
    ("Weber & Partner AG", "Via Nassa 15", "6900 Lugano"),
]
_LAWYERS = ["Dr. iur. Hans Meier", "lic. iur. Anna Keller", "RA Peter Brunner", "Dr. Eva Weber"]


def _sentence(rng: random.Random) -> str:
    s = f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)}"
    if rng.random() < 0.6:
        s += f", {rng.choice(_TAILS)}"
    if rng.random() < 0.3:
        s += f" (vgl. Art. {rng.randint(1, 30)} Abs. {rng.randint(1, 4)})"
    return s + "."


def _paragraph(rng: random.Random, n_sentences: int) -> str:
    return " ".join(_sentence(rng) for _ in range(n_sentences))


@dataclass(slots=True)
class LawFirmDoc:
    """One generated contract: the PDF and the page count."""

    data: bytes
    pages: int
    columns: int
    seed: int


def lawfirm_pdf(seed: int, *, columns: int = 1, pages: int = 2) -> LawFirmDoc:
    """A born-digital contract: title, ``Parteien:`` block of three lines, numbered
    clauses ``Art. N Titel`` with 1-3 paragraphs each, a running header and footer
    on every page, footnotes at the bottom. ``columns=2`` fills two text frames per
    page (``page.insert_textbox`` into a left and a right frame)."""
    rng = random.Random(seed)
    p1, p2 = rng.sample(_PARTY_NAMES, 2)
    lawyer = rng.choice(_LAWYERS)
    number = f"{2020 + rng.randint(0, 5)}-{rng.randint(1, 999):03d}"
    header = f"{p1[0]} / {p2[0]} - Vertrag Nr. {number}"
    doc = fitz.open()
    clause_no = 1
    body_size = 10.0 if columns == 1 else 9.5
    for k in range(pages):
        page = doc.new_page()  # A4
        page.insert_text((72, 45), header, fontsize=8)
        frames: list[tuple[fitz.Rect, list[str]]]
        if columns == 1:
            frames = [(fitz.Rect(72, 70, 523, 740), [])]
            n_clauses = (2, 2)
        else:
            frames = [(fitz.Rect(72, 70, 290, 740), []), (fitz.Rect(305, 70, 523, 740), [])]
            n_clauses = (2, 1)
        if k == 0:
            title = f"Aktionärbindungsvertrag Nr. {number}"
            page.insert_text((72, 90), title, fontsize=13, fontname=HELV_BOLD)
            intro = [
                "Parteien:",
                f"{p1[0]}, {p1[1]}, {p1[2]} (nachfolgend «Partei 1»)",
                f"{p2[0]}, {p2[1]}, {p2[2]} (nachfolgend «Partei 2»)",
                f"vertreten durch {lawyer}, Rechtsanwalt, Zürich",
                "",
            ]
            frames[0] = (
                fitz.Rect(frames[0][0].x0, 110, frames[0][0].x1, frames[0][0].y1),
                intro,
            )
        for fi, (rect, lines) in enumerate(frames):
            for _ in range(n_clauses[fi]):
                title_c = _CLAUSE_TITLES[(clause_no - 1) % len(_CLAUSE_TITLES)]
                lines.append(f"Art. {clause_no} {title_c}")
                for _p in range(rng.randint(1, 2)):
                    lines.append(_paragraph(rng, rng.randint(2, 3)))
                lines.append("")
                clause_no += 1
            rc = page.insert_textbox(rect, "\n".join(lines).rstrip("\n"), fontsize=body_size)
            assert rc >= 0, f"frame overflow on page {k} (seed {seed}, columns {columns})"
        foot = f"{k + 1} Vgl. BGE {100 + rng.randint(0, 50)} III {rng.randint(100, 499)} E. {rng.randint(1, 6)}.{rng.randint(1, 4)}."
        page.insert_textbox(fitz.Rect(72, 752, 523, 790), foot, fontsize=7.5)
        right_text(page, 523, 812, f"Seite {k + 1} von {pages}", 8)
    return LawFirmDoc(pdf_bytes(doc), pages, columns, seed)


def lawfirm_corpus(n: int = 20, *, pages: int = 2) -> list[LawFirmDoc]:
    """``n`` contracts, alternating one- and two-column layouts, seeds 0..n-1."""
    return [lawfirm_pdf(seed, columns=1 + seed % 2, pages=pages) for seed in range(n)]
