# Metadata reference + `Source.path`

**Audience:** developers who want to know exactly what each format
surfaces in `result.metadata`, how the `extra` namespace is structured,
and how the caller-supplied `Source.path` flows through the API.

**Status:** stable as of `spec_version = 1.2.0`. Every key listed here is
either produced today or explicitly documented as "only when the source
document provides it".

**Companion doc:** [citations.md](citations.md) covers sentences, line
numbers, and page coords.

---

## Top-level `Metadata`

```python
@dataclass
class Metadata:
    title: str | None
    author: str | None
    language: str | None       # BCP-47 (e.g. "en", "en-US")
    created: str | None        # ISO 8601
    modified: str | None       # ISO 8601
    page_count: int | None     # PDF only
    word_count: int | None     # derived, whitespace-split
    extra: dict[str, str | int | float | bool | None]
```

First-class fields (`title`, `author`, `language`, `created`, `modified`)
are populated from the format-specific "canonical" source when available:

| Format | Canonical source |
|---|---|
| PDF | XMP metadata (`dc:*`, `xmp:*`) > `Info` dict (`title`, `author`, `creationDate`, `modDate`). XMP wins when both non-empty. |
| DOCX | `docProps/core.xml` (`dc:title`, `dc:creator`, `dc:language`, `dcterms:created`, `dcterms:modified`). |
| HTML | `<title>` for title; `<meta name="author">` for author; `<html lang>` for language; `<meta property="article:published_time" / :modified_time>` for created/modified when set (never overwrites a non-null value). |
| Markdown | Frontmatter keys `title`, `author`, `language`, `created` (or `date`), `modified` — promoted verbatim. |
| EML | `Subject` → title; `From` → author; `Date` → created (RFC 2822 → ISO 8601 normalized). |
| MSG | `msg.subject`, `msg.sender`, `msg.date`. |
| TXT / RTF | None populated (no metadata surface). |

All values from hostile input flow through `_metadata.sanitize_scalar`
before being stored — see the [Security posture](#security-posture)
section below.

---

## The `extra` namespace

`extra` is a flat dict keyed as `<format>:<field>`. Values are scalars
only (`str | int | float | bool | None`). This keeps `extra` trivially
serializable to JSON without a nested schema per format. Nested
structures (e.g. Markdown frontmatter dicts) are JSON-serialized into a
single string, then sanitized.

### `pdf:` keys

| Key | Type | When populated | Source |
|---|---|---|---|
| `pdf:producer` | str | when `Info` has it | `Info.Producer` |
| `pdf:creator` | str | when `Info` has it | `Info.Creator` |
| `pdf:subject` | str | when `Info` has it | `Info.Subject` |
| `pdf:keywords` | str | when `Info` has it | `Info.Keywords` |
| `pdf:format` | str | when `Info` has it | PyMuPDF-reported format string |
| `pdf:xmp_description` | str | when XMP has `dc:description` | XMP |
| `pdf:xmp_creator_tool` | str | when XMP has `xmp:CreatorTool` | XMP |
| `pdf:pdfa_part` | str | when XMP declares PDF/A | XMP `pdfaid:part` |
| `pdf:pdf_version` | str | always (when PyMuPDF exposes it) | `doc.pdf_version()` |
| `pdf:permissions` | str | when the doc has non-default perms | Comma-joined tokens: `print,modify,copy,annotate,form,accessibility,assemble,print_high_res` |
| `pdf:outline_count` | int | always | `len(doc.get_toc())` |
| `pdf:is_form_pdf` | bool | always | `doc.is_form_pdf` |

**XMP is parsed via `defusedxml.ElementTree`** with a size cap of
`Limits.max_xmp_bytes` (1 MiB) applied **before** parse. A hostile
XMP block that exceeds the cap gets a counted warning `"pdf: xmp
metadata exceeded max_xmp_bytes; skipped"` and produces no XMP-derived
keys.

### `docx:` keys

Split between `docProps/core.xml` and `docProps/app.xml` — both parsed
via `defusedxml.ElementTree`.

From core.xml:

| Key | Type | Notes |
|---|---|---|
| `docx:subject` | str | |
| `docx:keywords` | str | |
| `docx:revision` | str | |
| `docx:last_modified_by` | str | |
| `docx:category` | str | |
| `docx:content_status` | str | Draft / In Review / Final |
| `docx:version` | str | Custom version tag, distinct from `revision` |

From app.xml (optional per OOXML spec):

| Key | Type | Notes |
|---|---|---|
| `docx:application` | str | e.g. "Microsoft Office Word" |
| `docx:app_version` | str | e.g. "16.0000" |
| `docx:template` | str | e.g. "Normal.dotm" |
| `docx:total_time` | str | minutes as a stringified int |
| `docx:pages_declared` | str | Author-declared page count, distinct from ours (we don't paginate DOCX) |
| `docx:paragraph_count` | str | |
| `docx:word_count_declared` | str | Author-declared, distinct from `word_count` (which we compute) |
| `docx:character_count` | str | |
| `docx:company` | str | |
| `docx:manager` | str | |

### `html:` keys

Extracted from `<head>` — every value routed through `sanitize_scalar`.

| Key | Type | Source |
|---|---|---|
| `html:description` | str | `<meta name="description">` |
| `html:keywords` | str | `<meta name="keywords">` |
| `html:author` | str | `<meta name="author">` |
| `html:canonical` | str | `<link rel="canonical" href>` — **only if scheme in allowlist** |
| `html:robots` | str | `<meta name="robots">` |
| `html:generator` | str | `<meta name="generator">` |
| `html:charset_declared` | str | `<meta charset>` or `<meta http-equiv="Content-Type">` |
| `html:charset_detected` | str | Encoding detected by chardet |
| `html:og:*` | str | Open Graph (`property="og:..."`). Fields: `title`, `description`, `url`, `type`, `site_name`, `image`. `og:url` and `og:image` gated by URL scheme allowlist. |
| `html:twitter:*` | str | Twitter Cards (accepts both `name="twitter:..."` and `property="twitter:..."`). Fields: `title`, `description`, `card`, `site`, `creator`. |
| `html:article_published_time` | str | `<meta property="article:published_time">` — also fed into `Metadata.created` when null |
| `html:article_modified_time` | str | `<meta property="article:modified_time">` — also fed into `Metadata.modified` when null |
| `html:article_author` | str | `<meta property="article:author">` |
| `html:article_section` | str | `<meta property="article:section">` |

**URL scheme allowlist**: values for `html:canonical`, `html:og:url`,
`html:og:image`, `html:twitter:url`, `html:twitter:image` are dropped
when the URL scheme is anything other than `http`, `https`, `mailto`,
`tel`. Drops are counted:

```
warnings: ["html: dropped 1 URL(s) with disallowed scheme from meta / link"]
```

`javascript:`, `data:`, `file:`, `vbscript:`, `blob:`,
`chrome-extension:`, and protocol-relative `//host` never appear in
`extra`.

### `eml:` keys

All extracted via stdlib `email` (RFC 5322-decoded), sanitized.

| Key | Type | Header |
|---|---|---|
| `eml:from` | str | `From` |
| `eml:to` | str | `To` |
| `eml:cc` | str | `Cc` |
| `eml:message_id` | str | `Message-ID` |
| `eml:reply_to` | str | `Reply-To` |
| `eml:sender` | str | `Sender` |
| `eml:in_reply_to` | str | `In-Reply-To` (thread chain) |
| `eml:references` | str | `References` (thread chain) |
| `eml:delivered_to` | str | `Delivered-To` |
| `eml:return_path` | str | `Return-Path` |
| `eml:list_id` | str | `List-Id` (mailing lists) |
| `eml:list_unsubscribe` | str | `List-Unsubscribe` |
| `eml:priority` | str | `X-Priority` (RFC-defined) |
| `eml:importance` | str | `Importance` (Outlook) |
| `eml:auth_results` | str | `Authentication-Results` (raw DKIM/SPF/DMARC; consumers parse) |
| `eml:content_language` | str | `Content-Language` |
| `eml:body_source` | str | `"text/plain"`, `"text/html"` — which body part fed `content.text` |
| `eml:has_attachments` | bool | `True` when any |
| `eml:attachment_count` | int | Non-zero when any |
| `eml:attachment_names` | str | Comma-joined; never the attachment bytes |

**Header injection**: a header containing raw CRLF still emits its
existing dedicated warning (`"Subject header contains embedded newline
(header-injection attempt)"`). Sanitizer drops the value from `extra`
under the same counted `metadata: ...` warning.

### `msg:` keys

Extracted via `extract-msg`. MSG attribute surface varies by variant —
missing fields simply don't appear.

| Key | Type | extract-msg attribute |
|---|---|---|
| `msg:from` | str | `msg.sender` |
| `msg:to` | str | Joined recipients |
| `msg:cc` | str | `msg.cc` |
| `msg:bcc` | str | `msg.bcc` |
| `msg:message_id` | str | `msg.messageId` |
| `msg:reply_to` | str | `msg.replyTo` |
| `msg:in_reply_to` | str | `msg.inReplyTo` |
| `msg:conversation_topic` | str | `msg.conversationTopic` |
| `msg:conversation_index` | str | `msg.conversationIndex` |
| `msg:categories` | str | `msg.categories` |
| `msg:sent_representing` | str | `msg.sentRepresentingName` or `Email` |
| `msg:importance` | int | 0=low / 1=normal / 2=high |
| `msg:sensitivity` | int | 0=normal / 1=personal / 2=private / 3=confidential |
| `msg:body_source` | str | `"text/plain"`, `"text/html"`, `"application/rtf"` |
| `msg:has_attachments` | bool | |
| `msg:attachment_count` | int | |
| `msg:attachment_names` | str | Comma-joined; never the payload bytes |

### `md:` keys

The known frontmatter keys `title`, `author`, `language`, `date`,
`created`, `modified` are promoted to first-class `Metadata` fields.
Every other frontmatter key lands under `md:frontmatter.<key>`
(lowercased) after `sanitize_scalar`:

```yaml
---
title: Q4 Earnings
author: Alice
tags: [finance, quarterly]
project: knovas
custom_field: value
---
```

produces:

```python
metadata.title == "Q4 Earnings"
metadata.author == "Alice"
metadata.extra["md:frontmatter.tags"] == '["finance", "quarterly"]'  # JSON-serialized list
metadata.extra["md:frontmatter.project"] == "knovas"
metadata.extra["md:frontmatter.custom_field"] == "value"
```

Nested dicts are JSON-serialized (with sorted keys for determinism);
lists are JSON-serialized; scalars pass through.

### `txt:` keys

| Key | Type | Notes |
|---|---|---|
| `txt:charset_detected` | str | Encoding chardet identified (or the BOM's declaration) |

### `rtf:` keys

None. `striprtf` discards RTF `\info` group metadata; extracting it
would require a second RTF parser and is out of scope for now.

---

## `Source`

```python
@dataclass
class Source:
    mime_type: str            # Canonicalized MIME (application/rtf, never text/rtf, etc.)
    sha256: str               # SHA-256 hex of the input bytes
    size_bytes: int           # Byte length of the input
    filename: str | None      # Basename derived from path or explicit filename kwarg
    path: str | None          # Caller-supplied path (verbatim after validation)
```

`sha256` is content-addressable — the same bytes always produce the same
hash. Use it to detect whether a stored citation is still valid (see
[citations.md](citations.md)).

### How `path` is populated

Two paths in, always the same field out:

```python
# 1. Automatic — dispatch derives path from a path-like input.
r = extract("reports/q4.pdf")
r.source.path       # "reports/q4.pdf" (verbatim caller form)
r.source.filename   # "q4.pdf"

# 2. Explicit — for bytes input.
data = open("reports/q4.pdf", "rb").read()
r = extract(data, mime="application/pdf", path="reports/q4.pdf")
r.source.path       # "reports/q4.pdf"

# 3. Neither.
r = extract(data, mime="application/pdf")
r.source.path       # None
```

Explicit `path=` **overrides** the argv-derived value when both are
present:

```python
r = extract("/tmp/actually_this.pdf", path="canonical/name.pdf")
r.source.path       # "canonical/name.pdf"
```

CLI:

```bash
knovas-extract reports/q4.pdf --pretty | jq '.source.path'
# "reports/q4.pdf"
```

### Validation

Before storage on `Source.path`, `_paths.validate_source_path` rejects
inputs that would corrupt logs / terminals / downstream renderers. See
the [Security posture](#security-posture) section below for the full
policy.

Rejections raise `ValueError` — caught by the CLI (exit code 2), thrown
to the caller in library use.

### What `Source.path` is not

- **Not a filesystem handle.** `knovas-extract` never opens it.
- **Not canonicalized.** No `os.path.realpath`; relative stays relative.
- **Not a URL.** No scheme filtering.
- **Not sanitized structurally.** Only character-safety validation.

Downstream consumers that use `Source.path` as a filesystem handle
must apply their own resolution and safety policy.

---

## Security posture

Every gap-filled scalar and every `Source.path` is a conduit for hostile
content from an untrusted document into consumer logs, terminals,
dashboards, and downstream renderers. Two chokepoints defuse this:

### `_paths.validate_source_path`

Applied to `Source.path` before storage. Rejects:

| Input | Reason |
|---|---|
| `\x00` | NUL byte truncates downstream C strings |
| ASCII control chars 0x00–0x1F (except `\t`) | Log injection (CRLF), terminal hijacking (ANSI escapes) |
| Unicode bidi override / isolate (U+202A..E, U+2066..9) | CVE-2021-42574 Trojan Source |
| Length > `Limits.max_path_length` (4096) | Path DoS |

Raises `ValueError` with a class-of-violation message. **The offending
payload never appears in the exception message** — re-logging is safe.

### `_metadata.sanitize_scalar`

Applied to every scalar added to `Metadata.extra`. Same character policy
as `Source.path` plus:

- **Length cap**: values over `Limits.max_metadata_value_length` (4096)
  are truncated. Counted warning.
- **Non-scalar → JSON-serialize**: nested dicts / lists are JSON-encoded
  with sorted keys (deterministic), then treated as strings under the
  same char policy.
- **Empty / whitespace-only** → dropped (returns `None`).

Rejections and truncations produce **counted, content-free warnings**:

```
"metadata: 3 values truncated"
"metadata: 2 values dropped for NUL / control / bidi-override characters"
"metadata: 1 values dropped as unserializable"
```

The warning message never contains the offending value. If a hostile
document tries to smuggle a payload via a metadata field, the payload is
dropped, the drop is counted, and the count is what appears in
`result.warnings`.

### PDF XMP size cap

Before `defusedxml` parses PDF XMP metadata, size is capped at
`Limits.max_xmp_bytes` (1 MiB). Oversized XMP produces the warning
`"pdf: xmp metadata exceeded max_xmp_bytes; skipped"` and no XMP-derived
keys appear in `extra`.

`defusedxml` itself disables external-entity fetching and DTD expansion
by default — the size cap is defense-in-depth for the CPU/memory hit of
even a "valid-but-huge" XMP block.

### XML parsing everywhere

Every XML parse in the library goes through `defusedxml.ElementTree`:

- DOCX `docProps/core.xml`
- DOCX `docProps/app.xml`
- PDF XMP metadata

Never stdlib `xml.etree`; never `lxml.etree.parse` directly (`python-docx`
uses lxml internally, and lxml's defaults disable external-entity
fetching — but the residual billion-laughs risk is bounded by our
decompression-ratio and per-entry size guards in the ZIP layer).

### Tuning caps for untrusted inputs

For pipelines processing user-supplied documents, tighten the defaults:

```python
from knovas_extract import extract, Limits

limits = Limits(
    max_input_bytes = 10 * 1024 * 1024,   # 10 MiB per doc
    max_text_bytes = 5 * 1024 * 1024,     # 5 MiB extracted text
    max_pages = 1_000,
    max_sentences = 10_000,
    max_metadata_value_length = 512,       # cap noisy metadata early
    max_xmp_bytes = 256 * 1024,            # 256 KiB XMP cap
    max_path_length = 1024,                # tighter than POSIX default
)
result = extract(data, limits=limits)
```

Everything above is enforced at the trust boundary. If you need
per-request auditing, `result.warnings` gives you the counted drop
totals without ever leaking the hostile payload.

---

## Further reading

- [citations.md](citations.md) — sentences, line numbers, page coords.
- [../SECURITY.md](../SECURITY.md) — the full "security promises"
  contract, including markdown emission and network isolation.
- `tests/unit/test_metadata_sanitization.py` — executable version of the
  sanitizer contract.
- `tests/unit/test_source_path_validation.py` — executable version of
  the path validation contract.
