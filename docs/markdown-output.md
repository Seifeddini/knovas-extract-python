# Markdown output

`extract(..., emit_markdown=True)` populates `content.markdown` with a whole-document Markdown rendering, sanitized against hostile input. Off by default so existing callers see no behavior or performance change.

- Spec: `spec_version = 1.1.0` (see `SPEC_VERSION` in `knovas_extract._version`).
- Scope: **whole-document only**. Per-page granularity stays in `content.pages[*].text`. See "Design decisions" below for why.
- Requires: the `[markdown]` extra (adds `markdownify`) for HTML-shaped inputs (HTML, DOCX, EML HTML alternative, MSG HTML body); PDF additionally requires `pymupdf4llm`, which ships in the `[pdf]` extra.

## Per-format fidelity

| Format | Backend | Emits Markdown? | Known losses |
|---|---|---|---|
| **HTML** | selectolax (DOM) + markdownify | Yes | Content of denylisted tags (`<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<applet>`, `<svg>`, `<math>`, `<link>`, `<meta>`, `<base>`, `<template>`, `<frame>`, `<frameset>`, `<noscript>`) is dropped. All event-handler attrs stripped. `<img>` → alt-text (URL never emitted). `<a href>` with disallowed schemes → plain text. |
| **DOCX** | mammoth (→ HTML) + shared sanitizer | Yes | Same sanitizer as HTML. Mammoth's style map controls heading/emphasis mapping; unusual custom styles may fall back to plain paragraphs. Numbered lists render as bullet lists (markdownify default). |
| **PDF** | pymupdf4llm | Yes | Whole-doc only (see below). Annotation URLs go through the shared URL allowlist post-pass. Scanned PDFs without OCR produce empty Markdown. Layout-heavy pages (columns, sidebars) may collapse; verify against your corpus. |
| **MD** | identity (frontmatter stripped) | Yes | None — the body already is Markdown. YAML frontmatter is lifted into `metadata` and does not appear in `content.markdown`. |
| **TXT** | identity | Yes | None — plain text is a valid Markdown source. |
| **EML** | HTML alternative → sanitizer; else identity | Yes when HTML alternative present | `cid:` / `mid:` inline references (attachments / linked content) fall through the URL allowlist and are rendered as plain link text with no href. |
| **MSG** | `.htmlBody` → sanitizer; else `.body` identity | Yes when HTML body present | Same as EML. When only an RTF body is present, `content.markdown` is `None` with a warning (striprtf preserves no structure). |
| **RTF** | (none) | **No — `content.markdown = None` + warning** | striprtf strips control words to plain text with no headings/lists/emphasis. Emitting the plain text as "markdown" would misrepresent the source; we honestly signal "no structure available." |

## Security contract

The `_markdown.html_to_markdown` helper is a **trust boundary**. Every extractor that produces Markdown from hostile input routes through it. See [`SECURITY.md` → Markdown emission](../SECURITY.md) for the normative statement. In summary:

- **Tag denylist** (stripped with their contents): `script`, `style`, `iframe`, `object`, `embed`, `applet`, `frame`, `frameset`, `noscript`, `svg`, `math`, `link`, `meta`, `base`, `template`. HTML comments and CDATA sections are removed before parsing.
- **Attribute denylist**: `style`, `srcset`, `formaction`, `background`, `ping`, `nonce`, `integrity`; every `on*` event handler; every colon-namespaced attribute except `xml:lang` and `xml:base`.
- **URL scheme allowlist** for `<a href>` / `<img src>`: `http`, `https`, `mailto`, `tel`. Anything else (including `javascript:`, `data:`, `vbscript:`, `file:`, `chrome-extension:`, `blob:`, protocol-relative `//host`, and relative paths) is unwrapped to plain text and counted.
- **Image policy**: `<img>` is unconditionally replaced with its `alt` text, even for benign `https://` URLs. Emitting `![](https://cdn/x)` would let downstream renderers beacon on render — a passive info-disclosure vector the plain-text output never had.
- **Structural DoS guards**: `Limits.max_recursion_depth` (DOM depth), `Limits.max_text_bytes` (post-conversion size), `Limits.max_markdown_expansion_ratio` (default `3.0` — the markdown-vs-text length ratio).
- **Warnings**: emitted as counted categories (e.g. `"markdown: 3 <script> tags stripped"`) — never content-verbatim.
- **Defense in depth**: `dispatch.extract` applies a final URL-allowlist scrub after every extractor returns, catching regressions in per-format markdown paths.

Consumers rendering `content.markdown` should still enable their renderer's HTML-passthrough safeguards. This sanitizer is defense-in-depth, not a replacement for the consumer's own escape/render policy.

## Design decisions

- **Opt-in default.** `emit_markdown=False` keeps behavior byte-identical to 1.0.0 for existing consumers and avoids the ~10–30% latency + memory cost when the field isn't needed.
- **Whole-document scope for PDF.** `pymupdf4llm.to_markdown(doc)` reconstructs headers, tables, and list continuations from cross-page context; stitching per-page results would be worse than one whole-doc call. Per-page Markdown is deferred; `content.pages[*].text` remains the per-page artifact.
- **Empty string ≠ null.** `content.markdown = ""` means "producer ran, no structure found." `content.markdown = None` means "not emitted." Consumers should distinguish these for retry logic.
- **RTF stays honest.** striprtf preserves no structure. Populating `content.markdown` with plain text would hide that fact from consumers; leaving `None` + emitting a warning gives them the choice.

## Recipes

```python
from knovas_extract import extract, Limits

# DOCX with headings, lists, emphasis:
r = extract("report.docx", emit_markdown=True)
print(r.content.markdown)

# Bytes input:
data = open("email.eml", "rb").read()
r = extract(data, mime="message/rfc822", emit_markdown=True)

# Tighter markdown expansion guard for LLM ingestion:
r = extract(
    "adversarial.html",
    emit_markdown=True,
    limits=Limits(max_markdown_expansion_ratio=1.5),
)

# CLI:
#   knovas-extract report.docx --emit-markdown --pretty
```
