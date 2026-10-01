"""OCR backends — `IOcrBackend` implementations and `select_backend`.

Order for ``engine="auto"`` (decision D6 / §3 of the plan):

1. `TesserocrBackend` — in-process Tesseract through the `tesserocr` wheel
   (MIT; the manylinux wheel bundles Tesseract 5.5 + Leptonica). One
   persistent `PyTessBaseAPI` per worker THREAD (thread-local), raw gray
   bytes via `SetImageBytes`, `SetSourceResolution`, `Recognize(timeout)`,
   `GetTSVText`. No process spawn, no model reload per page.
2. `CliBackend` — the system ``tesseract`` binary: ``tesseract stdin stdout
   --dpi N -l L --psm P -c preserve_interword_spaces=1 tsv`` with a PGM on
   stdin. Minimal explicit environment (``OMP_THREAD_LIMIT=1``,
   ``TESSDATA_PREFIX`` only when set, ``PATH``), ``stderr=DEVNULL`` (never
   captured: Tesseract prints page content in some diagnostics), a hard
   ``timeout`` that kills the child, ``close_fds=True``, no shell.
3. `MupdfBackend` — PyMuPDF's built-in OCR (`page.get_textpage_ocr(
   language, dpi=300, full=True)`); needs tessdata on the host but no
   numpy. The weakest engine (drops ruled tables on scans — findings F6)
   but it fixes the 72-dpi defect of 0.3.x on its own.

`OMP_THREAD_LIMIT=1` is set (``setdefault``) before `tesserocr` is first
imported: Tesseract's OpenMP threads would otherwise fight the worker pool
(10 pages/min with OpenMP vs 88 with one thread per worker, measured).

Every backend honours `PageImage.psm` so the pipeline can retry a page with
psm 4. Tesseract backends return words in frame pixels and go through
`finish_words` (rule-word filter, frame → page mapping, text rendering).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - fixed binary, argv list, no shell (see CliBackend)
import sys
import threading
from collections.abc import Iterable
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar

from knovas_extract._ocr.tsv import (
    OcrPageResult,
    OcrWord,
    frame_to_page,
    mean_confidence,
    parse_tsv,
    words_to_text,
)
from knovas_extract.errors import DependencyMissingError

if TYPE_CHECKING:
    from knovas_extract._ocr.preprocess import PageImage
    from knovas_extract.interfaces import IOcrBackend

OCR_EXTRA = "ocr"
TESSERACT_SYSTEM_PACKAGE = (
    "tesseract-ocr (system package; install e.g. apt install tesseract-ocr tesseract-ocr-deu)"
)
_WELL_KNOWN_TESSDATA = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/opt/homebrew/share/tessdata",
    "/opt/local/share/tessdata",
)
_LIST_LANGS_PATH = re.compile(r'List of available languages in "(.+?)"')
_OSD_ROTATE = re.compile(r"^Rotate:\s*(\d+)", re.MULTILINE)
_OSD_CONF = re.compile(r"^Orientation confidence:\s*([\d.]+)", re.MULTILINE)


def ensure_omp_env() -> None:
    """Pin Tesseract/OpenMP to one thread per worker (must run before the
    native library loads — i.e. before `import tesserocr`)."""
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def split_languages(language: str) -> list[str]:
    return [lang for lang in language.split("+") if lang]


def tessdata_has_languages(path: str, languages: Iterable[str]) -> bool:
    try:
        return all(os.path.isfile(os.path.join(path, f"{lang}.traineddata")) for lang in languages)
    except (TypeError, ValueError, OSError):
        return False


def _numpy_stack_available() -> bool:
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_tessdata_dir(explicit: str | None, languages: Iterable[str]) -> str | None:
    """First folder holding every ``<lang>.traineddata``: the explicit
    option, ``$TESSDATA_PREFIX`` (itself or its ``tessdata`` child),
    well-known system folders, then the folder a ``tesseract`` binary
    reports (``--list-langs``, no shell)."""
    langs = list(languages)
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        candidates += [env, os.path.join(env, "tessdata")]
    candidates += list(_WELL_KNOWN_TESSDATA)
    for cand in candidates:
        if tessdata_has_languages(cand, langs):
            return cand
    binary = shutil.which("tesseract")
    if binary:
        reported = _cli_list_langs(binary)
        if reported is not None and tessdata_has_languages(reported[0], langs):
            return reported[0]
    return None


def _minimal_env() -> dict[str, str]:
    """The ONLY environment the tesseract child sees."""
    env = {
        "OMP_THREAD_LIMIT": "1",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    tess = os.environ.get("TESSDATA_PREFIX")
    if tess:
        env["TESSDATA_PREFIX"] = tess
    if sys.platform == "win32" and os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]  # DLL loading needs it on Windows
    return env


@lru_cache(maxsize=8)
def _cli_list_langs(binary: str) -> tuple[str, frozenset[str]] | None:
    """``tesseract --list-langs`` → (tessdata folder, languages); cached."""
    try:
        cp = subprocess.run(  # noqa: S603 # nosec B603 - absolute path from shutil.which, fixed argv, no shell
            [binary, "--list-langs"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_minimal_env(),
            timeout=30,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = cp.stdout.decode("utf-8", errors="replace")
    m = _LIST_LANGS_PATH.search(out)
    if not m:
        return None
    langs = frozenset(
        line.strip() for line in out.splitlines() if line.strip() and " " not in line.strip()
    )
    return m.group(1).rstrip("/\\") or m.group(1), langs


@lru_cache(maxsize=8)
def _cli_version(binary: str) -> str:
    try:
        cp = subprocess.run(  # noqa: S603 # nosec B603 - absolute path from shutil.which, fixed argv, no shell
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_minimal_env(),
            timeout=30,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first = cp.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not first:
        return "unknown"
    parts = first[0].split()
    return parts[1] if len(parts) >= 2 and parts[0] == "tesseract" else first[0][:40]


def finish_words(words_px: list[OcrWord], pi: PageImage) -> OcrPageResult:
    """Rule-word filter (when the page carries a rule mask), frame → page
    mapping and text rendering for words returned in frame pixels."""
    if pi.rules is not None:
        from knovas_extract._ocr.preprocess import filter_rule_words

        words_px, _dropped = filter_rule_words(words_px, pi.rules)
    words = frame_to_page(words_px, pi.frame_to_page)
    return OcrPageResult(text=words_to_text(words), words=words, mean_conf=mean_confidence(words))


class TesserocrBackend:
    """In-process Tesseract (``tesserocr``), one API object per thread."""

    name: ClassVar[str] = "tesserocr"
    needs_image: ClassVar[bool] = True

    def __init__(
        self,
        *,
        language: str,
        psm: int = 3,
        tessdata_dir: str | None = None,
        page_timeout: float = 60.0,
    ) -> None:
        ensure_omp_env()
        if not _numpy_stack_available():
            raise DependencyMissingError(OCR_EXTRA, "numpy")
        try:
            import tesserocr
        except ImportError as exc:
            raise DependencyMissingError(OCR_EXTRA, "tesserocr") from exc
        self._tesserocr = tesserocr
        self.language = language
        self.psm = psm
        self.page_timeout = page_timeout
        langs = split_languages(language)
        tessdata = resolve_tessdata_dir(tessdata_dir, langs)
        if tessdata is None:
            raise DependencyMissingError(OCR_EXTRA, f"tessdata for {'+'.join(langs)}")
        self.tessdata_dir = tessdata
        self._local = threading.local()
        tess_version = str(tesserocr.tesseract_version()).strip().splitlines()[0].split()
        self.version = f"{tesserocr.__version__}/{tess_version[-1] if tess_version else '?'}"

    def _api(self, psm: int) -> Any:
        apis: dict[int, Any] | None = getattr(self._local, "apis", None)
        if apis is None:
            apis = {}
            self._local.apis = apis
        api = apis.get(psm)
        if api is None:
            api = self._tesserocr.PyTessBaseAPI(path=self.tessdata_dir, lang=self.language, psm=psm)
            api.SetVariable("preserve_interword_spaces", "1")
            apis[psm] = api
        return api

    def recognize(self, page_image: PageImage) -> OcrPageResult:
        import numpy as np

        arr = page_image.gray
        if arr is None:
            raise RuntimeError("tesserocr backend needs a rendered page image")
        h, w = arr.shape
        api = self._api(page_image.psm)
        api.SetImageBytes(np.ascontiguousarray(arr).tobytes(), w, h, 1, w)
        api.SetSourceResolution(int(page_image.dpi))
        api.Recognize(int(self.page_timeout * 1000))
        tsv = api.GetTSVText(0)
        api.Clear()
        return finish_words(parse_tsv(tsv, 1.0), page_image)

    def detect_orientation(self, gray: Any, dpi: int) -> int | None:
        """Degrees CLOCKWISE to rotate the page upright (0/90/180/270), via
        Tesseract's OSD on a half-size copy; None when undecided."""
        if not tessdata_has_languages(self.tessdata_dir, ["osd"]):
            return None
        from knovas_extract._ocr.preprocess import _thumbnail

        osd = getattr(self._local, "osd", None)
        if osd is None:
            osd = self._tesserocr.PyTessBaseAPI(
                path=self.tessdata_dir, lang="osd", psm=self._tesserocr.PSM.OSD_ONLY
            )
            self._local.osd = osd
        small = _thumbnail(gray, max(gray.shape) // 2)
        h, w = small.shape
        osd.SetImageBytes(small.tobytes(), w, h, 1, w)
        osd.SetSourceResolution(max(70, int(dpi // 2)))
        try:
            info = osd.DetectOrientationScript()
        except Exception:
            return None
        finally:
            osd.Clear()
        if not info:
            return None
        deg = int(info.get("orient_deg", 0) or 0)
        conf = float(info.get("orient_conf", 0.0) or 0.0)
        if conf < 2.0:
            return None
        return (360 - deg) % 360


class CliBackend:
    """The system ``tesseract`` binary, one short-lived child per page."""

    name: ClassVar[str] = "cli"
    needs_image: ClassVar[bool] = True

    def __init__(
        self,
        *,
        language: str,
        psm: int = 3,
        tessdata_dir: str | None = None,
        page_timeout: float = 60.0,
        binary: str | None = None,
    ) -> None:
        if not _numpy_stack_available():
            raise DependencyMissingError(OCR_EXTRA, "numpy")
        resolved = binary or shutil.which("tesseract")
        if not resolved:
            raise DependencyMissingError(OCR_EXTRA, TESSERACT_SYSTEM_PACKAGE)
        self.binary = resolved
        self.language = language
        self.psm = psm
        self.page_timeout = page_timeout
        langs = split_languages(language)
        self.tessdata_dir: str | None = None
        if tessdata_dir:
            if not tessdata_has_languages(tessdata_dir, langs):
                raise DependencyMissingError(OCR_EXTRA, f"tessdata for {'+'.join(langs)}")
            self.tessdata_dir = tessdata_dir
        else:
            listed = _cli_list_langs(self.binary)
            if listed is None or not set(langs) <= listed[1]:
                raise DependencyMissingError(OCR_EXTRA, f"tessdata for {'+'.join(langs)}")
            self._listed = listed
        self.version = _cli_version(self.binary)

    def env(self) -> dict[str, str]:
        return _minimal_env()

    def argv(
        self, *, dpi: int, psm: int, language: str | None = None, osd: bool = False
    ) -> list[str]:
        cmd = [self.binary, "stdin", "stdout", "--dpi", str(int(dpi))]
        if self.tessdata_dir:
            cmd += ["--tessdata-dir", self.tessdata_dir]
        if osd:
            return [*cmd, "--psm", "0"]
        cmd += ["-l", language or self.language, "--psm", str(int(psm))]
        cmd += ["-c", "preserve_interword_spaces=1", "tsv"]
        return cmd

    def _run(self, argv: list[str], pgm: bytes) -> bytes:
        # The binary is an absolute path resolved by shutil.which, argv is a
        # fixed list built here (no user-controlled elements), no shell.
        cp = subprocess.run(  # noqa: S603 # nosec B603 B607
            argv,
            input=pgm,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.env(),
            timeout=self.page_timeout,
            close_fds=True,
            check=False,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"tesseract exited with status {cp.returncode}")
        return cp.stdout

    def recognize(self, page_image: PageImage) -> OcrPageResult:
        from knovas_extract._ocr.preprocess import pgm_bytes

        arr = page_image.gray
        if arr is None:
            raise RuntimeError("cli backend needs a rendered page image")
        out = self._run(self.argv(dpi=page_image.dpi, psm=page_image.psm), pgm_bytes(arr))
        return finish_words(parse_tsv(out.decode("utf-8", errors="replace"), 1.0), page_image)

    def detect_orientation(self, gray: Any, dpi: int) -> int | None:
        from knovas_extract._ocr.preprocess import _thumbnail, pgm_bytes

        small = _thumbnail(gray, max(gray.shape) // 2)
        try:
            out = self._run(self.argv(dpi=max(70, dpi // 2), psm=0, osd=True), pgm_bytes(small))
        except Exception:
            return None
        text = out.decode("utf-8", errors="replace")
        m_rot, m_conf = _OSD_ROTATE.search(text), _OSD_CONF.search(text)
        if not m_rot or not m_conf or float(m_conf.group(1)) < 2.0:
            return None
        return int(m_rot.group(1)) % 360


class MupdfBackend:
    """PyMuPDF's built-in Tesseract OCR; renders the page itself at 300 dpi
    with `full=True`. Must run on the thread that owns the document."""

    name: ClassVar[str] = "mupdf"
    needs_image: ClassVar[bool] = False

    def __init__(
        self,
        *,
        language: str,
        tessdata_dir: str | None = None,
        dpi: int = 300,
    ) -> None:
        try:
            import fitz
        except ImportError as exc:
            raise DependencyMissingError("pdf", "pymupdf") from exc
        langs = split_languages(language)
        tessdata = resolve_tessdata_dir(tessdata_dir, langs)
        if tessdata is None:
            raise DependencyMissingError(OCR_EXTRA, f"tessdata for {'+'.join(langs)}")
        self.tessdata_dir = tessdata
        self.language = language
        self.dpi = dpi
        version = getattr(fitz, "version", None)
        self.version = str(version[0]) if version else getattr(fitz, "VersionBind", "?")

    def recognize(self, page_image: PageImage) -> OcrPageResult:
        page = page_image.page
        if page is None:
            raise RuntimeError("mupdf backend needs the PyMuPDF page object")
        textpage = page.get_textpage_ocr(
            language=self.language, dpi=self.dpi, full=True, tessdata=self.tessdata_dir
        )
        text = str(page.get_text("text", textpage=textpage) or "")
        words: list[OcrWord] = []
        try:
            for w in page.get_text("words", textpage=textpage):
                x0, y0, x1, y1 = (float(v) for v in w[:4])
                words.append(
                    OcrWord(
                        text=str(w[4]),
                        x0=round(x0, 2),
                        y0=round(y0, 2),
                        x1=round(x1, 2),
                        y1=round(y1, 2),
                        conf=-1.0,
                        block=int(w[5]),
                        par=0,
                        line=int(w[6]),
                        line_h=round(y1 - y0, 2),
                    )
                )
        except Exception:
            words = []
        return OcrPageResult(text=text, words=words, mean_conf=None)


def select_backend(
    engine: str = "auto",
    *,
    language: str = "deu+eng",
    psm: int = 3,
    tessdata_dir: str | None = None,
    page_timeout: float = 60.0,
) -> IOcrBackend:
    """Build the requested backend, or for ``"auto"`` the first available
    one in the order tesserocr → cli → mupdf. Raises `DependencyMissingError`
    when nothing (or the forced engine) is available."""
    if engine == "tesserocr":
        return TesserocrBackend(
            language=language, psm=psm, tessdata_dir=tessdata_dir, page_timeout=page_timeout
        )
    if engine == "cli":
        return CliBackend(
            language=language, psm=psm, tessdata_dir=tessdata_dir, page_timeout=page_timeout
        )
    if engine == "mupdf":
        return MupdfBackend(language=language, tessdata_dir=tessdata_dir)
    if engine != "auto":
        raise ValueError(f"unknown OCR engine {engine!r}")
    last: DependencyMissingError | None = None
    for candidate in ("tesserocr", "cli", "mupdf"):
        try:
            return select_backend(
                candidate,
                language=language,
                psm=psm,
                tessdata_dir=tessdata_dir,
                page_timeout=page_timeout,
            )
        except DependencyMissingError as exc:
            last = exc
    raise DependencyMissingError(
        OCR_EXTRA, f"an OCR engine: tesserocr, {TESSERACT_SYSTEM_PACKAGE}, or tessdata for PyMuPDF"
    ) from last
