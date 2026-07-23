"""HTML → Markdown conversion with sanitization (internal).

This module is the **single trust boundary** for producing Markdown output
from hostile inputs. Every extractor that emits markdown routes through
`html_to_markdown` (for HTML-shaped intermediates) or `apply_url_allowlist`
(as a defence-in-depth pass for backends that emit markdown directly, e.g.
`pymupdf4llm`).

Security posture (see SECURITY.md → "Markdown emission"):

- Denylisted tags (`script`, `style`, `iframe`, `object`, `embed`, `applet`,
  `frame`, `frameset`, `noscript`, `svg`, `math`, `link`, `meta`, `base`,
  `template`) are stripped **with their contents** — not merely unwrapped.
- HTML comments (`<!-- ... -->`) and CDATA sections (`<![CDATA[ ... ]]>`)
  are stripped before parsing. IE conditional comments hide payloads inside
  ordinary comments; both regex classes catch them.
- Denylisted attributes (`style`, `srcset`, `formaction`, `background`,
  `ping`, `nonce`, `integrity`), event handlers (`on*`), and any
  colon-namespaced attribute (except `xml:lang` / `xml:base`) are dropped.
- `<a href>` URLs are gated by an allowlist (`http`, `https`, `mailto`,
  `tel`). Disallowed schemes (`javascript:`, `data:`, `vbscript:`, `file:`,
  `chrome-extension:`, `blob:`), protocol-relative URLs (`//attacker`),
  and relative URLs are removed — the anchor is replaced with its plain
  text. Aggregated per-scheme warning count is appended.
- `<img>` is unconditionally replaced with its `alt` text. Even a benign
  `https://` image URL is stripped because emitting `![](url)` would let
  downstream renderers beacon on render (passive info-disclosure / SSRF).
- Structural DoS is bounded by `Limits.max_recursion_depth` (DOM depth),
  `Limits.max_text_bytes` (post-conversion size), and
  `Limits.max_markdown_expansion_ratio` (markdown / plain-text length).

The sanitizer is **deterministic** — no randomization, no timestamps —
so golden tests can pin exact markdown output byte-for-byte.

Warnings emitted here are **counted, not content**: e.g.
`"markdown: 3 <script> tags stripped"`. The exact per-payload strings are
never surfaced (would violate SECURITY.md promise #7, "no telemetry that
reveals document content").
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from knovas_extract.errors import DependencyMissingError, ResourceExhaustedError
from knovas_extract.normalize import canonicalize_text
from knovas_extract.result import Limits

# --- Security policy constants ---------------------------------------------

# Tags stripped ENTIRELY (contents discarded, not unwrapped).
_TAG_DENYLIST: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "applet",
        "frame",
        "frameset",
        "noscript",
        "svg",
        "math",
        "link",
        "meta",
        "base",
        "template",
    }
)

# Attributes removed from every element they appear on.
_ATTR_DENYLIST_EXACT: frozenset[str] = frozenset(
    {
        "style",
        "srcset",
        "formaction",
        "background",
        "ping",
        "nonce",
        "integrity",
    }
)

# URL schemes we're willing to emit as clickable markdown links.
_URL_SCHEME_ALLOWLIST: frozenset[str] = frozenset({"http", "https", "mailto", "tel"})

# Colon-namespaced attributes we treat as safe. Every other `foo:bar`
# attribute (e.g. `xlink:href`, `formaction:...`) is stripped.
_ATTR_COLON_ALLOWLIST: frozenset[str] = frozenset({"xml:lang", "xml:base"})


# HTML comments and CDATA — stripped before parsing so IE conditional
# comments (`<!--[if IE]><script>…</script><![endif]-->`) never reach the
# DOM. The regex is non-greedy across lines.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)

# Scheme extractor. RFC 3986: scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

# Markdown link / image *opening* only: `[text](` or `![alt](`. The
# closing `)` and URL are extracted by a depth-tracking walk (see
# `_find_close_paren`) so hostile URLs like `javascript:alert(1)` — which
# contain an inner `(` — are consumed as a single unit rather than
# truncated. Kept intentionally narrow (no nested brackets, no
# title-arg with unmatched quotes); the primary sanitizer in
# `html_to_markdown` does the structured job.
_MD_LINK_OPEN_RE = re.compile(r"(!?)\[([^\]]*)\]\(")


# --- Lazy backend imports ---------------------------------------------------


def _load_selectolax() -> Any:
    try:
        from selectolax.parser import HTMLParser
    except ImportError as exc:
        raise DependencyMissingError("markdown", "selectolax") from exc
    return HTMLParser


def _load_markdownify() -> Any:
    try:
        import markdownify
    except ImportError as exc:
        raise DependencyMissingError("markdown", "markdownify") from exc
    return markdownify


# --- Internals -------------------------------------------------------------


def _url_scheme(url: str) -> str:
    """Return lowercased URL scheme, or empty string.

    An empty return value covers: empty href, relative paths (`../x`),
    fragment-only (`#foo`), and protocol-relative (`//host`) — all of which
    we treat as "no valid scheme" and therefore reject.
    """
    u = url.strip()
    if not u or u.startswith("//"):
        return ""
    m = _SCHEME_RE.match(u)
    return m.group(1).lower() if m else ""


def _check_depth(root: Any, max_depth: int) -> None:
    """Raise ResourceExhaustedError if any subtree exceeds max_depth.

    Iterative DFS on selectolax's (child, next) linked-tree so we don't
    consume Python's recursion budget on adversarial input.
    """
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise ResourceExhaustedError("html nesting depth", max_depth, observed=depth)
        child = getattr(node, "child", None)
        while child is not None:
            stack.append((child, depth + 1))
            child = getattr(child, "next", None)


def _sanitize_dom(parser: Any) -> dict[str, int]:
    """Apply the security contract to the parsed DOM in place.

    Returns a dict of {warning_category: count} suitable for aggregation.
    """
    counts: dict[str, int] = {}

    # 1) Kill denylisted tags with their contents. A node whose parent
    # was already decomposed can raise on the second call — suppress; the
    # end state is the same either way.
    for tag in _TAG_DENYLIST:
        matches = parser.css(tag)
        n = 0
        for node in matches:
            with contextlib.suppress(Exception):
                node.decompose()
                n += 1
        if n:
            counts[f"{tag}_tags_stripped"] = n

    # 2) Attribute scrubbing across the remaining tree.
    attr_drops: dict[str, int] = {}
    for node in parser.css("*"):
        attrs = getattr(node, "attributes", None)
        if not attrs:
            continue
        # Copy keys to avoid mutation during iteration.
        for name in list(attrs.keys()):
            if _is_attr_forbidden(name):
                _drop_attr(node, name)
                attr_drops[name.lower()] = attr_drops.get(name.lower(), 0) + 1
    for name, count in attr_drops.items():
        if name.startswith("on"):
            counts["event_handler_attrs_dropped"] = (
                counts.get("event_handler_attrs_dropped", 0) + count
            )
        else:
            counts[f"attr_{name}_dropped"] = count

    # 3) <a href> URL scheme allowlist. Anchor with disallowed scheme is
    # unwrapped (replaced with its own text).
    scheme_drops: dict[str, int] = {}
    for a in parser.css("a"):
        href = (a.attributes.get("href") or "") if a.attributes else ""
        scheme = _url_scheme(href)
        if scheme in _URL_SCHEME_ALLOWLIST:
            continue
        # Also refuse path-traversal in relative URLs — even though we're
        # about to unwrap, be explicit for parity with the docstring.
        text = a.text() or ""
        try:
            a.replace_with(text)
        except Exception:
            # Fallback: strip href attribute so markdownify emits plain text.
            _drop_attr(a, "href")
            continue
        # Bucket the reason for the warning.
        bucket = scheme if scheme else "relative_or_missing"
        scheme_drops[bucket] = scheme_drops.get(bucket, 0) + 1
    for scheme, count in scheme_drops.items():
        counts[f"{scheme}_URLs_dropped"] = count

    # 4) <img> — replace with alt text, always. Even http/https images are
    # stripped because rendering the emitted `![](url)` on any downstream
    # markdown renderer would beacon back to the source host.
    img_replacements = 0
    for img in parser.css("img"):
        alt = (img.attributes.get("alt") or "") if img.attributes else ""
        try:
            img.replace_with(alt)
        except Exception:
            _drop_attr(img, "src")
            continue
        img_replacements += 1
    if img_replacements:
        counts["img_replaced_with_alt_text"] = img_replacements

    return counts


def _is_attr_forbidden(name: str) -> bool:
    """Return True if the attribute should be dropped from every element."""
    n = name.lower()
    if n.startswith("on"):
        return True
    if n in _ATTR_DENYLIST_EXACT:
        return True
    return ":" in n and n not in _ATTR_COLON_ALLOWLIST


def _drop_attr(node: Any, name: str) -> None:
    """Best-effort attribute removal across selectolax versions."""
    # Newer selectolax supports `del node.attributes[name]`.
    try:
        del node.attributes[name]
        return
    except (KeyError, TypeError, AttributeError):
        pass
    # Some versions expose strip_attribute_by_name / attrs. Best-effort —
    # if the API isn't there, the attr will have been stripped upstream
    # or is genuinely unremovable at this selectolax version.
    with contextlib.suppress(Exception):
        node.attributes[name] = None


def _emit_warnings(counts: dict[str, int], warnings: list[str]) -> None:
    """Append aggregated, content-free warnings in a deterministic order."""
    # Sorted for byte-stable golden tests.
    for key in sorted(counts):
        count = counts[key]
        if count <= 0:
            continue
        # Human-readable category name from the internal key.
        if key.endswith("_tags_stripped"):
            tag = key[: -len("_tags_stripped")]
            warnings.append(f"markdown: {count} <{tag}> tags stripped")
        elif key == "event_handler_attrs_dropped":
            warnings.append(f"markdown: {count} on* event-handler attrs dropped")
        elif key.startswith("attr_") and key.endswith("_dropped"):
            attr = key[len("attr_") : -len("_dropped")]
            warnings.append(f"markdown: {count} {attr}= attrs dropped")
        elif key.endswith("_URLs_dropped"):
            scheme = key[: -len("_URLs_dropped")]
            warnings.append(f"markdown: {count} {scheme} URLs dropped")
        elif key == "img_replaced_with_alt_text":
            warnings.append(f"markdown: {count} <img> replaced with alt-text")
        elif key.endswith("_comments_stripped"):
            kind = key[: -len("_comments_stripped")]
            warnings.append(f"markdown: {count} {kind} comments stripped")
        else:
            # Should not happen — keep the raw counter as a fallback.
            warnings.append(f"markdown: {count} {key}")


def check_expansion(md: str, plain_text_len: int, limits: Limits) -> None:
    """Raise if the markdown output is disproportionately larger than the text.

    Bypassed when `plain_text_len == 0` (division by zero, and there's
    nothing to compare against — the size cap alone is the guard).
    """
    if plain_text_len <= 0:
        return
    ratio = len(md) / plain_text_len
    if ratio > limits.max_markdown_expansion_ratio:
        raise ResourceExhaustedError(
            "markdown expansion ratio",
            limits.max_markdown_expansion_ratio,
            observed=ratio,
        )


# --- Public API ------------------------------------------------------------


def html_to_markdown(
    html: str,
    limits: Limits,
    *,
    warnings: list[str],
) -> str:
    """Convert HTML to sanitized Markdown.

    The DOM is sanitized before conversion; the resulting markdown is size-
    and expansion-ratio-bounded and canonicalized. See the module docstring
    for the full security contract.

    Args:
        html: Raw HTML string.
        limits: Resource caps.
        warnings: List to which counted, content-free warnings are appended.

    Returns:
        Canonicalized markdown string. May be empty when the input has no
        renderable content after sanitization.

    Raises:
        DependencyMissingError: selectolax or markdownify not installed.
        ResourceExhaustedError: DOM depth / markdown size / expansion ratio.
    """
    HTMLParser = _load_selectolax()
    markdownify = _load_markdownify()

    # 1) Pre-parse comment / CDATA scrub. These are the delivery vehicles
    # for IE-conditional payloads and would otherwise disappear silently
    # into the DOM without a warning trail.
    counts: dict[str, int] = {}
    original = html
    stripped, n_c = _COMMENT_RE.subn("", original)
    if n_c:
        counts["html_comments_stripped"] = n_c
    stripped, n_x = _CDATA_RE.subn("", stripped)
    if n_x:
        counts["cdata_comments_stripped"] = n_x

    # 2) Parse.
    parser = HTMLParser(stripped)

    # 3) Structural DoS guard: DOM depth.
    _check_depth(parser.root or parser, limits.max_recursion_depth)

    # 4) Sanitize the DOM.
    sanitizer_counts = _sanitize_dom(parser)
    counts.update(sanitizer_counts)

    # 5) Serialize sanitized DOM back to HTML for markdownify. Fall back to
    # the raw stripped input on any serialization glitch — the sanitizer
    # already removed the dangerous content in place.
    try:
        clean_html = parser.html or ""
    except Exception:
        clean_html = stripped

    # 6) Convert. `strip` is a defense-in-depth belt: if a denylisted tag
    # somehow survived DOM removal (parser quirk), markdownify still elides
    # it. `heading_style="ATX"` yields `# heading` (byte-stable). Bullets
    # forced to `-` for the same reason.
    md_raw = markdownify.markdownify(
        clean_html,
        heading_style="ATX",
        bullets="-",
        strip=sorted(_TAG_DENYLIST),
    )

    # 7) Canonicalize whitespace / NFC — same rules as `content.text`.
    md = canonicalize_text(md_raw or "")

    # 8) Size cap.
    if len(md.encode("utf-8")) > limits.max_text_bytes:
        raise ResourceExhaustedError("markdown size", limits.max_text_bytes, observed=len(md))

    # 9) Emit warnings deterministically.
    _emit_warnings(counts, warnings)

    return md


def _find_close_paren(md: str, start: int) -> int:
    """Return index of the balanced `)` for a URL that begins at `start`.

    Tracks nesting so hostile URLs like `javascript:alert(1)` — which
    contain an unbalanced `(` inside — are consumed as a single unit
    rather than truncated at the first `)`.

    Returns -1 when no matching close is found before end-of-string or a
    whitespace/newline break (markdown URLs cannot span lines).
    """
    depth = 1
    i = start
    n = len(md)
    while i < n:
        ch = md[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        elif ch in ("\n", "\r"):
            return -1
        i += 1
    return -1


def apply_url_allowlist(md: str, *, warnings: list[str]) -> str:
    """Defence-in-depth scrub of markdown link / image URLs.

    Used by:
    - PDF path after `pymupdf4llm.to_markdown` (PDF `/URI` annotations can
      contain `javascript:` / `file:` URLs).
    - `dispatch.extract` final pass, regardless of extractor.

    Replaces `[text](scheme:…)` and `![alt](scheme:…)` with plain text /
    alt-text when the scheme is not in the allowlist. Counts drops per
    scheme; appends aggregated warnings.

    Handles nested `(` inside the URL by depth-counting — hostile URLs
    like `javascript:alert(1)` are consumed as a single unit rather than
    truncated at the first inner `)`.

    Scope: this pass matches only inline `[text](url)` / `![alt](url)` syntax.
    Angle-bracket autolinks (`<javascript:...>`) and reference-style links
    (`[text][id]` + a separate `[id]: url` definition) are intentionally NOT
    rewritten — matching them generically would corrupt legitimate content
    (a bare `<http://…>` autolink, `<` in code spans) for no real gain: the
    only markdown producers we feed here (`markdownify` over a DOM-sanitized
    tree, and `pymupdf4llm`) emit neither form. For HTML-shaped inputs the
    authoritative defense is the DOM URL-allowlist in `_sanitize_dom`; this
    function is the defence-in-depth pass for direct-markdown backends.
    """
    if not md:
        return md

    scheme_drops: dict[str, int] = {}
    img_drops = 0
    out: list[str] = []
    cursor = 0

    for m in _MD_LINK_OPEN_RE.finditer(md):
        # Preserve anything before this match verbatim.
        if m.start() > cursor:
            out.append(md[cursor : m.start()])
        cursor = m.start()

        is_image = m.group(1) == "!"
        text = m.group(2) or ""
        # m.end() lands right after `(`, at the first char of the URL body.
        url_start = m.end()

        close = _find_close_paren(md, url_start)
        if close < 0:
            # Not a valid link — passthrough verbatim.
            out.append(md[cursor : m.end()])
            cursor = m.end()
            continue

        url_body = md[url_start:close].strip()
        # Peel off an optional title arg `"..."` at the tail (markdown
        # syntax); leave only the URL for scheme inspection.
        url_only = url_body.split(None, 1)[0] if url_body else ""

        scheme = _url_scheme(url_only)
        if scheme in _URL_SCHEME_ALLOWLIST and not is_image:
            # Allowed link — emit verbatim.
            out.append(md[m.start() : close + 1])
        else:
            if is_image:
                img_drops += 1
            else:
                bucket = scheme if scheme else "relative_or_missing"
                scheme_drops[bucket] = scheme_drops.get(bucket, 0) + 1
            out.append(text)
        cursor = close + 1

    if cursor < len(md):
        out.append(md[cursor:])
    cleaned = "".join(out)

    counts: dict[str, int] = {}
    for scheme, count in scheme_drops.items():
        counts[f"{scheme}_URLs_dropped"] = count
    if img_drops:
        counts["img_replaced_with_alt_text"] = img_drops
    _emit_warnings(counts, warnings)

    return cleaned
