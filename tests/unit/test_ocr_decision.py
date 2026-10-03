"""Obligation tests: the per-page OCR decision (GI-EXTRACT-01).

Alloy: KnowledgeBase ``models/alloy/mechanisms/client_pipeline.als::
PerPageOcrDecisionMechanism`` (model ``data_plane/ocr_budget_failsoft.als``,
mutant ``ocr__document_level_decision`` = the behaviour shipped in 0.3.x:
``_ocr_should_run(use_ocr, has_text=bool(full_text))`` decides for the whole
document, so a scanned body behind a digital cover page is never OCR'd).

Decision rules mirrored here (``knovas_extract._ocr.decision``):

    usable text layer      -> keep verbatim, never OCR (byte identity for
                              born-digital pages, also when a large image is
                              on the page — a title page with a logo)
    no usable text + image -> OCR candidate
    garbage layer + image  -> OCR candidate; the garbage text is NOT kept
    no text, no image      -> left empty (blank duplex back side)

The OCR engine is injected (``OcrOptions(backend=...)``) so these tests run
on every CI leg without Tesseract. RED until the feature lands.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pytest

from knovas_extract import extract

fitz = pytest.importorskip("fitz")

pytestmark = [pytest.mark.unit]


# ── helpers ────────────────────────────────────────────────────────────────


def _raster_of(text: str, *, dpi: int = 150) -> fitz.Pixmap:
    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 72), text, fontsize=14)
    pix = page.get_pixmap(dpi=dpi)
    src.close()
    return pix


def _pdf(pages: list[dict]) -> bytes:
    """Build a PDF from page specs: {"text": str|None, "image": str|None,
    "image_frac": float (fraction of page height covered by the image),
    "text_pos": (x, y)}."""
    doc = fitz.open()
    for spec in pages:
        page = doc.new_page()  # A4 points
        rect = page.rect
        if spec.get("image"):
            frac = spec.get("image_frac", 1.0)
            img_rect = fitz.Rect(rect.x0, rect.y0 + rect.height * (1 - frac), rect.x1, rect.y1)
            page.insert_image(img_rect, pixmap=_raster_of(spec["image"]))
        if spec.get("text"):
            page.insert_text(spec.get("text_pos", (72, 72)), spec["text"], fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@dataclass
class FakeOcrBackend:
    """An ``IOcrBackend`` that returns canned text and records every call."""

    text: str = "OCR-TEXT 1'234.50"
    name: str = "fake"
    calls: list[int] = field(default_factory=list)

    def recognize(self, page_image):
        self.calls.append(page_image.page_index)
        return self.text


def _extract_with(data: bytes, backend: FakeOcrBackend, **kw):
    from knovas_extract import OcrOptions

    return extract(data, mime="application/pdf", ocr=OcrOptions(backend=backend), **kw)


# ── the pure decision function ─────────────────────────────────────────────


class TestDecidePage:
    def test_usable_text_wins_over_image(self):
        from knovas_extract._ocr.decision import decide_page

        d = decide_page(
            "Jahresrechnung 2023\nMüller AG, Bahnhofstrasse 12", image_cover=0.6, use_ocr="auto"
        )
        assert d.needs_ocr is False and d.keep_text_layer is True

    def test_no_text_with_image_is_a_candidate(self):
        from knovas_extract._ocr.decision import decide_page

        d = decide_page("", image_cover=0.9, use_ocr="auto")
        assert d.needs_ocr is True

    def test_no_text_no_image_is_left_empty(self):
        from knovas_extract._ocr.decision import decide_page

        d = decide_page("", image_cover=0.0, use_ocr="auto")
        assert d.needs_ocr is False and d.reason == "no_image"

    def test_garbage_layer_over_image_is_a_candidate_and_not_kept(self):
        from knovas_extract._ocr.decision import decide_page

        garbage = "(cid:12)(cid:4)(cid:88) ��� (cid:9)(cid:3)"
        d = decide_page(garbage, image_cover=0.8, use_ocr="auto")
        assert d.needs_ocr is True and d.keep_text_layer is False

    def test_use_ocr_false_never_ocrs(self):
        from knovas_extract._ocr.decision import decide_page

        assert decide_page("", image_cover=1.0, use_ocr=False).needs_ocr is False

    def test_short_scanner_stamp_over_full_page_image_is_ocrd_and_text_appended(self):
        """'Scan 12.03.2024 14:33' over a raster page: too short to be the
        page's text; the stamp is kept AND the page is OCR'd."""
        from knovas_extract._ocr.decision import decide_page

        d = decide_page("Scan 12.03.2024", image_cover=0.95, use_ocr="auto")
        assert d.needs_ocr is True and d.keep_text_layer is True

    def test_scanner_stamp_that_looks_usable_over_full_page_raster_is_ocrd_and_kept(self):
        """A full-page raster (>= 85 % cover) is a scan whatever stamp sits
        on it: 'Gescannt am 12.03.2024 14:33 Seite 2 von 12' has >= 2
        alphabetic tokens and >= 20 chars, yet the page is OCR'd and the
        stamp kept."""
        from knovas_extract._ocr.decision import decide_page

        d = decide_page(
            "Gescannt am 12.03.2024 14:33 Seite 2 von 12", image_cover=0.98, use_ocr="auto"
        )
        assert d.needs_ocr is True and d.keep_text_layer is True

    def test_same_stamp_over_a_partial_image_is_a_usable_layer(self):
        from knovas_extract._ocr.decision import decide_page

        d = decide_page(
            "Gescannt am 12.03.2024 14:33 Seite 2 von 12", image_cover=0.6, use_ocr="auto"
        )
        assert d.needs_ocr is False and d.keep_text_layer is True


class TestTextLayerIsGarbage:
    def test_cid_soup(self):
        from knovas_extract._ocr.decision import text_layer_is_garbage

        assert text_layer_is_garbage("(cid:1)(cid:2)(cid:3) (cid:4)(cid:5)") is True

    def test_real_german_prose_is_not_garbage(self):
        from knovas_extract._ocr.decision import text_layer_is_garbage

        prose = (
            "Die Parteien vereinbaren, dass der Vertrag am 31. Dezember 2024 endet. "
            "Flüssige Mittel 1'234'567.80 CHF; Forderungen aus Lieferungen 456'789.00."
        )
        assert text_layer_is_garbage(prose) is False

    def test_amount_only_table_page_is_not_garbage(self):
        """A Bilanz page is mostly digits — digits are not garbage tokens."""
        from knovas_extract._ocr.decision import text_layer_is_garbage

        assert text_layer_is_garbage("Aktiven 1'234.50 987.30\nPassiven 2'000.00 1'500.00") is False


# ── the mechanism end to end through extract() ─────────────────────────────


class TestPerPageOcrDecisionMechanism:
    """Alloy obligation: mechanisms/client_pipeline.als::PerPageOcrDecisionMechanism."""

    def test_scanned_body_behind_digital_cover_is_ocrd(self):
        """The mixed document: page 1 digital, page 2 raster-only. Today the
        document-level decision sees text on page 1 and OCRs nothing."""
        backend = FakeOcrBackend()
        data = _pdf(
            [
                {"text": "Jahresrechnung 2023 Müller AG — Begleitschreiben zum Abschluss"},
                {"image": "Bilanz per 31.12.2023", "image_frac": 1.0},
            ]
        )
        r = _extract_with(data, backend)
        assert backend.calls == [1], "exactly the raster page is OCR'd"
        assert r.metadata.extra["pdf:ocr_pages"] == 1
        assert r.metadata.extra["pdf:text_pages"] == 1
        assert r.content.pages is not None
        assert "Begleitschreiben" in r.content.pages[0].text
        assert "OCR-TEXT" in r.content.pages[1].text

    def test_full_page_scan_behind_scanner_stamp_is_ocrd(self):
        """Page 1 a digital letter, page 2 a full-page raster carrying the
        scanner's text stamp. The stamp passes the usable-text test on its
        own; the page is still OCR'd, the stamp kept, the OCR text appended."""
        backend = FakeOcrBackend()
        data = _pdf(
            [
                {"text": "Sehr geehrte Damen und Herren, anbei der Jahresabschluss 2023."},
                {
                    "text": "Gescannt am 12.03.2024 14:33 Seite 2 von 12",
                    "image": "Bilanz per 31.12.2023",
                    "image_frac": 1.0,
                    "text_pos": (72, 820),
                },
            ]
        )
        r = _extract_with(data, backend)
        assert backend.calls == [1]
        assert r.metadata.extra["pdf:ocr_pages"] == 1
        assert r.metadata.extra["pdf:text_pages"] == 1
        assert r.content.pages is not None
        assert "Sehr geehrte" in r.content.pages[0].text
        assert "Gescannt am 12.03.2024" in r.content.pages[1].text
        assert "OCR-TEXT" in r.content.pages[1].text
        assert r.content.pages[1].text.index("Gescannt") < r.content.pages[1].text.index("OCR-TEXT")

    def test_usable_text_layer_is_kept_verbatim_even_with_large_image(self):
        """A title page with a 60% logo/scan image keeps its text layer and
        is NOT OCR'd (byte identity with plain extraction)."""
        backend = FakeOcrBackend()
        data = _pdf(
            [
                {
                    "text": "Jahresrechnung 2023\nMüller AG",
                    "image": "logo",
                    "image_frac": 0.6,
                    "text_pos": (72, 60),
                },
            ]
        )
        plain = extract(data, mime="application/pdf", use_ocr=False)
        r = _extract_with(data, backend)
        assert backend.calls == []
        assert r.metadata.extra["pdf:ocr_pages"] == 0
        assert r.content.text == plain.content.text

    def test_garbage_scanner_layer_over_raster_is_not_trusted(self):
        """An MFP's garbage text layer over a raster page: OCR'd, and the
        garbage never reaches content.text."""
        backend = FakeOcrBackend()
        garbage = "(cid:12)(cid:4)(cid:88)(cid:1) (cid:9)(cid:3)(cid:7) ��"
        data = _pdf(
            [
                {"text": garbage, "image": "Erfolgsrechnung 2023", "image_frac": 1.0},
            ]
        )
        r = _extract_with(data, backend)
        assert backend.calls == [0]
        assert "(cid:" not in r.content.text
        assert "OCR-TEXT" in r.content.text

    def test_blank_page_is_left_empty_not_ocrd(self):
        backend = FakeOcrBackend()
        data = _pdf([{"text": "Seite eins"}, {}, {"text": "Seite drei"}])
        r = _extract_with(data, backend)
        assert backend.calls == []
        assert r.content.pages is not None and r.content.pages[1].text == ""

    def test_counts_are_scalars_and_warning_carries_no_page_text(self):
        """GI-EXTRACT-04: metadata and warnings carry counts, never text."""
        backend = FakeOcrBackend(text="SECRET-AMOUNT 9'999.99")
        data = _pdf([{"image": "x", "image_frac": 1.0}])
        r = _extract_with(data, backend)
        assert isinstance(r.metadata.extra["pdf:ocr_pages"], int)
        assert all("SECRET-AMOUNT" not in w for w in r.warnings)
        assert any("OCR applied to 1 of 1 pages" in w for w in r.warnings)
