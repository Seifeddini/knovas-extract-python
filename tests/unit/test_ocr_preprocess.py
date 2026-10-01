"""OCR render / preprocess primitives, the pixel-bomb guard, fail-soft
backend handling and the decision wrapper on real PyMuPDF pages.

numpy + Pillow tests skip when the `[ocr]` extra is absent; the pixel-bomb
and fail-soft tests run everywhere (fake backend, no Tesseract).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from knovas_extract import Limits, OcrOptions, extract
from knovas_extract._ocr import pipeline as pipeline_mod
from knovas_extract._ocr.decision import image_cover, page_needs_ocr, text_layer_is_garbage
from knovas_extract._ocr.tsv import OcrPageResult, OcrWord, mat_apply
from knovas_extract.errors import DependencyMissingError

pytestmark = [pytest.mark.unit]

fitz = pytest.importorskip("fitz")


@dataclass
class FakeOcrBackend:
    text: str = "OCR-TEXT 1'234.50"
    name: str = "fake"
    calls: list[int] = field(default_factory=list)

    def recognize(self, page_image: Any) -> str:
        self.calls.append(page_image.page_index)
        return self.text


def _raster(text: str, *, dpi: int = 100) -> Any:
    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 72), text, fontsize=14)
    return page.get_pixmap(dpi=dpi)


def _pdf(pages: list[dict[str, Any]]) -> bytes:
    doc = fitz.open()
    for spec in pages:
        page = doc.new_page()
        if spec.get("image"):
            frac = spec.get("image_frac", 1.0)
            r = page.rect
            page.insert_image(
                fitz.Rect(r.x0, r.y0 + r.height * (1 - frac), r.x1, r.y1),
                pixmap=_raster(spec["image"]),
                keep_proportion=False,
            )
        if spec.get("text"):
            page.insert_text((72, 72), spec["text"], fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── pixel bomb (Limits.max_ocr_image_megapixels) ────────────────────────────


def _pixel_bomb_pdf() -> bytes:
    """A text cover page + a page whose only image is 12000x12000 1-bit
    Flate: ~18 MB decoded, < 50 KB on disk."""
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("1", (12000, 12000), 1)
    png = io.BytesIO()
    im.save(png, format="PNG")
    doc = fitz.open()
    cover = doc.new_page()
    cover.insert_text((72, 72), "Begleitschreiben zum Jahresabschluss 2023", fontsize=12)
    bomb = doc.new_page()
    bomb.insert_image(bomb.rect, stream=png.getvalue())
    buf = io.BytesIO()
    doc.save(buf, deflate=True)
    return buf.getvalue()


def test_pixel_bomb_page_is_never_rendered_and_text_pages_are_extracted(monkeypatch):
    data = _pixel_bomb_pdf()
    assert len(data) < 50 * 1024, f"fixture must stay small on disk, got {len(data)} bytes"
    doc = fitz.open(stream=data, filetype="pdf")
    info = doc[1].get_image_info()[0]
    assert (info["width"], info["height"], info["bpc"]) == (12000, 12000, 1)
    doc.close()

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the pixel-bomb page must never be rendered")

    monkeypatch.setattr(fitz.Page, "get_pixmap", boom)
    backend = FakeOcrBackend()
    r = extract(data, mime="application/pdf", ocr=OcrOptions(backend=backend))
    assert backend.calls == []
    assert r.content.pages is not None and "Begleitschreiben" in r.content.pages[0].text
    assert r.content.pages[1].text == ""
    assert r.metadata.extra["pdf:ocr_pages"] == 0
    assert r.metadata.extra["pdf:ocr_pages_skipped"] == 1
    assert any("max_ocr_image_megapixels" in w for w in r.warnings)
    assert all("Begleitschreiben" not in w for w in r.warnings)


def test_render_megapixel_cap_also_covers_huge_pages(monkeypatch):
    """A tiny image on an A0-sized page would still render to > 40 MP at 300 dpi."""
    doc = fitz.open()
    page = doc.new_page(width=2384, height=3370)  # A0 in points
    page.insert_image(page.rect, pixmap=_raster("x", dpi=20))
    buf = io.BytesIO()
    doc.save(buf)
    backend = FakeOcrBackend()
    r = extract(buf.getvalue(), mime="application/pdf", ocr=OcrOptions(backend=backend, dpi=300))
    assert backend.calls == [] and r.metadata.extra["pdf:ocr_pages_skipped"] == 1
    r2 = extract(
        buf.getvalue(),
        mime="application/pdf",
        ocr=OcrOptions(backend=backend, dpi=300),
        limits=Limits(max_ocr_image_megapixels=200),
    )
    assert backend.calls == [0] and r2.metadata.extra["pdf:ocr_pages"] == 1


# ── fail-soft backend handling (decision D10) ───────────────────────────────


def _no_backend(*a: object, **k: object) -> Any:
    raise DependencyMissingError("ocr", "tesserocr")


def test_auto_with_missing_backend_keeps_text_layers_and_counts(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "select_backend", _no_backend)
    data = _pdf(
        [{"text": "Digital cover page with enough words"}, {"image": "scan", "image_frac": 1.0}]
    )
    r = extract(data, mime="application/pdf", use_ocr="auto")
    assert r.content.pages is not None and "Digital cover" in r.content.pages[0].text
    assert r.content.pages[1].text == ""
    assert r.metadata.extra["pdf:ocr_backend"] == "none"
    assert r.metadata.extra["pdf:ocr_pages"] == 0 and r.metadata.extra["pdf:ocr_pages_skipped"] == 1
    assert sum("OCR backend unavailable" in w for w in r.warnings) == 1


def test_auto_with_missing_backend_raises_when_document_would_be_empty(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "select_backend", _no_backend)
    with pytest.raises(DependencyMissingError):
        extract(_pdf([{"image": "scan"}]), mime="application/pdf", use_ocr="auto")


def test_forced_engine_or_forced_ocr_raises_when_backend_is_missing(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "select_backend", _no_backend)
    data = _pdf([{"text": "Digital cover page with enough words"}, {"image": "scan"}])
    with pytest.raises(DependencyMissingError):
        extract(data, mime="application/pdf", ocr=OcrOptions(engine="cli"))
    with pytest.raises(DependencyMissingError):
        extract(data, mime="application/pdf", use_ocr=True)


def test_use_ocr_true_ocrs_every_image_page_and_replaces_its_layer():
    backend = FakeOcrBackend()
    data = _pdf(
        [
            {"text": "Digital title page without any image"},
            {"text": "Jahresrechnung 2023 Müller AG", "image": "logo", "image_frac": 0.6},
        ]
    )
    r = extract(data, mime="application/pdf", use_ocr=True, ocr=OcrOptions(backend=backend))
    assert backend.calls == [1]
    assert r.content.pages is not None
    assert "Digital title" in r.content.pages[0].text
    assert r.content.pages[1].text == "OCR-TEXT 1'234.50"


def test_short_stamp_over_scan_keeps_stamp_and_appends_ocr_text():
    backend = FakeOcrBackend(text="Bilanz per 31.12.2023")
    data = _pdf([{"text": "Scan 12.03.2024", "image": "body", "image_frac": 1.0}])
    r = extract(data, mime="application/pdf", ocr=OcrOptions(backend=backend))
    assert backend.calls == [0]
    assert r.content.pages is not None
    assert r.content.pages[0].text == "Scan 12.03.2024\n\nBilanz per 31.12.2023"


def test_born_digital_default_output_has_no_ocr_keys_and_no_ocr_warning():
    data = _pdf([{"text": "Digital page one with words"}, {"text": "Digital page two with words"}])
    r = extract(data, mime="application/pdf")
    assert not any(k.startswith("pdf:ocr") for k in r.metadata.extra)
    assert "pdf:text_pages" not in r.metadata.extra
    assert not any("OCR" in w for w in r.warnings)


def test_rich_backend_result_feeds_mean_confidence_and_words():
    class RichBackend:
        name = "rich"
        version = "1.0"

        def recognize(self, page_image: Any) -> OcrPageResult:
            words = [
                OcrWord("Total", 10, 10, 50, 20, 91.0, 1, 1, 1, 10),
                OcrWord("1'000.00", 60, 10, 100, 20, 87.0, 1, 1, 1, 10),
            ]
            return OcrPageResult(text="Total 1'000.00", words=words, mean_conf=89.0)

    r = extract(
        _pdf([{"image": "scan"}]), mime="application/pdf", ocr=OcrOptions(backend=RichBackend())
    )
    assert r.content.text == "Total 1'000.00"
    assert r.metadata.extra["pdf:ocr_mean_conf"] == 89.0
    assert r.metadata.extra["pdf:ocr_backend"] == "rich"
    assert r.metadata.extra["pdf:ocr_backend_version"] == "1.0"


def test_backend_exception_is_a_failed_page_not_a_document_failure():
    class Broken:
        name = "broken"

        def recognize(self, page_image: Any) -> str:
            raise MemoryError("decoder blew up on Lohnausweis Meier")

    data = _pdf([{"text": "Digital cover page with enough words"}, {"image": "scan"}])
    r = extract(data, mime="application/pdf", ocr=OcrOptions(backend=Broken()))
    assert r.metadata.extra["pdf:ocr_pages_failed"] == 1 and r.metadata.extra["pdf:ocr_pages"] == 0
    assert r.content.pages is not None and r.content.pages[1].text == ""
    assert any("failed OCR" in w for w in r.warnings)
    assert all("Meier" not in w for w in r.warnings)


def test_max_ocr_pages_budget_is_fail_soft_through_extract():
    backend = FakeOcrBackend()
    data = _pdf([{"image": f"scan {i}"} for i in range(4)])
    r = extract(
        data,
        mime="application/pdf",
        ocr=OcrOptions(backend=backend),
        limits=Limits(max_ocr_pages=2),
    )
    assert sorted(backend.calls) == [0, 1]
    assert r.metadata.extra["pdf:ocr_pages"] == 2 and r.metadata.extra["pdf:ocr_pages_skipped"] == 2
    assert any(w == "pdf: 2 pages skipped: OCR budget exhausted" for w in r.warnings)
    assert r.content.pages is not None and [bool(p.text) for p in r.content.pages] == [
        True,
        True,
        False,
        False,
    ]


def test_limits_defaults():
    lim = Limits()
    assert (lim.max_ocr_pages, lim.ocr_time_budget_seconds, lim.ocr_page_timeout_seconds) == (
        500,
        240.0,
        60.0,
    )
    assert (lim.max_ocr_workers, lim.max_ocr_image_megapixels) == (8, 40)


def test_ocr_options_validation():
    with pytest.raises(ValueError):
        OcrOptions(engine="rapidocr")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OcrOptions(dpi=5)
    with pytest.raises(ValueError):
        OcrOptions(workers=0)
    with pytest.raises(ValueError):
        OcrOptions(language="deu eng")


def test_cli_flags_build_ocr_options(tmp_path, capsys):
    from knovas_extract.cli import main

    path = tmp_path / "doc.pdf"
    path.write_bytes(_pdf([{"text": "Digital page with words in it"}]))
    assert (
        main(
            [
                str(path),
                "--ocr-engine",
                "mupdf",
                "--ocr-dpi",
                "200",
                "--ocr-workers",
                "1",
                "--ocr-psm",
                "4",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["metadata"]["extra"]["pdf:ocr_pages"] == 0
    assert main([str(path), "--ocr-dpi", "1"]) == 2


# ── decision wrapper on real pages ──────────────────────────────────────────


def test_image_cover_and_page_needs_ocr_on_real_pages():
    doc = fitz.open(
        stream=_pdf([{"image": "x", "image_frac": 0.5}, {"text": "Hello world page"}, {}]),
        filetype="pdf",
    )
    assert 0.45 < image_cover(doc[0]) < 0.55
    assert page_needs_ocr(doc[0], "auto").needs_ocr is True
    assert page_needs_ocr(doc[0], False).needs_ocr is False
    assert page_needs_ocr(doc[1], "auto").reason == "no_image"
    assert page_needs_ocr(doc[2], "auto").reason == "no_image"


def test_garbage_detection_edges():
    assert text_layer_is_garbage("") is False
    assert text_layer_is_garbage("\ue000\ue001\ue002 \ue003\ue004") is True  # private-use glyphs
    assert text_layer_is_garbage("xkqzrtplm wvbnmgt pqrtzx") is True  # vowel-less runs
    assert text_layer_is_garbage("Rechnung Nr. 2023-0815 vom 12.03.2024 CHF 1'250.00") is False


# ── numpy / Pillow primitives ───────────────────────────────────────────────

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
from knovas_extract._ocr import preprocess as P  # noqa: E402


def _page_with_rules() -> Any:
    doc = fitz.open()
    page = doc.new_page()
    for i in range(12):
        page.insert_text(
            (72, 100 + i * 22), f"Flüssige Mittel {i}   1'234'567.80   987'654.30", fontsize=11
        )
    page.draw_line((72, 380), (520, 380), width=1.0)
    page.draw_line((72, 420), (520, 420), width=0.7)
    page.draw_line((300, 60), (300, 500), width=0.8)
    page.insert_text((72, 460), "Total " + "." * 60 + " 99'999.00", fontsize=11)
    return page


@pytest.fixture(scope="module")
def gray() -> Any:
    page = _page_with_rules()
    samples, w, h = P.render_gray(page, 300)
    return P.to_array(samples, w, h)


def test_native_dpi_and_render_dpi_never_upsample():
    doc = fitz.open()
    for dpi in (150, 600):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=_raster("x", dpi=dpi))
    infos150 = P.image_infos(doc[0])
    infos600 = P.image_infos(doc[1])
    assert abs(P.native_dpi(doc[0], infos150) - 150) <= 2
    assert abs(P.native_dpi(doc[1], infos600) - 600) <= 4
    assert P.choose_render_dpi(doc[0], infos150, None) == 150
    assert P.choose_render_dpi(doc[1], infos600, None) == 300
    assert P.choose_render_dpi(doc[1], infos600, 200) == 200
    assert P.choose_render_dpi(doc.new_page(), [], None) == 300


def test_render_gray_returns_raw_samples(gray):
    assert gray.dtype == np.uint8 and gray.ndim == 2
    assert abs(gray.shape[1] - 595 * 300 / 72) <= 1
    assert P.pgm_bytes(gray).startswith(b"P5\n%d %d\n255\n" % (gray.shape[1], gray.shape[0]))


def test_rot90_matrices_map_corners():
    arr = np.zeros((30, 20), np.uint8)
    for k in range(4):
        out, m = P.rot90k(arr, k)
        xs, ys = zip(
            *(mat_apply(m, x, y) for x, y in ((0, 0), (20, 0), (20, 30), (0, 30))), strict=True
        )
        assert (min(xs), min(ys), max(xs), max(ys)) == (0, 0, out.shape[1], out.shape[0])


def test_rotate_arr_matrix_tracks_a_marked_pixel():
    arr = np.full((400, 300), 255, np.uint8)
    arr[80, 220] = 0
    out, m = P.rotate_arr(arr, 5.0)
    ys, xs = np.where(out < 128)
    px, py = mat_apply(m, 220, 80)
    assert abs(xs.mean() - px) <= 1.5 and abs(ys.mean() - py) <= 1.5


def test_sideways_ratio_distinguishes_rotated_pages(gray):
    assert P.sideways_ratio(gray) >= 1.0
    rotated, _ = P.rot90k(gray, 1)
    assert P.sideways_ratio(rotated) < 1.0
    assert P.sideways_ratio(np.full((500, 400), 255, np.uint8)) == 10.0


def test_estimate_skew_within_two_tenths_of_a_degree(gray):
    assert P.estimate_skew(gray) == 0.0
    for angle in (1.5, -1.5):
        skewed, _ = P.rotate_arr(gray, -angle)
        assert abs(P.estimate_skew(skewed) - angle) <= 0.2
    assert P.estimate_skew(np.full((500, 400), 255, np.uint8)) == 0.0


def test_rules_are_detected_and_rule_words_dropped(gray):
    rules = P.detect_rules(gray, 300)
    assert rules.h_rules >= 3 and rules.v_rules >= 1
    px = 300 / 72
    on_rule = OcrWord("_____", 100 * px, 379 * px, 300 * px, 381 * px, 50, 1, 1, 1, 2)
    leader = OcrWord(".....", 110 * px, 452 * px, 400 * px, 461 * px, 50, 1, 1, 2, 10)
    real = OcrWord("Mittel", 72 * px, 92 * px, 140 * px, 102 * px, 90, 1, 1, 3, 10)
    kept, dropped = P.filter_rule_words([on_rule, leader, real], rules)
    assert [w.text for w in kept] == ["Mittel"] and dropped == 2
    blank = P.detect_rules(np.full((600, 400), 255, np.uint8), 300)
    assert blank.h_rules == 0 and blank.v_rules == 0


def test_preprocess_sets_frame_to_page_for_a_rotated_page(gray):
    rotated, _ = P.rot90k(gray, 1)
    pi = P.PageImage(
        0, 300, rotated.shape[1], rotated.shape[0], (0, 0, 842, 595), "deu", 3, gray=rotated.copy()
    )
    P.preprocess(pi, osd=lambda a, d: 270, deskew=False, rules=False)
    assert pi.rot90 == 270 and pi.gray.shape == gray.shape
    x, y = mat_apply(pi.frame_to_page, 0, 0)
    assert abs(x - 842) < 1 and abs(y) < 1
    x, y = mat_apply(pi.frame_to_page, pi.width, pi.height)
    assert abs(x) < 1 and abs(y - 595) < 1


def test_preprocess_of_a_clean_page_is_identity(gray):
    pi = P.PageImage(
        0, 300, gray.shape[1], gray.shape[0], (0, 0, 595, 842), "deu", 3, gray=gray.copy()
    )
    P.preprocess(pi)
    assert pi.rot90 == 0 and pi.skew == 0.0 and pi.rules is not None
    assert pi.frame_to_page == pytest.approx((0.24, 0, 0, 0.24, 0, 0))
