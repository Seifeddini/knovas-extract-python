"""The per-page OCR decision (GI-EXTRACT-01, decision D9 of the plan).

Alloy: ``mechanisms/client_pipeline.als::PerPageOcrDecisionMechanism``
(model ``data_plane/ocr_budget_failsoft.als``). The decision is a property
of the PAGE, never of the document: a scanned body behind a digital cover
page is OCR'd, a born-digital title page with a large logo is not.

Rules (``use_ocr="auto"``), in order:

    use_ocr=False                     → never OCR, the text layer is kept
    image cover < 30 %                → never OCR ("no_image"); whatever
                                        text layer exists is kept
    use_ocr=True                      → OCR every page carrying an image;
                                        the OCR text replaces the layer
    empty text layer over an image    → OCR; nothing to keep
    garbage text layer over an image  → OCR; the garbage is DISCARDED
                                        (cid / U+FFFD soup from an MFP)
    image cover ≥ 85 % (a full-page   → OCR; the layer is kept and the OCR
    raster) and layer < 200 chars       text is APPENDED — a scan is a scan
                                        whatever scanner stamp ("Gescannt am
                                        12.03.2024 14:33 Seite 2 von 12")
                                        sits on it
    usable text layer, image < 85 %   → keep verbatim, never OCR. Usable =
                                        not garbage AND ≥ 2 alphabetic
                                        tokens AND ≥ 20 characters (a title
                                        page with a 60 % logo stays
                                        byte-identical)
    short / sparse non-garbage layer  → OCR; the layer is kept and the OCR
    over an image ≥ 30 %                text is APPENDED

"Image" means raster coverage of the page area (image bboxes from
`page.get_image_info()` clipped to the page, summed, capped at 1.0):
`MIN_IMAGE_COVER` (30 %) makes a page an OCR candidate at all,
`FULL_PAGE_COVER` (85 %) marks a full-page scan.

Pure functions only — no numpy, no Tesseract. `page_needs_ocr` is the thin
PyMuPDF wrapper the extractor calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

UseOcrT = bool | Literal["auto"]

MIN_IMAGE_COVER = 0.30
# A raster covering this much of the page IS the page (a scan): a short text
# layer over it is a scanner stamp, never the page's text.
FULL_PAGE_COVER = 0.85
STAMP_MAX_CHARS = 200
MIN_USABLE_CHARS = 20
MIN_ALPHA_TOKENS = 2
MIN_LETTERS_PER_ALPHA_TOKEN = 2
# Garbage-layer thresholds (decision D9).
CID_FFFD_TOKEN_RATE = 0.05
GARBAGE_TOKEN_RATE = 0.15
GARBAGE_TOKEN_MIN_LEN = 4

_CID = re.compile(r"\(cid:\d+\)")
_NUMERIC_TOKEN = re.compile(r"^[\d'’.,:/%+\-()\[\]]*\d[\d'’.,:/%+\-()\[\]]*$")  # noqa: RUF001 - typographic apostrophe is a Swiss thousands separator
_VOWELS = set("aeiouyäöüéèêàâôûïîœæø")
_PLAIN_PUNCT = set(".,;:!?'\"()[]{}-–—/\\&%+*=<>#@§°_|~`´^’“”„«»…·•")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class OcrDecision:
    needs_ocr: bool
    keep_text_layer: bool
    reason: str


def _is_private_use(ch: str) -> bool:
    o = ord(ch)
    return 0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0xFFFFD or 0x100000 <= o <= 0x10FFFD


def _token_is_garbage(tok: str) -> bool:
    if "(cid:" in tok or "�" in tok or any(_is_private_use(c) for c in tok):
        return True
    if _NUMERIC_TOKEN.match(tok):
        return False  # digits are never garbage (a Bilanz page is mostly amounts)
    letters = [c for c in tok if c.isalpha()]
    digits = sum(1 for c in tok if c.isdigit())
    other = sum(1 for c in tok if not c.isalnum() and c not in _PLAIN_PUNCT and not c.isspace())
    if not letters and not digits:
        return True  # symbol soup
    if other / max(1, len(tok)) > 0.30:
        return True
    # A long run of consonants only (no vowel at all) is not a word in any
    # Latin-script language we handle — the classic broken-ToUnicode shape.
    return len(letters) >= 8 and not any(c.lower() in _VOWELS for c in letters)


def text_layer_is_garbage(text: str) -> bool:
    """True when the text layer is not trustworthy as the page's text.

    - `(cid:N)` groups, U+FFFD and private-use characters exceed 5 % of the
      token count, or
    - more than 15 % of the tokens of ≥ 4 characters are garbage tokens
      (cid / replacement / private-use characters, symbol soup, > 30 %
      non-alphanumeric non-punctuation characters, or ≥ 8 letters without a
      single vowel). Numeric tokens (amounts, dates, references) are never
      garbage.

    An empty layer is not garbage — it is "no text" and handled separately.
    """
    tokens = text.split()
    if not tokens:
        return False
    bad_marks = (
        len(_CID.findall(text)) + text.count("\ufffd") + sum(1 for c in text if _is_private_use(c))
    )
    if bad_marks / len(tokens) > CID_FFFD_TOKEN_RATE:
        return True
    long_tokens = [t for t in tokens if len(t) >= GARBAGE_TOKEN_MIN_LEN]
    if not long_tokens:
        return False
    garbage = sum(1 for t in long_tokens if _token_is_garbage(t))
    return garbage / len(long_tokens) > GARBAGE_TOKEN_RATE


def _alpha_tokens(tokens: list[str]) -> int:
    return sum(1 for t in tokens if sum(1 for c in t if c.isalpha()) >= MIN_LETTERS_PER_ALPHA_TOKEN)


def text_layer_is_usable(text: str) -> bool:
    """A non-garbage layer with ≥ 2 alphabetic tokens and ≥ 20 characters."""
    stripped = text.strip()
    if len(stripped) < MIN_USABLE_CHARS:
        return False
    if text_layer_is_garbage(stripped):
        return False
    return _alpha_tokens(stripped.split()) >= MIN_ALPHA_TOKENS


def decide_page(text: str, image_cover: float, use_ocr: UseOcrT) -> OcrDecision:
    """The pure decision for one page. See the module docstring for the rules."""
    if use_ocr is False:
        return OcrDecision(needs_ocr=False, keep_text_layer=True, reason="ocr_disabled")
    has_image = image_cover >= MIN_IMAGE_COVER
    if not has_image:
        return OcrDecision(needs_ocr=False, keep_text_layer=True, reason="no_image")
    if use_ocr is True:
        return OcrDecision(needs_ocr=True, keep_text_layer=False, reason="forced")

    stripped = text.strip()
    if not stripped:
        return OcrDecision(needs_ocr=True, keep_text_layer=False, reason="no_text")
    if text_layer_is_garbage(stripped):
        return OcrDecision(needs_ocr=True, keep_text_layer=False, reason="garbage_text")
    if image_cover >= FULL_PAGE_COVER and len(stripped) < STAMP_MAX_CHARS:
        # "Gescannt am 12.03.2024 14:33 Seite 2 von 12" passes the usable
        # test, but a full-page raster is a scan whatever stamp sits on it;
        # a real text page with a background image has far more text.
        return OcrDecision(needs_ocr=True, keep_text_layer=True, reason="full_page_scan")
    if text_layer_is_usable(stripped):
        return OcrDecision(needs_ocr=False, keep_text_layer=True, reason="usable_text")
    reason = "short_text" if len(stripped) < MIN_USABLE_CHARS else "sparse_text"
    return OcrDecision(needs_ocr=True, keep_text_layer=True, reason=reason)


def image_cover(page: Any, infos: list[dict[str, Any]] | None = None) -> float:
    """Fraction of the page area covered by raster images (clipped to the
    page rectangle, summed, capped at 1.0). 0.0 when PyMuPDF cannot
    enumerate the images."""
    try:
        rect = page.rect
        px0, py0, px1, py1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
        area = max(0.0, (px1 - px0) * (py1 - py0))
        if area <= 0:
            return 0.0
        if infos is None:
            infos = list(page.get_image_info() or [])
        covered = 0.0
        for info in infos:
            bbox = info.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x0 = max(px0, float(bbox[0]))
            y0 = max(py0, float(bbox[1]))
            x1 = min(px1, float(bbox[2]))
            y1 = min(py1, float(bbox[3]))
            if x1 > x0 and y1 > y0:
                covered += (x1 - x0) * (y1 - y0)
        return min(1.0, covered / area)
    except Exception:
        return 0.0


def page_needs_ocr(page: Any, use_ocr: UseOcrT, *, text: str | None = None) -> OcrDecision:
    """`decide_page` over a PyMuPDF page: text from `page.get_text("text")`
    (or the caller-supplied `text`), image cover from `get_image_info()`."""
    if text is None:
        try:
            text = str(page.get_text("text") or "")
        except Exception:
            text = ""
    if use_ocr is False:
        return decide_page(text, 0.0, False)
    return decide_page(text, image_cover(page), use_ocr)
