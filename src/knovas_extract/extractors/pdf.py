"""PDF extractor — PyMuPDF backend.

PyMuPDF wraps MuPDF (C). It's by far the fastest mature Python PDF library
(20-100 pages/sec text-only on a single thread) but it is **AGPL-licensed** -
downstream users must comply with the AGPL's network-distribution clauses if
they embed knovas-extract in a closed-source product that loads PyMuPDF at
runtime. See NOTICE; install permissive-only stack via `pip install
knovas-extract[minimal]`.

Security posture (see SECURITY.md):
- Embedded JavaScript: never executed; we don't even enumerate JS streams.
  A warning is emitted when present so callers can audit.
- Encrypted PDFs: raise EncryptedDocumentError (we never attempt blank-password
  bypass; some valid PDFs are intentionally locked).
- Page-count cap: enforced via Limits.max_pages → ResourceExhaustedError when
  the source exceeds it (default 10 000).
- Hostile inputs (malformed object streams etc.): PyMuPDF's `fitz.open()` and
  page-level operations raise `fitz.FileDataError` / `RuntimeError`, which we
  re-raise as `CorruptDocumentError`.

The extractor is intentionally text-focused. Images, forms, annotations, and
embedded files are NOT extracted in v1; per-format metadata of interest is
surfaced under `metadata.extra` with `pdf:` namespace.

OCR (0.4.0, see docs/ocr.md): the decision is made PER PAGE
(`_ocr.decision`): a usable text layer is kept verbatim, a raster page
without one is an OCR candidate. Candidates go through the bounded,
fail-soft scheduler (`_ocr.pipeline` / `_ocr.pool`); budgets in `Limits`
are never raised, pages beyond them are counted. Born-digital PDFs produce
exactly the same output as before.

Layout mode (`text_mode="layout"`, see docs/layout-text-mode.md): every
page's word boxes — the born-digital text layer read through the SAME
PyMuPDF text page that produced the plain text, or the OCR words of a
scanned page — are rendered into markdown-lite by `_layout` and that
rendering becomes `Page.text`. An unstructured page is byte-identical to
plain mode (GI-EXTRACT-03). `_pdf_layout` holds the wiring.
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from knovas_extract._ocr.decision import OcrDecision, page_needs_ocr
from knovas_extract._ocr.options import OcrOptions
from knovas_extract._pdf_layout import (
    LayoutPageInput,
    LayoutPageOutput,
    TextModeT,
    layout_words_from_ocr,
    render_layout_pages,
    validate_text_mode,
)
from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import (
    CorruptDocumentError,
    DependencyMissingError,
    EncryptedDocumentError,
    ResourceExhaustedError,
)
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Page, Section

UseOcrT = bool | Literal["auto"]
DEFAULT_OCR_LANGUAGE = "deu+eng"

# PyMuPDF `doc.permissions` bitmask flags. See pdfmark spec + PyMuPDF docs.
_PDF_PERM_TOKENS = (
    (1 << 2, "print"),
    (1 << 3, "modify"),
    (1 << 4, "copy"),
    (1 << 5, "annotate"),
    (1 << 8, "form"),
    (1 << 9, "accessibility"),
    (1 << 10, "assemble"),
    (1 << 11, "print_high_res"),
)


def _perm_tokens(bits: int | None) -> str | None:
    if not bits:
        return None
    tokens = [name for mask, name in _PDF_PERM_TOKENS if bits & mask]
    return ",".join(tokens) if tokens else None


def _parse_xmp(xmp: str, limits: Limits, warnings: list[str]) -> dict[str, str]:
    """Parse PDF XMP metadata (XML) via defusedxml, size-capped.

    Returns a dict with any of: title, author, description, language,
    creator_tool, pdfa_part. Empty dict on error / oversize / absence.
    """
    if not xmp:
        return {}
    if len(xmp) > limits.max_xmp_bytes:
        warnings.append("pdf: xmp metadata exceeded max_xmp_bytes; skipped")
        return {}

    try:
        from defusedxml import ElementTree as ET

        root = ET.fromstring(xmp)
    except Exception:
        warnings.append("pdf: xmp metadata unparseable; skipped")
        return {}

    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "xmp": "http://ns.adobe.com/xap/1.0/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "pdfaid": "http://www.aiim.org/pdfa/ns/id/",
    }

    def _text_deep(tag: str, prefix: str) -> str | None:
        # dc: / xmp: fields are usually wrapped in rdf:Alt / rdf:Seq / rdf:li.
        for el in root.iter(f"{{{ns[prefix]}}}{tag}"):
            if el.text and el.text.strip():
                return cast(str, el.text.strip())
            # Nested rdf:li.
            for child in el.iter(f"{{{ns['rdf']}}}li"):
                if child.text and child.text.strip():
                    return cast(str, child.text.strip())
        return None

    def _attr_deep(tag: str, prefix: str, attr_prefix: str, attr: str) -> str | None:
        for el in root.iter(f"{{{ns[prefix]}}}{tag}"):
            v = el.attrib.get(f"{{{ns[attr_prefix]}}}{attr}") or el.attrib.get(attr)
            if v:
                return v.strip() or None
        return None

    out: dict[str, str] = {}
    for k, tag, pfx in (
        ("title", "title", "dc"),
        ("author", "creator", "dc"),
        ("description", "description", "dc"),
        ("language", "language", "dc"),
        ("creator_tool", "CreatorTool", "xmp"),
    ):
        v = _text_deep(tag, pfx)
        if v:
            out[k] = v

    part = _attr_deep("part", "pdfaid", "pdfaid", "part")
    if part:
        out["pdfa_part"] = part
    return out


if TYPE_CHECKING:
    import fitz

    from knovas_extract._layout import Rule, Word

    from ..result import Table


def _parse_pdf_date(s: str | None) -> str | None:
    """Convert a PDF /D:YYYYMMDDHHMMSS+ZZ'zz' date string to ISO 8601.

    PyMuPDF returns metadata dates in the raw PDF format; the schema requires
    ISO 8601. Best-effort: returns None if parsing fails (the field is
    optional in the schema).
    """
    if not s:
        return None
    s = s.strip()
    if s.startswith("D:"):
        s = s[2:]
    # Format: YYYYMMDDHHMMSSOHH'mm' (O = + or - or Z)
    if len(s) < 4:
        return None
    try:
        year = int(s[0:4])
        month = int(s[4:6]) if len(s) >= 6 else 1
        day = int(s[6:8]) if len(s) >= 8 else 1
        hour = int(s[8:10]) if len(s) >= 10 else 0
        minute = int(s[10:12]) if len(s) >= 12 else 0
        second = int(s[12:14]) if len(s) >= 14 else 0
    except ValueError:
        return None
    # Timezone — best-effort. Common forms: Z, +0200, +02'00'.
    tz = "Z"
    if len(s) >= 15:
        rest = s[14:].replace("'", "")
        if rest.startswith(("+", "-")) and len(rest) >= 3:
            sign = rest[0]
            tzhour = rest[1:3]
            tzmin = rest[3:5] if len(rest) >= 5 else "00"
            # Only emit a numeric offset when both fields are digits; otherwise
            # fall back to Z rather than produce a malformed ISO string like
            # "+ab:cd" that downstream date parsers would reject.
            if tzhour.isdigit() and tzmin.isdigit():
                tz = f"{sign}{tzhour}:{tzmin}"
        elif rest.startswith("Z"):
            tz = "Z"
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}{tz}"


def _pdf_to_markdown(
    doc: fitz.Document,
    plain_text: str,
    limits: Limits,
    warnings: list[str],
) -> str | None:
    """Emit whole-doc markdown via pymupdf4llm, with URL allowlist + size guards.

    Returns None (and appends a warning) on backend failure — never
    silently substitutes plain text as if it were markdown.
    """
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise DependencyMissingError("pdf-markdown", "pymupdf4llm") from exc

    from knovas_extract._markdown import apply_url_allowlist, check_expansion

    try:
        # to_markdown() returns str by default (page_chunks=False); the union
        # return type (str | list[dict]) only applies with page_chunks=True.
        raw_md = cast(str, pymupdf4llm.to_markdown(doc))
    except Exception:
        warnings.append("pdf: pymupdf4llm conversion failed; content.markdown left null")
        return None

    md = canonicalize_text(raw_md or "")

    if len(md.encode("utf-8")) > limits.max_text_bytes:
        raise ResourceExhaustedError("markdown size", limits.max_text_bytes, observed=len(md))
    check_expansion(md, len(plain_text), limits)

    # PDF annotation URLs are the primary risk: /URI actions with
    # javascript: / file: schemes come through pymupdf4llm as clickable
    # markdown links. Scrub via the shared allowlist.
    return apply_url_allowlist(md, warnings=warnings)


# ---------- structured tables (spec 1.1.0+) ----------

_TABLE_CELL_MAX_CHARS = 1024
_TABLE_MAX_ROWS = 5000
_TABLE_MAX_COLS = 64
_TABLES_MAX_PER_DOC = 50


def _cap_pdf_cell(
    v: object, warnings: list[str], t_idx: int, page_1based: int, row_hint: str
) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) <= _TABLE_CELL_MAX_CHARS:
        return s
    warnings.append(
        f"pdf: tables[{t_idx}].{row_hint} truncated at {_TABLE_CELL_MAX_CHARS} chars (page {page_1based})"
    )
    return s[:_TABLE_CELL_MAX_CHARS]


def _extract_structured_tables_from_pdf(doc: Any, warnings: list[str]) -> list[Table]:
    """Iterate pages, call PyMuPDF.find_tables(), materialize as `Table` list.

    Failures during table detection on a single page are demoted to warnings —
    they must not fail the whole extraction. `page.find_tables()` was added in
    PyMuPDF 1.23; older builds raise AttributeError and this helper returns [].
    """
    from ..result import Table

    tables: list[Table] = []
    t_idx = 0

    for page_index in range(doc.page_count):
        if len(tables) >= _TABLES_MAX_PER_DOC:
            warnings.append(
                f"pdf: table extraction stopped at {_TABLES_MAX_PER_DOC} tables (spec cap)"
            )
            break
        try:
            page = doc.load_page(page_index)
        except Exception as exc:
            warnings.append(
                f"pdf: page {page_index} could not load for table scan ({type(exc).__name__})"
            )
            continue
        try:
            finder = page.find_tables()
        except AttributeError:
            # PyMuPDF too old (< 1.23) — no table API. Return what we've got.
            warnings.append("pdf: table extraction unavailable (PyMuPDF too old, needs >= 1.23)")
            return tables
        except Exception as exc:
            # Never leak the raw exception message — PyMuPDF errors can carry
            # cell contents. Log the exception CLASS only.
            warnings.append(
                f"pdf: table detection failed on page {page_index + 1} ({type(exc).__name__})"
            )
            continue

        found = getattr(finder, "tables", None) or []
        for on_page_idx, tbl in enumerate(found):
            if len(tables) >= _TABLES_MAX_PER_DOC:
                warnings.append(
                    f"pdf: table extraction stopped at {_TABLES_MAX_PER_DOC} tables (spec cap)"
                )
                return tables
            try:
                raw_rows = tbl.extract()
            except Exception as exc:
                warnings.append(
                    f"pdf: table extract failed on page {page_index + 1}, table {on_page_idx} ({type(exc).__name__})"
                )
                continue

            if not raw_rows:
                continue

            # Headers: prefer explicit tbl.header.names when PyMuPDF's heuristic
            # identifies a header row; fall back to row[0].
            hdr_names = None
            try:
                header = getattr(tbl, "header", None)
                if header is not None and getattr(header, "names", None):
                    hdr_names = list(header.names)
            except Exception:
                hdr_names = None

            data_rows = raw_rows
            if hdr_names is None:
                hdr_names = raw_rows[0]
                data_rows = raw_rows[1:]

            headers = [
                _cap_pdf_cell(
                    h if h is not None else f"col_{i}",
                    warnings,
                    t_idx,
                    page_index + 1,
                    f"header[{i}]",
                )
                for i, h in enumerate(hdr_names)
            ]
            headers = [h if h else f"col_{i}" for i, h in enumerate(headers)]

            if len(headers) > _TABLE_MAX_COLS:
                warnings.append(
                    f"pdf: tables[{t_idx}] column count {len(headers)} exceeds spec cap {_TABLE_MAX_COLS} — truncated (page {page_index + 1})"
                )
                headers = headers[:_TABLE_MAX_COLS]

            n_cols = len(headers)
            if n_cols == 0:
                continue

            rows_out: list[list[str]] = []
            for r_idx, raw_row in enumerate(data_rows):
                if len(rows_out) >= _TABLE_MAX_ROWS:
                    warnings.append(
                        f"pdf: tables[{t_idx}] row count exceeded spec cap {_TABLE_MAX_ROWS} — truncated (page {page_index + 1})"
                    )
                    break
                raw_list = list(raw_row) if raw_row is not None else []
                if len(raw_list) < n_cols:
                    raw_list = raw_list + [""] * (n_cols - len(raw_list))
                elif len(raw_list) > n_cols:
                    raw_list = raw_list[:n_cols]
                cells = [
                    _cap_pdf_cell(c, warnings, t_idx, page_index + 1, f"rows[{r_idx}][{ci}]")
                    for ci, c in enumerate(raw_list)
                ]
                if any(cells):
                    rows_out.append(cells)

            if not rows_out:
                continue

            # bbox: (x0, y0, x1, y1) in the page coordinate system when available.
            bbox: tuple[float, float, float, float] | None = None
            try:
                raw_bbox = getattr(tbl, "bbox", None)
                if raw_bbox is not None and len(raw_bbox) == 4:
                    bbox = (
                        float(raw_bbox[0]),
                        float(raw_bbox[1]),
                        float(raw_bbox[2]),
                        float(raw_bbox[3]),
                    )
            except Exception:
                bbox = None

            tables.append(
                Table(
                    client_table_hint=f"pdf_p{page_index + 1}_t{on_page_idx}",
                    title=None,
                    headers=headers,
                    rows=rows_out,
                    page=page_index + 1,
                    bbox=bbox,
                )
            )
            t_idx += 1

    return tables


@dataclass(slots=True)
class _OcrSummary:
    """What the per-page OCR pass reports (scalars only — GI-EXTRACT-04)."""

    report: bool  # emit the pdf:ocr_* keys at all
    ocr_pages: int = 0  # pages whose text came from OCR (attempted - failed)
    text_pages: int = 0  # pages whose text layer was used as is (non-empty)
    skipped: int = 0  # candidates never attempted (budget / pixel cap / no backend)
    failed: int = 0
    backend: str = "none"
    backend_version: str | None = None
    seconds: float | None = None
    cpu_seconds: float | None = None
    mean_conf: float | None = None


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _read_page_for_layout(page: Any) -> tuple[str, list[Word], list[Rule], bool]:
    """Plain text and layout words of a born-digital page from ONE PyMuPDF text
    page (``TEXTFLAGS_TEXT`` — the flags ``page.get_text("text")`` uses, so the
    text is byte-identical to plain mode and the word set is the text's), plus
    the vector rulings. Returns ``(text, words, rules, ok)``; a failure on the
    layout side leaves the page without words (it renders as passthrough).
    """
    import fitz  # local import — keeps top-level startup cost flat

    tp = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
    layer = cast(str, page.get_text("text", textpage=tp) or "")
    words: list[Word] = []
    rules: list[Rule] = []
    ok = True
    if layer.strip():
        from knovas_extract._layout import words_from_fitz_page

        try:
            words = words_from_fitz_page(page, tp)
        except Exception:
            words, ok = [], False
    from knovas_extract._layout import vector_rules_from_fitz_page

    with contextlib.suppress(Exception):
        rules = vector_rules_from_fitz_page(page)
    return layer, words, rules, ok


def _collect_page_texts(
    doc: Any,
    page_count: int,
    limits: Limits,
    warnings: list[str],
    *,
    use_ocr: UseOcrT = "auto",
    ocr: OcrOptions | None = None,
    explicit_ocr: bool = False,
    text_mode: TextModeT = "plain",
) -> tuple[list[Page], list[str], int, _OcrSummary, list[LayoutPageOutput] | None]:
    """Return (pages, raw page text chunks, total UTF-8 bytes, OCR summary,
    layout outputs — ``None`` in plain mode).

    Per-page OCR decision (GI-EXTRACT-01, Alloy
    ``PerPageOcrDecisionMechanism``): every page's text layer is read, the
    decision is taken per page, the candidates are OCR'd through the
    bounded fail-soft scheduler, and each page's text is assembled from
    the layer (kept / discarded) and the OCR text (replacing / appended).

    Fail-soft (decision D10): in ``use_ocr="auto"`` a missing OCR backend
    leaves every candidate's text layer in place, adds one counted warning
    and reports ``backend="none"``; `DependencyMissingError` propagates
    only when an engine was forced (``OcrOptions.engine`` / ``use_ocr=True``)
    or when the document would otherwise be completely empty.

    Layout mode (``text_mode="layout"``): the plain-mode page text above is
    kept as the renderer's passthrough text; the page's words (text layer
    through the same text page, or the OCR words) go through `_layout` and
    the rendering replaces the page text. A kept text layer on an OCR'd
    stamp page is rendered as a leading paragraph above the OCR layout.
    """
    options = ocr or OcrOptions()
    layout_mode = text_mode == "layout"
    layer_texts: dict[int, str] = {}
    decisions: dict[int, OcrDecision] = {}
    loaded: list[int] = []
    geometry: dict[int, tuple[float, float]] = {}
    digital_words: dict[int, list[Word]] = {}
    page_rules: dict[int, list[Rule]] = {}
    layout_failed = 0

    for i in range(page_count):
        try:
            page = doc.load_page(i)
        except Exception as exc:
            warnings.append(f"page {i}: could not load ({exc})")
            continue
        if layout_mode:
            layer, words, rules, ok = _read_page_for_layout(page)
            digital_words[i] = words
            page_rules[i] = rules
            geometry[i] = (float(page.rect.width), float(page.rect.height))
            if not ok:
                layout_failed += 1
        else:
            layer = cast(str, page.get_text("text") or "")
        if not layer and i == 0:
            warnings.append("first page produced no text (OCR may help for scanned PDFs)")
        layer_texts[i] = layer
        decisions[i] = page_needs_ocr(page, use_ocr, text=layer)
        loaded.append(i)

    candidates = [i for i in loaded if decisions[i].needs_ocr]
    summary = _OcrSummary(report=explicit_ocr or bool(candidates) or use_ocr is True)
    summary.text_pages = sum(
        1 for i in loaded if not decisions[i].needs_ocr and layer_texts[i].strip()
    )

    doc_res = None
    backend_missing = False
    if candidates:
        from knovas_extract._ocr.pipeline import run_document_ocr

        try:
            doc_res = run_document_ocr(
                doc, candidates, options=options, limits=limits, language=options.language
            )
        except DependencyMissingError:
            forced = options.engine != "auto" or use_ocr is True
            kept_any = any(layer_texts[i].strip() for i in loaded if decisions[i].keep_text_layer)
            if forced or not kept_any:
                raise
            backend_missing = True
            warnings.append(
                f"pdf: OCR backend unavailable; {_plural(len(candidates), 'page')} left without OCR"
            )
            summary.skipped = len(candidates)

    if doc_res is not None:
        run = doc_res.run
        summary.ocr_pages = len(run.attempted) - len(run.failed)
        summary.failed = len(run.failed)
        summary.skipped = len(run.skipped) + len(doc_res.oversize)
        summary.backend = doc_res.backend_name
        summary.backend_version = doc_res.backend_version
        summary.seconds = round(run.seconds, 3)
        summary.cpu_seconds = round(run.cpu_seconds, 3)
        summary.mean_conf = doc_res.mean_conf
        if summary.ocr_pages:
            warnings.append(
                f"pdf: OCR applied to {summary.ocr_pages} of {page_count} pages via {summary.backend}"
            )
        if run.skipped:
            warnings.append(
                f"pdf: {_plural(len(run.skipped), 'page')} skipped: OCR budget exhausted"
            )
        if doc_res.oversize:
            warnings.append(
                f"pdf: {_plural(len(doc_res.oversize), 'page')} skipped: image exceeds max_ocr_image_megapixels"
            )
        if run.failed:
            warnings.append(f"pdf: {_plural(len(run.failed), 'page')} failed OCR")

    text_chunks: list[str] = []
    layout_inputs: list[LayoutPageInput] = []
    for i in loaded:
        decision = decisions[i]
        layer = layer_texts[i]
        ocr_text = ""
        ocr_words: list[Any] = []
        if decision.needs_ocr and not backend_missing and doc_res is not None:
            res = doc_res.results.get(i)
            if res is not None:
                ocr_text = res.text
                ocr_words = res.words
        if not decision.needs_ocr or backend_missing:
            page_text = layer
        elif decision.keep_text_layer:
            page_text = layer
            if ocr_text.strip():
                page_text = f"{layer}\n\n{ocr_text}" if layer.strip() else ocr_text
        else:
            page_text = ocr_text
        text_chunks.append(page_text)
        if not layout_mode:
            continue
        from_ocr = decision.needs_ocr and not backend_missing and bool(ocr_text.strip())
        lead = ""
        if from_ocr:
            try:
                words = layout_words_from_ocr(ocr_words)
            except Exception:
                words = []
                layout_failed += 1
            if decision.keep_text_layer and layer.strip():
                lead = layer
        else:
            words = digital_words.get(i, [])
        page_w, page_h = geometry[i]
        layout_inputs.append(
            LayoutPageInput(
                i, page_w, page_h, page_text, words, from_ocr, page_rules.get(i, []), lead
            )
        )

    layout_outputs: list[LayoutPageOutput] | None = None
    if layout_mode:
        try:
            layout_outputs = render_layout_pages(layout_inputs)
        except Exception as exc:
            # Fail-soft: the plain-mode text is always a valid rendering.
            warnings.append(
                f"pdf: layout rendering failed ({type(exc).__name__}); plain text emitted"
            )
            layout_outputs = [
                LayoutPageOutput(t, False, word_count=word_count(t)) for t in text_chunks
            ]
        text_chunks = [o.text for o in layout_outputs]
        if layout_failed:
            warnings.append(
                f"pdf: layout words unavailable on {_plural(layout_failed, 'page')}; "
                "emitted as plain text"
            )

    pages: list[Page] = []
    total_bytes = 0
    for i, page_text in zip(loaded, text_chunks, strict=True):
        pages.append(Page(index=i, text=canonicalize_text(page_text)))
        total_bytes += len(page_text.encode("utf-8"))
        if total_bytes > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=total_bytes)
    return pages, text_chunks, total_bytes, summary, layout_outputs


def _sections_from_layout(pages: list[Page], outputs: list[LayoutPageOutput]) -> list[Section]:
    """`Section` records for the ``#`` lines the layout renderer emitted.

    `SectionRecord` lines are 0-based inside the page text; `Page.line_start`
    (1-based, into ``content.text``) maps them into document coordinates —
    the same contract DOCX / HTML sections carry. The section body is the
    page text between the heading and the section's last line.
    """
    sections: list[Section] = []
    for page, out in zip(pages, outputs, strict=True):
        if page.line_start is None or not out.sections:
            continue
        lines = page.text.split("\n")
        for rec in out.sections:
            if not 0 <= rec.line_start < len(lines):
                continue
            end = min(max(rec.line_end, rec.line_start), len(lines) - 1)
            heading = lines[rec.line_start].lstrip("#").strip()
            body = "\n".join(lines[rec.line_start + 1 : end + 1])
            sections.append(
                Section(
                    heading=heading,
                    level=max(1, min(rec.level, 4)),
                    text=canonicalize_text(body),
                    line_start=page.line_start + rec.line_start,
                    line_end=page.line_start + end,
                )
            )
    return sections


def _open_doc(data: bytes) -> fitz.Document:
    """Open the PDF, mapping every PyMuPDF failure mode to a typed ExtractError."""
    import fitz  # local import — keeps top-level startup cost flat

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        # fitz.FileDataError, RuntimeError, ValueError — all signal an
        # unparseable input. Don't leak the underlying exception type to
        # callers; the contract is "ExtractError or success".
        raise CorruptDocumentError(f"could not parse PDF: {exc}") from exc

    # MuPDF (1.25+) sniffs the stream's content and `filetype` is only a hint:
    # a payload that starts like Markdown (`# …`) or HTML (`<html>`) opens as a
    # "Markdown document" / "HTML5" document and its raw characters -- form
    # feeds included, which are the Remote Controller's page-break marker --
    # reach the text. This extractor handles PDFs only; anything else that
    # arrives under application/pdf is an unparseable PDF, not a document
    # of whatever kind MuPDF recognised.
    if not doc.is_pdf:
        recognised = (doc.metadata or {}).get("format") or "non-PDF"
        doc.close()
        raise CorruptDocumentError(
            f"could not parse PDF: the payload is not a PDF (MuPDF recognised it as {recognised!r})"
        )

    # Encryption check. We refuse password-protected PDFs predictably; the
    # blank-password attempt covers PDFs that claim is_encrypted but accept
    # an empty owner password (some scanner-generated PDFs do this).
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise EncryptedDocumentError(
            "PDF is encrypted; provide an unlocked copy or a password "
            "(passwords are not exposed via the public knovas-extract API yet)."
        )
    return doc


class PdfExtractor(IExtractor):
    """PDF text + metadata extractor."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/pdf"})
    name: ClassVar[str] = "pdf"

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
        emit_markdown: bool = False,
        emit_sentences: bool = False,
        use_ocr: UseOcrT = "auto",
        ocr_language: str = DEFAULT_OCR_LANGUAGE,
        ocr: OcrOptions | None = None,
        text_mode: TextModeT = "plain",
    ) -> ExtractionResult:
        from collections import Counter

        from knovas_extract._metadata import finalize_warnings, sanitize_scalar

        limits = limits or Limits()
        if len(data) > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))
        mode = validate_text_mode(text_mode)

        warnings: list[str] = []
        counts: Counter[str] = Counter()
        doc = _open_doc(data)
        ocr_options = ocr if ocr is not None else OcrOptions(language=ocr_language)

        try:
            page_count = doc.page_count
            if page_count > limits.max_pages:
                raise ResourceExhaustedError("page_count", limits.max_pages, observed=page_count)

            pages, text_chunks, _total_bytes, ocr_summary, layout_outputs = _collect_page_texts(
                doc,
                page_count,
                limits,
                warnings,
                use_ocr=use_ocr,
                ocr=ocr_options,
                explicit_ocr=ocr is not None,
                text_mode=mode,
            )
            full_text = canonicalize_text("\n\n".join(text_chunks))
            ocr_applied = ocr_summary.ocr_pages > 0
            if ocr_applied and emit_markdown:
                warnings.append(
                    "pdf: content.markdown omitted for OCR output (no structure to preserve)"
                )

            had_js = False
            # Doc-level JS check (cheap; runs once). Heuristic only; the
            # only thing we ever do with detected JS is emit a warning.
            with contextlib.suppress(Exception):
                if doc.has_links() or any(
                    "/JS" in str(doc.xref_object(xref))
                    for xref in range(1, min(50, doc.xref_length()))
                ):
                    had_js = True
            if had_js:
                warnings.append("PDF embedded JavaScript ignored (never executed)")

            # Authoritative post-canonicalization byte cap. The per-page running
            # total is a cheap early abort; canonicalization can shift the size,
            # so re-check the final joined text (parity with docx/html/rtf/eml).
            full_text_bytes = len(full_text.encode("utf-8"))
            if full_text_bytes > limits.max_text_bytes:
                raise ResourceExhaustedError(
                    "text size", limits.max_text_bytes, observed=full_text_bytes
                )

            # Attach 1-based line coordinates to each Page by locating each
            # page.text in full_text (they are identical after
            # canonicalize_text, up to the "\n\n" join). The cursor keeps
            # searches linear.
            cursor = 0
            for p in pages:
                if not p.text:
                    p.line_start = None
                    p.line_end = None
                    continue
                loc = full_text.find(p.text, cursor)
                if loc < 0:
                    p.line_start = None
                    p.line_end = None
                    continue
                p.line_start = 1 + full_text.count("\n", 0, loc)
                p.line_end = 1 + full_text.count("\n", 0, loc + len(p.text) - 1)
                cursor = loc + len(p.text)

            # Layout mode: `Section` records from the emitted `#` lines
            # (page-relative lines mapped through Page.line_start).
            sections: list[Section] | None = None
            if layout_outputs is not None:
                sections = _sections_from_layout(pages, layout_outputs) or None

            # Markdown path — whole-doc via pymupdf4llm (text-layer PDFs only).
            markdown: str | None = None
            if emit_markdown and not ocr_applied:
                markdown = _pdf_to_markdown(doc, full_text, limits, warnings)

            raw_meta = doc.metadata or {}

            # XMP metadata — parsed via defusedxml with a size cap.
            xmp_dict: dict[str, str] = {}
            with contextlib.suppress(Exception):
                xmp_raw = doc.get_xml_metadata() or ""
                xmp_dict = _parse_xmp(xmp_raw, limits, warnings)

            # Merge policy: XMP wins over the older doc.metadata dict when
            # both are present and non-empty for the same first-class field.
            title = xmp_dict.get("title") or (raw_meta.get("title") or "").strip() or None
            author = xmp_dict.get("author") or (raw_meta.get("author") or "").strip() or None
            language = xmp_dict.get("language") or None

            extra: dict[str, str | int | float | bool | None] = {}
            for k, v in {
                "pdf:producer": (raw_meta.get("producer") or "").strip() or None,
                "pdf:creator": (raw_meta.get("creator") or "").strip() or None,
                "pdf:subject": (raw_meta.get("subject") or "").strip() or None,
                "pdf:keywords": (raw_meta.get("keywords") or "").strip() or None,
                "pdf:format": raw_meta.get("format"),
                "pdf:xmp_description": xmp_dict.get("description"),
                "pdf:xmp_creator_tool": xmp_dict.get("creator_tool"),
                "pdf:pdfa_part": xmp_dict.get("pdfa_part"),
            }.items():
                if v is None:
                    continue
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[k] = clean

            # PDF version, permissions, outline count, forms/annotations.
            with contextlib.suppress(Exception):
                pdf_version = getattr(doc, "pdf_version", None)
                if callable(pdf_version):
                    pv = pdf_version()
                    if pv is not None:
                        extra["pdf:pdf_version"] = str(pv)
            with contextlib.suppress(Exception):
                perms = _perm_tokens(getattr(doc, "permissions", 0))
                if perms:
                    extra["pdf:permissions"] = perms
            with contextlib.suppress(Exception):
                extra["pdf:outline_count"] = len(doc.get_toc())
            with contextlib.suppress(Exception):
                extra["pdf:is_form_pdf"] = bool(doc.is_form_pdf)

            # OCR scalars (GI-EXTRACT-04: counts and names, never text).
            # Reported whenever OCR was configured explicitly or considered
            # for at least one page — a born-digital PDF extracted with the
            # defaults carries no OCR keys, so its output is unchanged.
            if ocr_summary.report:
                extra["pdf:ocr_pages"] = ocr_summary.ocr_pages
                extra["pdf:text_pages"] = ocr_summary.text_pages
                extra["pdf:ocr_pages_skipped"] = ocr_summary.skipped
                extra["pdf:ocr_pages_failed"] = ocr_summary.failed
                extra["pdf:ocr_backend"] = ocr_summary.backend
                if ocr_summary.backend_version:
                    extra["pdf:ocr_backend_version"] = ocr_summary.backend_version
                if ocr_summary.seconds is not None:
                    extra["pdf:ocr_seconds"] = ocr_summary.seconds
                if ocr_summary.cpu_seconds is not None:
                    extra["pdf:ocr_cpu_seconds"] = ocr_summary.cpu_seconds
                if ocr_summary.mean_conf is not None:
                    extra["pdf:ocr_mean_conf"] = ocr_summary.mean_conf

            # Layout-mode scalars (counts only; absent in plain mode so the
            # default output is unchanged). The word count is taken on the
            # markup-stripped text so it matches plain mode on unstructured
            # documents (plan §4, [C-reg-11]).
            if layout_outputs is not None:
                extra["pdf:text_mode"] = "layout"
                extra["pdf:structured_pages"] = sum(1 for o in layout_outputs if o.structured)
                extra["pdf:layout_tables"] = sum(o.tables for o in layout_outputs)
                total_words = sum(o.word_count for o in layout_outputs)
            else:
                total_words = word_count(full_text)

            finalize_warnings(counts, warnings)

            metadata = Metadata(
                title=title,
                author=author,
                language=language,
                created=_parse_pdf_date(raw_meta.get("creationDate")),
                modified=_parse_pdf_date(raw_meta.get("modDate")),
                page_count=page_count,
                word_count=total_words,
                extra=extra,
            )

            # Sentences — per-page tokenization stitched into
            # document-global coords via split_sentences_for_pages.
            sentences = None
            if emit_sentences:
                from knovas_extract._sentences import split_sentences_for_pages

                sentences = split_sentences_for_pages(
                    pages, full_text, limits, warnings=warnings, language=language
                )

            # Structured tables (spec 1.1.0+) — never let table extraction fail
            # the whole document; demote to a warning.
            try:
                tables = _extract_structured_tables_from_pdf(doc, warnings)
            except Exception as exc:
                tables = []
                warnings.append(f"pdf: structured table pass failed ({type(exc).__name__})")

            return make_result(
                text=full_text,
                mime="application/pdf",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                filename=filename,
                metadata=metadata,
                pages=pages or None,
                sections=sections,
                warnings=warnings,
                markdown=markdown,
                sentences=sentences,
                tables=tables or None,
            )
        finally:
            doc.close()


MIME_REGISTRY["application/pdf"] = PdfExtractor()
