"""OCR result cache — key composition (decision D5) and the default in-memory cache.

The library never writes files: the default `DictOcrCache` lives for one
`extract()` call. A caller (the RemoteController) injects its own
`IOcrCache` (SQLite, LRU, purged per document — GI-EXTRACT-04) through
`OcrOptions(cache=...)`.

Key = sha256 over
    content fingerprint
      | page is ONE full-page image  → sha256 of the UNDECODED image stream
        (`doc.xref_stream_raw(xref)`) plus the image dictionary's
        /Filter /Width /Height /BitsPerComponent /Decode — computed before
        any render, so a hit skips the render entirely. `extract_image()`
        is deliberately NOT used: it re-encodes non-JPEG streams to PNG,
        which costs as much as the OCR itself.
      | otherwise → sha256 of the rendered gray samples
    + page /Rotate + render dpi + language + psm + engine name/version
    + preprocessing fingerprint (which steps are enabled, their version)

so an engine, language or preprocessing change never serves stale text.
"""

from __future__ import annotations

import hashlib
from typing import Any

_FULL_PAGE_COVER = 0.90
_IMAGE_DICT_KEYS = ("Filter", "Width", "Height", "BitsPerComponent", "Decode")


class DictOcrCache:
    """Per-document in-memory cache (the default)."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def put(self, key: str, value: str) -> None:
        self._d[key] = value

    def __len__(self) -> int:
        return len(self._d)


def single_full_page_image(page: Any, infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one image dict when the page consists of exactly one raster
    covering ≥ 90 % of the page and nothing else is drawn on it."""
    if len(infos) != 1:
        return None
    info = infos[0]
    if info.get("xref") in (None, 0):
        return None  # inline image: no stream to hash
    try:
        rect = page.rect
        area = float(rect.width) * float(rect.height)
        bx0, by0, bx1, by1 = (float(v) for v in info["bbox"])
        x0, y0 = max(float(rect.x0), bx0), max(float(rect.y0), by0)
        x1, y1 = min(float(rect.x1), bx1), min(float(rect.y1), by1)
        cover = max(0.0, (x1 - x0) * (y1 - y0)) / area if area > 0 else 0.0
    except Exception:
        return None
    if cover < _FULL_PAGE_COVER:
        return None
    return dict(info)


def stream_fingerprint(doc: Any, xref: int) -> str | None:
    """sha256 of the undecoded stream + the image dictionary keys that
    change how the same bytes decode. None when PyMuPDF cannot read it."""
    try:
        raw = doc.xref_stream_raw(xref)
    except Exception:
        return None
    if raw is None:
        return None
    h = hashlib.sha256()
    h.update(bytes(raw))
    for key in _IMAGE_DICT_KEYS:
        try:
            kind, value = doc.xref_get_key(xref, key)
        except Exception:
            kind, value = "null", "null"
        h.update(f"|{key}={kind}:{value}".encode())
    return h.hexdigest()


def samples_fingerprint(samples: bytes, width: int, height: int) -> str:
    h = hashlib.sha256()
    h.update(f"{width}x{height}:".encode())
    h.update(samples)
    return h.hexdigest()


def compose_key(
    content: str,
    *,
    rotation: int,
    dpi: int,
    language: str,
    psm: int,
    engine: str,
    preproc: str,
) -> str:
    parts = f"{content}|rot={rotation}|dpi={dpi}|lang={language}|psm={psm}|engine={engine}|pre={preproc}"
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()
