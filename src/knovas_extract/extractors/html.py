"""HTML extractor — selectolax (lexbor) backend.

selectolax wraps the lexbor C HTML5 parser. It is fast (~25× BeautifulSoup),
forgiving of malformed input, and crucially it does **not** execute scripts,
follow `<img>`/`<link>` resources, or resolve any external entity references.
For our threat model that's the right tool.

Security posture (see SECURITY.md):
- **No script execution**: lexbor is a pure parser; <script> bodies are parsed
  as text and either dropped or surfaced as raw content depending on caller
  preference. We never `exec`/`eval`.
- **No network**: lexbor performs zero I/O. pytest-socket enforces this from
  the test side; nothing in this extractor opens a socket.
- **No XML/XXE**: HTML5 parsing is not XML parsing. Even XHTML input is parsed
  as HTML5 by lexbor — `<!ENTITY>` / `<!DOCTYPE>` references are ignored, not
  resolved. This is the design difference that makes selectolax safer than
  `lxml.etree.parse` for hostile input.
- **Resource exhaustion**: lexbor handles deeply-nested input in linear time.
  The size + nesting depth caps in Limits still apply.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, ClassVar

from knovas_extract.dispatch import MIME_REGISTRY, make_result
from knovas_extract.errors import CorruptDocumentError, ResourceExhaustedError
from knovas_extract.extractors.txt import _decode
from knovas_extract.interfaces import IExtractor
from knovas_extract.normalize import canonicalize_text, word_count
from knovas_extract.result import ExtractionResult, Limits, Metadata, Section

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

_URL_SCHEME_ALLOWLIST = frozenset({"http", "https", "mailto", "tel"})


def _url_scheme_ok(url: str | None) -> bool:
    """True iff the URL's scheme is in the allowlist. `None`/empty → False."""
    if not url:
        return False
    s = url.strip()
    # Protocol-relative (`//host/...`) has no explicit scheme → reject.
    if s.startswith("//"):
        return False
    if ":" not in s:
        # No scheme — treat as relative; safe.
        return True
    scheme = s.split(":", 1)[0].strip().lower()
    return scheme in _URL_SCHEME_ALLOWLIST


def _extract_html_metadata(tree: Any) -> dict[str, str | None]:
    # selectolax HTMLParser has no type stubs; `tree: Any` preserves the
    # css_first/css attribute access under mypy strict.
    """Pull title + selected <meta> fields. Always returns a dict (possibly all None).

    Includes the extended metadata surface: description, keywords, author,
    canonical URL, robots, generator, article:*, OpenGraph og:* (property=),
    Twitter Card twitter:* (name= or property=). URL-valued fields are
    kept only when the URL scheme is in the allowlist; drops are counted
    into the top-level `dropped_urls` sentinel key.
    """
    meta: dict[str, str | None] = {
        "title": None,
        "description": None,
        "keywords": None,
        "author": None,
        "language": None,
        "charset_declared": None,
        "canonical": None,
        "robots": None,
        "generator": None,
        "article_published_time": None,
        "article_modified_time": None,
        "article_author": None,
        "article_section": None,
    }
    og: dict[str, str] = {}
    twitter: dict[str, str] = {}
    dropped_urls = 0

    title_node = tree.css_first("title")
    if title_node and title_node.text():
        meta["title"] = title_node.text(strip=True) or None

    # <html lang="...">.
    html_node = tree.css_first("html")
    if html_node is not None:
        lang = html_node.attributes.get("lang")
        if lang:
            meta["language"] = lang.strip() or None

    # <link rel="canonical" href=...>.
    for link in tree.css("link"):
        rel = (link.attributes.get("rel") or "").strip().lower()
        if rel == "canonical":
            href = link.attributes.get("href")
            if href:
                if _url_scheme_ok(href):
                    meta["canonical"] = href.strip() or None
                else:
                    dropped_urls += 1
            break

    for m in tree.css("meta"):
        name = (m.attributes.get("name") or "").strip().lower()
        prop = (m.attributes.get("property") or "").strip().lower()
        content_attr = m.attributes.get("content")
        content = content_attr.strip() if content_attr else None

        # Basic name= tags.
        if name == "description" and content:
            meta["description"] = content
        elif name == "keywords" and content:
            meta["keywords"] = content
        elif name == "author" and content:
            meta["author"] = content
        elif name == "robots" and content:
            meta["robots"] = content
        elif name == "generator" and content:
            meta["generator"] = content

        # Open Graph — spec-canonical namespace is `property=`.
        if prop.startswith("og:") and content:
            og_key = prop[3:]  # strip "og:"
            # URL-valued OG fields must pass the scheme filter.
            if og_key in ("url", "image") and not _url_scheme_ok(content):
                dropped_urls += 1
            else:
                og[og_key] = content

        # Twitter Cards — accept both name= and property=.
        tw_key: str | None = None
        if name.startswith("twitter:"):
            tw_key = name[8:]
        elif prop.startswith("twitter:"):
            tw_key = prop[8:]
        if tw_key and content:
            if tw_key in ("image", "url") and not _url_scheme_ok(content):
                dropped_urls += 1
            else:
                twitter[tw_key] = content

        # Article schema — property=.
        if prop == "article:published_time" and content:
            meta["article_published_time"] = content
        elif prop == "article:modified_time" and content:
            meta["article_modified_time"] = content
        elif prop == "article:author" and content:
            meta["article_author"] = content
        elif prop == "article:section" and content:
            meta["article_section"] = content

        # <meta charset="..."> AND <meta http-equiv="Content-Type" content="...">.
        charset = m.attributes.get("charset")
        if charset:
            meta["charset_declared"] = charset.strip() or None
        elif (m.attributes.get("http-equiv") or "").lower() == "content-type":
            ct = (content or "").lower()
            if "charset=" in ct:
                meta["charset_declared"] = ct.split("charset=", 1)[1].strip() or None

    for k, v in og.items():
        meta[f"og_{k}"] = v
    for k, v in twitter.items():
        meta[f"twitter_{k}"] = v
    if dropped_urls:
        meta["_dropped_urls"] = str(dropped_urls)
    return meta


# ---------- structured tables (spec 1.1.0+) ----------

_HTML_TABLE_CELL_MAX_CHARS = 1024
_HTML_TABLE_MAX_ROWS = 5000
_HTML_TABLE_MAX_COLS = 64
_HTML_TABLES_MAX_PER_DOC = 50


def _extract_html_structured_tables(tree, warnings: list[str]):
    """Walk `<table>` elements; emit `Table` dataclass instances.

    Preference for `<thead>`/`<tbody>` when present; falls back to using the
    first `<tr>` as headers. Nested tables are skipped intentionally — they
    are extremely rare in business documents and would explode the flat
    tables[] array in ways downstream consumers don't expect.
    """
    from ..result import Table

    tables: list = []
    seen_ids: set[int] = set()
    for dom_idx, table_node in enumerate(tree.css("table")):
        if len(tables) >= _HTML_TABLES_MAX_PER_DOC:
            warnings.append(
                f"html: table extraction stopped at {_HTML_TABLES_MAX_PER_DOC} tables (spec cap)"
            )
            break
        # Skip nested tables — take only top-level tables.
        node_id = id(table_node)
        if node_id in seen_ids:
            continue
        for nested in table_node.css("table"):
            seen_ids.add(id(nested))

        headers: list[str] = []
        rows: list[list[str]] = []
        header_tr_ids: set[int] = set()

        thead = None
        try:
            thead = table_node.css_first("thead")
        except Exception:
            thead = None
        tbody = None
        try:
            tbody = table_node.css_first("tbody")
        except Exception:
            tbody = None

        # Headers.
        if thead is not None:
            for th_row in thead.css("tr"):
                header_tr_ids.add(id(th_row))
                cells = th_row.css("th, td")
                if cells and not headers:
                    headers = [_html_cell_text(c) for c in cells]

        # Row source. When <tbody> is present, iterate only its rows; otherwise
        # iterate rows in the table but exclude any that live under a <thead>.
        # selectolax `id()` does not survive across CSS queries, so we compare
        # by cell-text signature against headers we already captured.
        header_signature = tuple(headers) if headers else None
        if tbody is not None:
            candidate_trs = tbody.css("tr")
        else:
            all_trs = table_node.css("tr")
            if thead is not None:
                thead_trs = thead.css("tr")
                thead_signatures = {
                    tuple(_html_cell_text(c) for c in tr.css("th, td"))
                    for tr in thead_trs
                }
                candidate_trs = [
                    tr for tr in all_trs
                    if tuple(_html_cell_text(c) for c in tr.css("th, td")) not in thead_signatures
                ]
            else:
                candidate_trs = all_trs

        for tr in candidate_trs:
            th_cells = tr.css("th")
            td_cells = tr.css("td")
            if not headers and th_cells and not td_cells:
                headers = [_html_cell_text(c) for c in th_cells]
                header_signature = tuple(headers)
                continue
            cells = tr.css("th, td")
            if not cells:
                continue
            row_vals = [_html_cell_text(c) for c in cells]
            if header_signature is not None and tuple(row_vals) == header_signature:
                continue  # duplicate header row picked up by fallback iteration
            rows.append(row_vals)

        # If still no headers, use the first data row.
        if not headers and rows:
            headers = rows.pop(0)

        # Skip pathological empty tables.
        if not headers or not rows:
            continue

        # Apply spec caps.
        if len(headers) > _HTML_TABLE_MAX_COLS:
            warnings.append(
                f"html: tables[{len(tables)}] column count {len(headers)} exceeds spec cap {_HTML_TABLE_MAX_COLS} — truncated"
            )
            headers = headers[:_HTML_TABLE_MAX_COLS]
        n_cols = len(headers)

        rows_norm: list[list[str]] = []
        for r_idx, row in enumerate(rows):
            if len(rows_norm) >= _HTML_TABLE_MAX_ROWS:
                warnings.append(
                    f"html: tables[{len(tables)}] row count exceeded spec cap {_HTML_TABLE_MAX_ROWS} — truncated"
                )
                break
            if len(row) < n_cols:
                row = row + [""] * (n_cols - len(row))
            elif len(row) > n_cols:
                row = row[:n_cols]
            row = [_cap_html_cell(c, warnings, len(tables), r_idx, ci) for ci, c in enumerate(row)]
            rows_norm.append(row)

        # Cap headers cells too.
        headers = [_cap_html_cell(h, warnings, len(tables), -1, ci) for ci, h in enumerate(headers)]

        tables.append(
            Table(
                client_table_hint=f"html_dom_t{dom_idx}",
                title=None,
                headers=headers,
                rows=rows_norm,
                page=None,
                bbox=None,
            )
        )
    return tables


def _html_cell_text(cell) -> str:
    try:
        raw = cell.text(separator=" ", strip=True) or ""
    except Exception:
        raw = ""
    # Collapse internal whitespace to a single space; strip.
    return " ".join(raw.split())


def _cap_html_cell(value: str, warnings: list[str], t_idx: int, r_idx: int, c_idx: int) -> str:
    if len(value) <= _HTML_TABLE_CELL_MAX_CHARS:
        return value
    where = (
        f"tables[{t_idx}].headers[{c_idx}]" if r_idx < 0
        else f"tables[{t_idx}].rows[{r_idx}].[{c_idx}]"
    )
    warnings.append(f"html: {where} truncated at {_HTML_TABLE_CELL_MAX_CHARS} chars")
    return value[:_HTML_TABLE_CELL_MAX_CHARS]


def _extract_html_sections(html: str, canonical_text: str) -> list[Section]:
    """Flat list of sections derived from <h1>..<h6>, using text-level slicing.

    Walking selectolax's DOM sibling chain is fragile (node wrappers don't
    compare cleanly across calls and walks descend into elements at unexpected
    times). Instead we slice the **rendered text** at heading boundaries — the
    same logic the markdown extractor uses. Simpler, deterministic, no DOM
    identity hazards.

    Line coordinates are computed against ``canonical_text`` (== the
    ``content.text`` we return) so the consumer-facing retrieval formula
    resolves cleanly. Headings that don't appear in the canonical text
    (edge cases where canonicalization reflows whitespace) get `None`
    coords.
    """
    from selectolax.parser import HTMLParser

    # Render to text with explicit newlines so we can find heading positions.
    tree = HTMLParser(html)
    headings = tree.css("h1, h2, h3, h4, h5, h6")
    if not headings:
        return []

    body = tree.body
    if body is None:
        return []
    body_text = body.text(separator="\n")

    found: list[tuple[str, int, int]] = []  # (heading, level, start_index in body_text)
    cursor = 0
    for h in headings:
        ht = h.text(strip=True) or ""
        if not ht:
            continue
        level = int(h.tag[1])
        idx = body_text.find(ht, cursor)
        if idx < 0:
            continue
        found.append((ht, level, idx))
        cursor = idx + len(ht)

    sections: list[Section] = []
    canon_cursor = 0
    for i, (heading, level, start) in enumerate(found):
        text_start = start + len(heading)
        end = len(body_text)
        for j in range(i + 1, len(found)):
            if found[j][1] <= level:
                end = found[j][2]
                break
        section_text = canonicalize_text(body_text[text_start:end])

        # Line coords against canonical_text.
        line_start: int | None = None
        line_end: int | None = None
        hstart = canonical_text.find(heading, canon_cursor)
        if hstart >= 0:
            line_start = 1 + canonical_text.count("\n", 0, hstart)
            # End: next heading's position in canonical_text, or end-of-text.
            next_pos = len(canonical_text)
            for j in range(i + 1, len(found)):
                if found[j][1] <= level:
                    nh = found[j][0]
                    n = canonical_text.find(nh, hstart + len(heading))
                    if n >= 0:
                        next_pos = n
                    break
            line_end = 1 + canonical_text.count("\n", 0, max(next_pos - 1, hstart))
            canon_cursor = hstart + len(heading)

        sections.append(
            Section(
                heading=heading,
                level=level,
                text=section_text,
                line_start=line_start,
                line_end=line_end,
            )
        )
    return sections


class HtmlExtractor(IExtractor):
    """HTML5 / XHTML text + metadata extractor (lexbor backend)."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})
    name: ClassVar[str] = "html"

    def extract(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        limits: Limits | None = None,
        emit_markdown: bool = False,
        emit_sentences: bool = False,
    ) -> ExtractionResult:
        from collections import Counter

        from knovas_extract._metadata import finalize_warnings, sanitize_scalar

        limits = limits or Limits()
        if len(data) > limits.max_input_bytes:
            raise ResourceExhaustedError("input size", limits.max_input_bytes, observed=len(data))

        raw_text, charset = _decode(data)

        from selectolax.parser import HTMLParser

        try:
            tree = HTMLParser(raw_text)
        except Exception as exc:
            raise CorruptDocumentError(f"HTML parse failed: {exc}") from exc

        # Body text — drop scripts/styles before extracting visible text. We
        # use the regex on the raw input rather than relying on selectolax's
        # node walk because the regex stripping is deterministic and the test
        # corpus expects it.
        cleaned = _SCRIPT_STYLE.sub("", raw_text)
        body_tree = HTMLParser(cleaned)
        body = body_tree.body.text(separator="\n") if body_tree.body else ""
        text = canonicalize_text(body)

        if len(text.encode("utf-8")) > limits.max_text_bytes:
            raise ResourceExhaustedError("text size", limits.max_text_bytes, observed=len(text))

        meta_raw = _extract_html_metadata(tree)
        sections = _extract_html_sections(cleaned, text)

        warnings: list[str] = []
        counts: Counter[str] = Counter()

        extra: dict[str, str | int | float | bool | None] = {}
        # Map raw meta keys to `html:` extra keys — all values sanitized.
        _mapping = {
            "description": "html:description",
            "keywords": "html:keywords",
            "author": "html:author",
            "canonical": "html:canonical",
            "robots": "html:robots",
            "generator": "html:generator",
            "charset_declared": "html:charset_declared",
            "article_published_time": "html:article_published_time",
            "article_modified_time": "html:article_modified_time",
            "article_author": "html:article_author",
            "article_section": "html:article_section",
        }
        for src_key, dst_key in _mapping.items():
            raw_value = meta_raw.get(src_key)
            if raw_value is None:
                continue
            clean = sanitize_scalar(raw_value, limits=limits, counts=counts)
            if clean is not None:
                extra[dst_key] = clean
        # OG + Twitter — anything starting with og_ / twitter_.
        for k, v in meta_raw.items():
            if v is None:
                continue
            if k.startswith("og_"):
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[f"html:og:{k[3:]}"] = clean
            elif k.startswith("twitter_"):
                clean = sanitize_scalar(v, limits=limits, counts=counts)
                if clean is not None:
                    extra[f"html:twitter:{k[8:]}"] = clean

        # URL-drop warning (aggregated from _extract_html_metadata).
        dropped = meta_raw.get("_dropped_urls")
        if dropped:
            warnings.append(
                f"html: dropped {dropped} URL(s) with disallowed scheme from meta / link"
            )

        if charset:
            extra["html:charset_detected"] = charset
        finalize_warnings(counts, warnings)

        # Feed article:* into top-level Metadata.created / modified when the
        # existing slots are empty (never overwrite).
        created = extra.get("html:article_published_time")
        modified = extra.get("html:article_modified_time")

        metadata = Metadata(
            title=meta_raw.get("title"),
            author=meta_raw.get("author"),
            language=meta_raw.get("language"),
            created=str(created) if isinstance(created, str) else None,
            modified=str(modified) if isinstance(modified, str) else None,
            word_count=word_count(text),
            extra=extra,
        )

        # Markdown path: use the DOM-based sanitizer over the RAW input, not
        # the `cleaned` (regex-stripped) input. The regex only removes
        # <script>/<style>; the sanitizer also enforces the attr denylist,
        # URL scheme allowlist, image alt-only policy, and structural DoS
        # guards — all of which are required for hostile HTML.
        markdown: str | None = None
        if emit_markdown:
            from knovas_extract._markdown import check_expansion, html_to_markdown

            markdown = html_to_markdown(raw_text, limits, warnings=warnings)
            check_expansion(markdown, len(text), limits)

        sentences = None
        if emit_sentences:
            from knovas_extract._sentences import split_sentences

            sentences = split_sentences(text, limits, warnings=warnings, language=metadata.language)

        # Structured tables (spec 1.1.0+). Failures never break the extraction.
        try:
            tables = _extract_html_structured_tables(tree, warnings)
        except Exception as exc:
            tables = []
            warnings.append(f"html: structured table pass failed ({type(exc).__name__})")

        return make_result(
            text=text,
            mime="text/html",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            filename=filename,
            metadata=metadata,
            sections=sections or None,
            warnings=warnings or None,
            markdown=markdown,
            sentences=sentences,
            tables=tables or None,
        )


_inst = HtmlExtractor()
for _m in _inst.supported_mimes:
    MIME_REGISTRY[_m] = _inst
