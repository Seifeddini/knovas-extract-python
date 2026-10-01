"""OCR backends: TSV parsing, backend selection order, cache keys, the CLI
child's environment, and (live, `needs_tesseract` / `needs_tesserocr`) the
accuracy floor per engine on a rasterised page of Swiss amounts.

Everything above the live section runs on every CI leg without Tesseract
(fakes + monkeypatched imports — decision D17).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from knovas_extract import OcrOptions, extract
from knovas_extract._ocr import backend as backend_mod
from knovas_extract._ocr.backend import CliBackend, select_backend
from knovas_extract._ocr.cache import (
    DictOcrCache,
    compose_key,
    samples_fingerprint,
    single_full_page_image,
    stream_fingerprint,
)
from knovas_extract._ocr.tsv import (
    IDENTITY,
    OcrPageResult,
    OcrWord,
    frame_to_page,
    mean_confidence,
    parse_tsv,
    words_to_text,
)
from knovas_extract.errors import DependencyMissingError

pytestmark = [pytest.mark.unit]

fitz = pytest.importorskip("fitz")

SWISS_AMOUNTS = [
    "1'234'567.80",
    "987'654.30",
    "456'789.00",
    "400'120.00",
    "12'345.65",
    "3'210.00",
    "99'999.95",
    "1'000.00",
    "250'000.50",
    "78'900.25",
    "5'432.10",
    "67'890.00",
    "123.45",
    "8'765'432.10",
]

# A real Tesseract 5 TSV fragment (header, page/block/par/line rows, words,
# an empty-text row Tesseract emits for layout, two blocks).
TSV_FIXTURE = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t2480\t3509\t-1\t\n"
    "2\t1\t1\t0\t0\t0\t304\t263\t555\t48\t-1\t\n"
    "3\t1\t1\t1\t0\t0\t304\t263\t555\t48\t-1\t\n"
    "4\t1\t1\t1\t1\t0\t304\t263\t555\t48\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t304\t264\t139\t47\t90.534134\tBetrag\n"
    "5\t1\t1\t1\t1\t2\t466\t264\t276\t37\t91.395874\t1'234'567.80\n"
    "5\t1\t1\t1\t1\t3\t760\t263\t99\t38\t96.495026\tCHF\n"
    "4\t1\t1\t1\t2\t0\t304\t330\t400\t40\t-1\t\n"
    "5\t1\t1\t1\t2\t1\t304\t330\t200\t40\t88.0\tTotal\n"
    "5\t1\t1\t1\t2\t2\t520\t330\t10\t40\t0.0\t \n"
    "2\t1\t2\t0\t0\t0\t304\t900\t555\t48\t-1\t\n"
    "3\t1\t2\t1\t0\t0\t304\t900\t555\t48\t-1\t\n"
    "4\t1\t2\t1\t1\t0\t304\t900\t555\t48\t-1\t\n"
    "5\t1\t2\t1\t1\t1\t304\t900\t300\t48\t75.5\tAnhang\n"
)


# ── TSV ─────────────────────────────────────────────────────────────────────


class TestParseTsv:
    def test_level5_words_with_line_heights(self):
        words = parse_tsv(TSV_FIXTURE, px_to_pt=1.0)
        assert [w.text for w in words] == ["Betrag", "1'234'567.80", "CHF", "Total", "Anhang"]
        first = words[0]
        assert (first.x0, first.y0, first.x1, first.y1) == (304.0, 264.0, 443.0, 311.0)
        assert first.conf == pytest.approx(90.534134)
        assert (first.block, first.par, first.line) == (1, 1, 1)
        assert first.line_h == 48.0  # from the enclosing level-4 row
        assert words[3].line_h == 40.0
        assert words[4].block == 2

    def test_scaling_px_to_pt(self):
        words = parse_tsv(TSV_FIXTURE, px_to_pt=72 / 300)
        assert words[0].x0 == pytest.approx(304 * 0.24, abs=0.01)
        assert words[0].line_h == pytest.approx(48 * 0.24, abs=0.01)

    def test_headerless_input_from_tesserocr_is_accepted(self):
        body = TSV_FIXTURE.split("\n", 1)[1]
        assert not body.startswith("level")
        assert len(parse_tsv(body)) == 5

    def test_malformed_rows_are_skipped_not_raised(self):
        assert parse_tsv("5\t1\tx\n5\t1\t1\t1\t1\t1\tnot\ta\tnumber\t1\t1\tword\n") == []
        assert parse_tsv("") == []

    def test_words_to_text_breaks_lines_and_blocks(self):
        text = words_to_text(parse_tsv(TSV_FIXTURE))
        assert text == "Betrag 1'234'567.80 CHF\nTotal\n\nAnhang"

    def test_mean_confidence_ignores_negative(self):
        words = parse_tsv(TSV_FIXTURE)
        assert mean_confidence(words) == pytest.approx(
            (90.534134 + 91.395874 + 96.495026 + 88.0 + 75.5) / 5, abs=0.01
        )
        assert mean_confidence([]) is None

    def test_frame_to_page_rot90_renormalises_boxes(self):
        w = OcrWord("x", 10, 20, 30, 40, 50.0, 1, 1, 1, 20)
        # x' = y, y' = W - x  (np.rot90 k=1 with W=100)
        m = (0.0, -1.0, 1.0, 0.0, 0.0, 100.0)
        out = frame_to_page([w], m)[0]
        assert (out.x0, out.y0, out.x1, out.y1) == (20.0, 70.0, 40.0, 90.0)
        assert frame_to_page([w], IDENTITY)[0] is w

    def test_page_result_json_roundtrip(self):
        words = parse_tsv(TSV_FIXTURE)
        res = OcrPageResult(text="t", words=words, mean_conf=88.3)
        back = OcrPageResult.from_json(res.to_json())
        assert back.text == "t" and back.mean_conf == 88.3 and back.words == words


# ── cache keys (decision D5) ────────────────────────────────────────────────


def _one_image_pdf(text: str = "Bilanz", *, dpi: int = 100) -> bytes:
    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 72), text, fontsize=14)
    pix = page.get_pixmap(dpi=dpi)
    dst = fitz.open()
    p = dst.new_page()
    p.insert_image(p.rect, pixmap=pix)
    buf = io.BytesIO()
    dst.save(buf)
    return buf.getvalue()


class TestCacheKey:
    def test_compose_key_changes_with_every_parameter(self):
        base = {
            "rotation": 0,
            "dpi": 300,
            "language": "deu+eng",
            "psm": 3,
            "engine": "e1",
            "preproc": "p1",
        }
        k = compose_key("c", **base)
        assert len(k) == 64
        assert compose_key("c", **base) == k
        for field_name, other in [
            ("rotation", 90),
            ("dpi", 200),
            ("language", "deu"),
            ("psm", 4),
            ("engine", "e2"),
            ("preproc", "p2"),
        ]:
            changed = dict(base)
            changed[field_name] = other
            assert compose_key("c", **changed) != k, field_name
        assert compose_key("other", **base) != k

    def test_single_full_page_image_stream_fingerprint_is_stable_and_cheap(self):
        data = _one_image_pdf()
        d1 = fitz.open(stream=data, filetype="pdf")
        d2 = fitz.open(stream=_one_image_pdf(), filetype="pdf")
        infos1 = d1[0].get_image_info(xrefs=True)
        single = single_full_page_image(d1[0], infos1)
        assert single is not None and single["xref"]
        fp1 = stream_fingerprint(d1, single["xref"])
        fp2 = stream_fingerprint(d2, d2[0].get_image_info(xrefs=True)[0]["xref"])
        assert fp1 == fp2 and fp1 is not None and len(fp1) == 64
        other = fitz.open(stream=_one_image_pdf("Erfolgsrechnung"), filetype="pdf")
        assert stream_fingerprint(other, other[0].get_image_info(xrefs=True)[0]["xref"]) != fp1

    def test_partial_or_multi_image_pages_are_not_single(self):
        doc = fitz.open()
        page = doc.new_page()
        pix = fitz.open().new_page().get_pixmap(dpi=30)
        page.insert_image(fitz.Rect(0, 0, 200, 200), pixmap=pix)
        assert single_full_page_image(page, page.get_image_info(xrefs=True)) is None
        page.insert_image(fitz.Rect(0, 300, 200, 500), pixmap=pix)
        assert single_full_page_image(page, page.get_image_info(xrefs=True)) is None

    def test_samples_fingerprint_includes_dimensions(self):
        assert samples_fingerprint(b"\xff" * 4, 2, 2) != samples_fingerprint(b"\xff" * 4, 4, 1)


@dataclass
class _CountingBackend:
    name: str = "fake"
    calls: list[int] = field(default_factory=list)

    def recognize(self, page_image: Any) -> str:
        self.calls.append(page_image.page_index)
        return "OCR-TEXT 1'234.50"


def test_injected_cache_serves_identical_page_without_calling_the_backend():
    data = _one_image_pdf()
    cache = DictOcrCache()
    b1 = _CountingBackend()
    r1 = extract(data, mime="application/pdf", ocr=OcrOptions(backend=b1, cache=cache))
    assert b1.calls == [0] and "OCR-TEXT" in r1.content.text and len(cache) == 1
    b2 = _CountingBackend()
    r2 = extract(data, mime="application/pdf", ocr=OcrOptions(backend=b2, cache=cache))
    assert (
        b2.calls == []
    ), "the single full-page image is keyed by its raw stream: no render, no OCR"
    assert r2.content.text == r1.content.text
    assert r2.metadata.extra["pdf:ocr_pages"] == 1


# ── backend selection order (tesserocr → cli → mupdf) ───────────────────────


def _block_import(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = __import__

    def _blocked(mod: str, *a: object, **kw: object) -> object:
        if mod == name or mod.startswith(name + "."):
            raise ImportError(name=name)
        return real_import(mod, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.__import__", _blocked)


@pytest.fixture
def tessdata(tmp_path):
    for lang in ("deu", "eng", "osd"):
        (tmp_path / f"{lang}.traineddata").write_bytes(b"\0")
    return str(tmp_path)


class _FakeApi:
    def __init__(self, *a: object, **kw: object) -> None:
        pass

    def SetVariable(self, *a: object) -> None:
        pass


def _fake_tesserocr() -> Any:
    return SimpleNamespace(
        __version__="9.9.9",
        tesseract_version=lambda: "tesseract 5.5.1\n leptonica-1.85.0",
        PyTessBaseAPI=_FakeApi,
        PSM=SimpleNamespace(OSD_ONLY=0),
    )


class TestSelectBackend:
    def test_auto_prefers_tesserocr(self, monkeypatch, tessdata):
        monkeypatch.setitem(sys.modules, "tesserocr", _fake_tesserocr())
        b = select_backend("auto", language="deu+eng", tessdata_dir=tessdata)
        assert b.name == "tesserocr" and b.version == "9.9.9/5.5.1"
        assert b.tessdata_dir == tessdata

    def test_auto_falls_back_to_cli_when_tesserocr_is_missing(self, monkeypatch, tessdata):
        _block_import(monkeypatch, "tesserocr")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
        monkeypatch.setattr(
            backend_mod, "_cli_list_langs", lambda b: (tessdata, frozenset({"deu", "eng"}))
        )
        monkeypatch.setattr(backend_mod, "_cli_version", lambda b: "5.3.4")
        b = select_backend("auto", language="deu+eng")
        assert b.name == "cli" and b.version == "5.3.4" and b.binary == "/usr/bin/tesseract"

    def test_auto_falls_back_to_mupdf_when_no_tesseract_binary(self, monkeypatch, tessdata):
        _block_import(monkeypatch, "tesserocr")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        b = select_backend("auto", language="deu+eng", tessdata_dir=tessdata)
        assert b.name == "mupdf" and b.tessdata_dir == tessdata

    def test_auto_raises_dependency_missing_when_nothing_is_available(self, monkeypatch, tmp_path):
        _block_import(monkeypatch, "tesserocr")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
        monkeypatch.setattr(backend_mod, "_WELL_KNOWN_TESSDATA", (str(tmp_path / "nowhere"),))
        with pytest.raises(DependencyMissingError) as exc:
            select_backend("auto", language="deu+eng")
        assert exc.value.extra == "ocr"

    def test_forced_engine_does_not_fall_back(self, monkeypatch, tessdata):
        _block_import(monkeypatch, "tesserocr")
        with pytest.raises(DependencyMissingError) as exc:
            select_backend("tesserocr", language="deu+eng", tessdata_dir=tessdata)
        assert exc.value.missing_package == "tesserocr"
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(DependencyMissingError):
            select_backend("cli", language="deu+eng")

    def test_missing_language_pack_is_a_missing_dependency(self, monkeypatch, tessdata):
        monkeypatch.setitem(sys.modules, "tesserocr", _fake_tesserocr())
        with pytest.raises(DependencyMissingError) as exc:
            select_backend("tesserocr", language="deu+fra", tessdata_dir=tessdata)
        assert "fra" in exc.value.missing_package

    def test_omp_thread_limit_is_pinned_before_tesserocr_loads(self, monkeypatch, tessdata):
        monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
        monkeypatch.setitem(sys.modules, "tesserocr", _fake_tesserocr())
        select_backend("tesserocr", language="deu", tessdata_dir=tessdata)
        assert os.environ["OMP_THREAD_LIMIT"] == "1"
        monkeypatch.setenv("OMP_THREAD_LIMIT", "4")
        backend_mod.ensure_omp_env()
        assert os.environ["OMP_THREAD_LIMIT"] == "4", "an explicit operator setting is respected"


# ── the CLI child: minimal env, no shell, stderr discarded, hard timeout ────


class TestCliInvocation:
    @pytest.fixture
    def cli(self, monkeypatch, tessdata) -> CliBackend:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
        monkeypatch.setattr(
            backend_mod, "_cli_list_langs", lambda b: (tessdata, frozenset({"deu", "eng"}))
        )
        monkeypatch.setattr(backend_mod, "_cli_version", lambda b: "5.3.4")
        return CliBackend(language="deu+eng", psm=3, page_timeout=12.5)

    def test_env_is_minimal(self, cli, monkeypatch):
        monkeypatch.setenv("HOME", "/home/secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
        env = cli.env()
        assert set(env) <= {"OMP_THREAD_LIMIT", "PATH", "TESSDATA_PREFIX", "SYSTEMROOT"}
        assert env["OMP_THREAD_LIMIT"] == "1" and "PATH" in env
        monkeypatch.setenv("TESSDATA_PREFIX", "/td")
        assert cli.env()["TESSDATA_PREFIX"] == "/td"

    def test_argv_is_a_fixed_list(self, cli):
        argv = cli.argv(dpi=300, psm=3)
        assert argv[0] == "/usr/bin/tesseract" and os.path.isabs(argv[0])
        assert argv[1:3] == ["stdin", "stdout"]
        assert argv[-1] == "tsv"
        assert "-c" in argv and "preserve_interword_spaces=1" in argv
        assert argv[argv.index("--psm") + 1] == "3" and argv[argv.index("-l") + 1] == "deu+eng"
        assert cli.argv(dpi=150, psm=0, osd=True)[-2:] == ["--psm", "0"]

    def test_subprocess_call_has_no_shell_discards_stderr_and_times_out(self, cli, monkeypatch):
        np = pytest.importorskip("numpy")
        seen: dict[str, Any] = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen.update(kw)
            return SimpleNamespace(returncode=0, stdout=TSV_FIXTURE.encode())

        monkeypatch.setattr(subprocess, "run", fake_run)
        from knovas_extract._ocr.preprocess import PageImage

        pi = PageImage(
            0, 300, 20, 10, (0, 0, 595, 842), "deu+eng", 3, gray=np.full((10, 20), 255, np.uint8)
        )
        res = cli.recognize(pi)
        assert isinstance(seen["argv"], list) and seen.get("shell", False) is False
        assert seen["stderr"] is subprocess.DEVNULL
        assert seen["timeout"] == 12.5 and seen["close_fds"] is True
        assert seen["env"] == cli.env()
        assert seen["input"].startswith(b"P5\n20 10\n255\n")
        assert [w.text for w in res.words][:3] == ["Betrag", "1'234'567.80", "CHF"]

    def test_nonzero_exit_raises_without_leaking_output(self, cli, monkeypatch):
        np = pytest.importorskip("numpy")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=3, stdout=b"SECRET page text"),
        )
        from knovas_extract._ocr.preprocess import PageImage

        pi = PageImage(0, 300, 4, 4, (0, 0, 10, 10), "deu", 3, gray=np.full((4, 4), 255, np.uint8))
        with pytest.raises(RuntimeError) as exc:
            cli.recognize(pi)
        assert "SECRET" not in str(exc.value)


# ── live: accuracy floor per engine (needs the host's tesseract) ───────────


def _amounts_pdf() -> bytes:
    src = fitz.open()
    page = src.new_page()
    for i, amount in enumerate(SWISS_AMOUNTS):
        page.insert_text((72, 90 + i * 26), f"Position {i + 1} Betrag CHF {amount}", fontsize=11)
    pix = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
    dst = fitz.open()
    p = dst.new_page(width=595, height=842)
    p.insert_image(p.rect, pixmap=pix)
    buf = io.BytesIO()
    dst.save(buf)
    return buf.getvalue()


def _amounts_found(text: str) -> int:
    return sum(1 for a in SWISS_AMOUNTS if a in text)


def _have_tesseract() -> bool:
    return shutil.which("tesseract") is not None


@pytest.mark.needs_tesseract
def test_cli_backend_recovers_swiss_amounts_at_300_dpi():
    if not _have_tesseract():
        pytest.skip("tesseract not installed")
    pytest.importorskip("numpy")
    r = extract(_amounts_pdf(), mime="application/pdf", ocr=OcrOptions(engine="cli"))
    assert r.metadata.extra["pdf:ocr_backend"] == "cli"
    assert r.metadata.extra["pdf:ocr_pages"] == 1
    assert _amounts_found(r.content.text) >= 13
    assert any(w == "pdf: OCR applied to 1 of 1 pages via cli" for w in r.warnings)
    assert isinstance(r.metadata.extra["pdf:ocr_seconds"], float)
    assert isinstance(r.metadata.extra["pdf:ocr_mean_conf"], float)


@pytest.mark.needs_tesseract
def test_mupdf_backend_at_300_dpi_recovers_most_amounts():
    if not _have_tesseract():
        pytest.skip("tesseract not installed")
    r = extract(_amounts_pdf(), mime="application/pdf", ocr=OcrOptions(engine="mupdf"))
    assert r.metadata.extra["pdf:ocr_backend"] == "mupdf"
    assert _amounts_found(r.content.text) >= 10


@pytest.mark.needs_tesserocr
def test_tesserocr_backend_recovers_swiss_amounts():
    pytest.importorskip("tesserocr")
    pytest.importorskip("numpy")
    try:
        select_backend("tesserocr", language="deu+eng")
    except DependencyMissingError as exc:
        pytest.skip(str(exc))
    r = extract(_amounts_pdf(), mime="application/pdf", ocr=OcrOptions(engine="tesserocr"))
    assert r.metadata.extra["pdf:ocr_backend"] == "tesserocr"
    assert _amounts_found(r.content.text) >= 13


@pytest.mark.needs_tesseract
def test_process_pool_produces_the_same_pages_as_the_thread_pool():
    if not _have_tesseract():
        pytest.skip("tesseract not installed")
    pytest.importorskip("numpy")
    if sys.platform == "win32":
        pytest.skip("forkserver pool is POSIX-only")
    try:
        select_backend("auto", language="deu+eng")
    except DependencyMissingError as exc:
        pytest.skip(str(exc))
    data = _amounts_pdf()
    thread = extract(data, mime="application/pdf", ocr=OcrOptions(pool="thread", workers=2))
    process = extract(data, mime="application/pdf", ocr=OcrOptions(pool="process", workers=2))
    assert process.metadata.extra["pdf:ocr_pages"] == 1
    assert process.metadata.extra["pdf:ocr_pages_failed"] == 0
    assert process.content.text == thread.content.text
