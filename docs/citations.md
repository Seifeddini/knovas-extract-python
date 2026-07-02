# Citations, sentences, and coordinates

**Audience:** developers integrating `knovas-extract` output into a
downstream system (retrieval, RAG, review UI, provenance store) that
needs to cite extracted text back to the source document.

**Status:** stable contract as of `spec_version = 1.2.0`. Every guarantee
below is enforced by a post-condition in `dispatch.extract` or asserted
by a positive-assertion test in `tests/unit/`. If any of them ever
regresses, the CI unit suite fails loudly.

**tl;dr:** enable `emit_sentences=True`; each `Sentence` carries an
exact char slice, a human-readable line range, an optional page number,
and an optional section back-pointer. All coordinates resolve against
`result.content.text`.

---

## Enable

```python
from knovas_extract import extract

result = extract("report.pdf", emit_sentences=True)

for s in result.content.sentences:
    print(f"[p{s.page_number or '-'}:L{s.line_start}] {s.text}")
```

Requires the `[sentences]` extra:

```bash
pip install 'knovas-extract[sentences]'
```

The extra adds one dependency: `pysbd >= 0.3.4,<0.4` (MIT, pure-Python,
22 languages, no data download).

The `emit_sentences=False` default is preserved — existing code sees
zero behaviour or performance change.

---

## The Sentence type

```python
@dataclass
class Sentence:
    index: int                     # 0-based, monotonic within document
    text: str                      # canonicalized sentence text
    char_start: int                # 0-based char offset in content.text (inclusive)
    char_end: int                  # 0-based char offset in content.text (exclusive)
    line_start: int                # 1-based line in content.text (inclusive)
    line_end: int                  # 1-based line in content.text (inclusive)
    page_index: int | None         # 0-based; matches Page.index (PDF only)
    page_number: int | None        # 1-based; convenience (== page_index + 1)
    section_index: int | None      # 0-based; matches content.sections[i]
```

Each field is populated when meaningful and `None` when not — no
fabrication. The contracts below say exactly when each is populated.

---

## Retrieval — two ways

### Exact (machine-readable): use char offsets

```python
retrieved = result.content.text[s.char_start : s.char_end]
assert retrieved == s.text   # byte-for-byte
```

This is the **strongest** guarantee — no whitespace fudge, no line-window
ambiguity. Use it for highlight overlays, span offsets, or any case
where you need to reconstruct the sentence from `content.text` bytes.

### Human-readable citation: use line numbers

```python
lines = result.content.text.split("\n")
window = "\n".join(lines[s.line_start - 1 : s.line_end])
assert s.text in window   # sentence appears as substring
```

Lines are **1-based indexes into `content.text`** — the same string every
consumer receives. `content.text` uses `\n` line endings after
canonicalization (CRLF is normalized before line numbers are computed),
so the split is unambiguous across platforms.

For PDF, pages are joined with `\n\n` in `content.text`; the blank
separator lines are counted in the document-global line index. That is:

- A sentence on the first line of page 2 has `line_start ==
  last_line_of_page_1 + 2` (one line for the blank, one for the new page).
- **Line numbers are always document-global.** They never reset per page.
  This lets a single retrieval formula work for every format regardless
  of `page_index`.

For a human citation, prefer `page_number` (1-based, matches what a
reader sees in a PDF viewer):

```python
print(f"Cite: {result.source.path}, page {s.page_number}, line {s.line_start}")
```

For non-paginated formats (HTML, DOCX, TXT, MD, EML, MSG, RTF),
`page_number is None`; use `line_start` alone.

---

## Guaranteed contracts

Every one of these holds unconditionally when `emit_sentences=True` and
the extractor succeeded. Violation is a producer bug (raised as
`RuntimeError` from `dispatch.extract`, never silent).

### 1. Exact retrieval

```python
result.content.text[s.char_start : s.char_end] == s.text
```

### 2. Line-window retrieval

```python
window = "\n".join(result.content.text.split("\n")[s.line_start - 1 : s.line_end])
s.text in window
```

### 3. Ordering + non-overlap

```python
for i in range(1, len(result.content.sentences)):
    assert result.content.sentences[i].char_start >= result.content.sentences[i-1].char_end
```

Sentences appear in reading order. Adjacent sentences never overlap.

### 4. Monotonic index

```python
assert [s.index for s in result.content.sentences] == list(range(len(result.content.sentences)))
```

No gaps, no duplicates. Consumers can use `s.index` as a stable
per-document identifier.

### 5. Sentence↔page linkage — never partial

- When `result.content.pages is not None` (PDF today): **every** sentence
  has `page_index is not None` AND `page_number is not None` AND
  `page_number == page_index + 1`.
- When `result.content.pages is None` (every other format today): **every**
  sentence has `page_index is None` AND `page_number is None`. Never
  fabricated.

### 6. Sentence↔section linkage — bounded, innermost

- When a sentence's line window falls inside one or more section
  windows, `section_index` points to the **most specific** (smallest)
  enclosing section. Consumers doing "cite from Section 3.2" get the
  subsection, not the parent.
- When no section encloses the sentence (e.g. sentences before the first
  heading), `section_index is None`.
- When set, `0 <= section_index < len(result.content.sections)`
  unconditionally.

### 7. Empty input semantics

- `extract(b"", mime="text/plain", emit_sentences=True)` returns
  `content.sentences == []` — an **empty list**, not `None`.
- `extract(b"", mime="text/plain")` (default) returns `content.sentences
  is None`.

Consumers can therefore distinguish "opted-in and got nothing" from
"opted-out" without ambiguity.

### 8. Determinism

Running `extract(same_bytes, emit_sentences=True)` twice yields
byte-identical `content.sentences` — same `text`, `char_start`,
`char_end`, `line_start`, `line_end`, `page_index`, `page_number`,
`section_index`, `index`. No timestamps, no PIDs, no randomness anywhere
in the sentence path.

Because pysbd is version-pinned tight (`>=0.3.4,<0.4`), tokenization is
stable across `pip install` runs of the same version range too.

### 9. Backward compatibility

Consumers pinned to spec 1.1.0 still see valid results. All new fields
are additive; `to_dict()` output validates against the older schema
(unknown keys are ignored per JSON-Schema convention).

Gate on `result.spec_version >= "1.2.0"` if you need to detect support.

---

## `Source.path`

The caller-supplied source path. Populated in two ways:

```python
# 1. Automatic (from path-like input to extract):
r = extract("reports/q4.pdf")
r.source.path   # "reports/q4.pdf" (verbatim, relative stays relative)

# 2. Explicit (when input is bytes):
data = open("reports/q4.pdf", "rb").read()
r = extract(data, mime="application/pdf", path="reports/q4.pdf")
r.source.path   # "reports/q4.pdf"
```

### Validation (log-safe)

Before storage, `_paths.validate_source_path` rejects:

| Input | Reason | Reference |
|---|---|---|
| `"foo\x00bar"` | NUL byte truncates downstream C strings | — |
| `"foo\nbar"`, `"foo\rbar"`, `"foo\x1bbar"` | ASCII control character — used for log injection and terminal hijacking | — |
| `"foo‮bar"` and 8 other bidi override / isolate chars | Trojan Source attack — renders as something different than its bytes contain | CVE-2021-42574 |
| Length > `Limits.max_path_length` (default 4096) | Path DoS | POSIX PATH_MAX |

Rejections raise `ValueError` — a stdlib exception, distinct from our
`ExtractError` hierarchy (caller misuse ≠ document corruption). Error
messages describe the **class** of violation without including the
offending payload:

```python
try:
    extract(b"...", path="Trojan‮Source")
except ValueError as e:
    print(str(e))
    # "Source.path contains Unicode bidirectional-override character
    #  (CVE-2021-42574 Trojan Source)"
    # — no payload substring included, so re-logging is safe.
```

Tab (`\t`) is deliberately allowed.

### What validation does NOT do

- No `os.path.realpath` or canonicalization. Consumer-supplied form is
  preserved so absolute stays absolute and relative stays relative.
- No existence check.
- No file open, no filesystem access.
- No URL-scheme filtering (paths are not URLs).

`Source.path` is caller-supplied metadata, not a filesystem handle.
Consumers that pass it to `open()` or `requests.get()` downstream must
apply their own resolution/validation policy.

---

## Per-format fidelity

| Format | `sentences` | `page_*` | `line_*` | `section_index` | Notes |
|---|---|---|---|---|---|
| PDF | ✅ per-page tokenization, document-global coords | ✅ | ✅ | ⛔ (no sections) | Per-page tokenization stitched into document-global char/line offsets. |
| DOCX | ✅ | ⛔ (no pages) | ✅ | ✅ from mammoth-detected headings | Section coords computed against canonical `content.text`. |
| HTML | ✅ | ⛔ | ✅ | ✅ from `<h1>`..`<h6>` | Innermost enclosing section wins for nested headings. |
| Markdown | ✅ | ⛔ | ✅ | ✅ from ATX headings | Same nesting rule as HTML. |
| EML | ✅ | ⛔ | ✅ | ⛔ | Multipart/alternative: sentences tokenize the **plain-text** body (post-HTML-strip). |
| MSG | ✅ | ⛔ | ✅ | ⛔ | Same body-cascade as EML. |
| RTF | ✅ (from striprtf plain text) | ⛔ | ✅ | ⛔ | striprtf preserves no headings, so no sections. |
| TXT | ✅ | ⛔ | ✅ | ⛔ | |

`page_count` in `Metadata` reflects the input; only PDF populates
`content.pages`.

---

## The full citation recipe

Given an extracted result, build a fully-qualified citation tuple:

```python
def cite(result, s):
    """Return a stable citation for one sentence."""
    return {
        "sha256": result.source.sha256,        # content-addressable identity
        "path": result.source.path,            # human-facing path
        "spec_version": result.spec_version,   # producer contract version
        "sentence_index": s.index,             # per-document stable id
        "char_start": s.char_start,            # exact offset for retrieval
        "char_end": s.char_end,
        "line_start": s.line_start,            # human-readable coordinate
        "line_end": s.line_end,
        "page_number": s.page_number,          # human page cite (or None)
        "section_heading":
            result.content.sections[s.section_index].heading
            if s.section_index is not None else None,
        "text": s.text,                        # cached for offline display
    }
```

Store these citations in your retrieval index. To render at query time:

```python
r = extract(cite["path"])
assert r.source.sha256 == cite["sha256"], "document changed since indexed"

# Exact retrieval, guaranteed:
retrieved = r.content.text[cite["char_start"] : cite["char_end"]]
assert retrieved == cite["text"]
```

If the sha256 diverges, the source document has changed; the char
offsets from the old citation no longer apply. Re-extract and re-index.

---

## Multi-language notes

pysbd auto-selects a language rule set. Selection order:

1. `Metadata.language` (from HTML `<html lang="...">`, DOCX
   `dc:language`, PDF XMP `dc:language`, Markdown frontmatter
   `language:`, EML `Content-Language`).
2. If missing or unsupported by pysbd, fall back to English (`"en"`).

Supported languages (as of pysbd 0.3.4): en, hi, mr, zh, es, am, ar, hy,
bg, ur, ru, pl, fa, nl, da, fr, it, el, my, ja, de, kk, sk. `Metadata.language`
in the form `en-US` or `zh_CN` is normalized to the base code.

The choice is deterministic — same document, same language selection,
same sentence boundaries.

---

## Limits that apply to sentences

`Limits` fields you may want to tune:

| Field | Default | Applies to |
|---|---|---|
| `max_sentences` | 100,000 | Explicit DoS cap on the sentence array. Pathological many-short-sentence inputs raise `ResourceExhaustedError("sentence count", ...)`. |
| `max_text_bytes` | 50 MiB | Upstream cap on `content.text`. Also caps the input pysbd sees. |
| `max_path_length` | 4096 | `Source.path` length. POSIX PATH_MAX. |

Two more that are indirectly relevant (see [SECURITY.md](../SECURITY.md)):

| Field | Default | Applies to |
|---|---|---|
| `max_metadata_value_length` | 4096 | Per-scalar cap in `Metadata.extra` after sanitization. |
| `max_xmp_bytes` | 1 MiB | PDF XMP metadata size cap **before** `defusedxml` parses it. |

For untrusted-input pipelines, prefer smaller `max_text_bytes` (5–10 MiB)
to keep pysbd bounded on adversarial input; chunk larger documents
upstream.

---

## What sentences are **not**

- **Not chunks for retrieval.** Sentences are the atomic citable unit,
  not a chunk-sized window. Combine them into chunks in your ingestion
  layer if you need overlap, larger windows, or metadata-aware splitting.
- **Not HTML-safe.** `sentence.text` is a slice of `content.text`, which
  inherits the source document's untrusted posture. Consumers rendering
  sentences in a web UI must apply their own escape policy (defense in
  depth: our sanitizer is not a substitute for your renderer's escape).
- **Not pre-embedded.** `knovas-extract` never talks to an embedding
  model, never uploads, never hits the network. Embed in your own code.

---

## Consumer example: chunk-and-cite RAG index

Full-round-trip example:

```python
from knovas_extract import extract

def index_document(path: str, chunk_size: int = 3):
    r = extract(path, emit_sentences=True)
    if r.content.sentences is None:  # extractor failed to produce any
        return

    # Group into chunks of N sentences.
    for i in range(0, len(r.content.sentences), chunk_size):
        chunk = r.content.sentences[i : i + chunk_size]
        chunk_text = " ".join(s.text for s in chunk)
        section_headings = sorted({
            r.content.sections[s.section_index].heading
            for s in chunk
            if s.section_index is not None
        })

        yield {
            "text": chunk_text,
            "path": r.source.path,
            "sha256": r.source.sha256,
            "spec_version": r.spec_version,
            "sentence_range": (chunk[0].index, chunk[-1].index),
            "char_range": (chunk[0].char_start, chunk[-1].char_end),
            "line_range": (chunk[0].line_start, chunk[-1].line_end),
            "page_range": (chunk[0].page_number, chunk[-1].page_number),
            "section_headings": section_headings,
        }
```

Each returned chunk carries every coordinate needed to (a) embed and
retrieve the chunk, (b) render a human citation, and (c) reconstruct the
exact source span for provenance display.

---

## Further reading

- [SECURITY.md](../SECURITY.md) — the sanitization + limits posture for
  metadata, XMP, and paths.
- [README.md](../README.md) — the quickstart, with `emit_sentences=True`
  in context.
- `tests/unit/test_sentence_contracts.py` — the executable version of
  the contracts on this page. If you're evaluating whether to depend on
  a contract, read the test.
