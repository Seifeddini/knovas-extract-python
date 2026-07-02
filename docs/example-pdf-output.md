# Example — extracting a PDF

**Audience:** developers who want to see the full shape of an
`ExtractionResult` before wiring it into their pipeline.

The output below is captured live from `knovas-extract 0.1.3` /
`spec_version 1.2.0` on a synthetic 2-page PDF. Nothing here is
hand-edited or fabricated — everything you see is what your client
would receive from the same call.

---

## The input

A 2-page PDF with a title, two body paragraphs on page 1, a heading and
two body paragraphs on page 2, plus `Info`-dict metadata (title,
author, subject, keywords, producer). ~2 KiB on disk.

## The call

```python
from knovas_extract import extract

result = extract("sample_earnings.pdf", emit_sentences=True, emit_markdown=True)
```

Equivalent CLI:

```bash
knovas-extract sample_earnings.pdf --emit-sentences --emit-markdown --pretty
```

Both surface the same result as JSON via `result.to_dict()`.

## The output

```json
{
  "spec_version": "1.2.0",
  "source": {
    "mime_type": "application/pdf",
    "sha256": "dddc88e4437612d6c28aca246777c4208b72d36ad6a10d968d76af356fb0f7fd",
    "size_bytes": 2174,
    "filename": "sample_earnings.pdf",
    "path": "sample_earnings.pdf"
  },
  "metadata": {
    "title": "Q4 Earnings Report",
    "author": "Investor Relations",
    "language": null,
    "created": null,
    "modified": null,
    "page_count": 2,
    "word_count": 35,
    "extra": {
      "pdf:producer": "knovas-extract-python demo",
      "pdf:subject": "Quarterly financial results",
      "pdf:keywords": "earnings, Q4, revenue, guidance",
      "pdf:format": "PDF 1.7",
      "pdf:permissions": "print,modify,copy,annotate,form,accessibility,assemble,print_high_res",
      "pdf:outline_count": 0,
      "pdf:is_form_pdf": false
    }
  },
  "content": {
    "text": "Q4 Earnings Report\nRevenue grew 12% year-over-year. Operating margin\nexpanded to 22%. Free cash flow was 340M USD.\n\nOutlook\nThe board reaffirmed guidance. We expect continued\nstrength in the enterprise segment. Risks remain in EMEA.",
    "pages": [
      {
        "index": 0,
        "text": "Q4 Earnings Report\nRevenue grew 12% year-over-year. Operating margin\nexpanded to 22%. Free cash flow was 340M USD.",
        "line_start": 1,
        "line_end": 3
      },
      {
        "index": 1,
        "text": "Outlook\nThe board reaffirmed guidance. We expect continued\nstrength in the enterprise segment. Risks remain in EMEA.",
        "line_start": 5,
        "line_end": 7
      }
    ],
    "sections": null,
    "markdown": "# Q4 Earnings Report\n\nRevenue grew 12% year-over-year. Operating margin\nexpanded to 22%. Free cash flow was 340M USD.\n\n-----\n\n## Outlook\n\nThe board reaffirmed guidance. We expect continued\nstrength in the enterprise segment. Risks remain in EMEA.\n\n-----",
    "sentences": [
      {
        "index": 0,
        "text": "Q4 Earnings Report",
        "char_start": 0,
        "char_end": 18,
        "line_start": 1,
        "line_end": 1,
        "page_index": 0,
        "page_number": 1,
        "section_index": null
      },
      {
        "index": 1,
        "text": "Revenue grew 12% year-over-year.",
        "char_start": 19,
        "char_end": 51,
        "line_start": 2,
        "line_end": 2,
        "page_index": 0,
        "page_number": 1,
        "section_index": null
      },
      {
        "index": 2,
        "text": "Operating margin",
        "char_start": 52,
        "char_end": 68,
        "line_start": 2,
        "line_end": 2,
        "page_index": 0,
        "page_number": 1,
        "section_index": null
      },
      {
        "index": 3,
        "text": "expanded to 22%.",
        "char_start": 69,
        "char_end": 85,
        "line_start": 3,
        "line_end": 3,
        "page_index": 0,
        "page_number": 1,
        "section_index": null
      },
      {
        "index": 4,
        "text": "Free cash flow was 340M USD.",
        "char_start": 86,
        "char_end": 114,
        "line_start": 3,
        "line_end": 3,
        "page_index": 0,
        "page_number": 1,
        "section_index": null
      },
      {
        "index": 5,
        "text": "Outlook",
        "char_start": 116,
        "char_end": 123,
        "line_start": 5,
        "line_end": 5,
        "page_index": 1,
        "page_number": 2,
        "section_index": null
      },
      {
        "index": 6,
        "text": "The board reaffirmed guidance.",
        "char_start": 124,
        "char_end": 154,
        "line_start": 6,
        "line_end": 6,
        "page_index": 1,
        "page_number": 2,
        "section_index": null
      },
      {
        "index": 7,
        "text": "We expect continued",
        "char_start": 155,
        "char_end": 174,
        "line_start": 6,
        "line_end": 6,
        "page_index": 1,
        "page_number": 2,
        "section_index": null
      },
      {
        "index": 8,
        "text": "strength in the enterprise segment.",
        "char_start": 175,
        "char_end": 210,
        "line_start": 7,
        "line_end": 7,
        "page_index": 1,
        "page_number": 2,
        "section_index": null
      },
      {
        "index": 9,
        "text": "Risks remain in EMEA.",
        "char_start": 211,
        "char_end": 232,
        "line_start": 7,
        "line_end": 7,
        "page_index": 1,
        "page_number": 2,
        "section_index": null
      }
    ]
  },
  "warnings": [],
  "extractor": {
    "name": "knovas-extract-python",
    "version": "0.1.3"
  }
}
```

---

## Walk-through

Every section of the result has a specific role. Here's what each is
telling you and what you'd do with it.

### `source` — content-addressable identity

```json
{
  "mime_type": "application/pdf",
  "sha256": "dddc88e4437612d6c28aca246777c4208b72d36ad6a10d968d76af356fb0f7fd",
  "size_bytes": 2174,
  "filename": "sample_earnings.pdf",
  "path": "sample_earnings.pdf"
}
```

- **`sha256`** is the content hash of the input bytes. Store it in your
  index. If you re-extract later and the sha256 differs, the source
  document changed and any char offsets you cached are no longer valid.
- **`path`** is the caller-supplied source path, verbatim. Because we
  passed a path-like input, dispatch derived it from argv. It is
  **not** canonicalized (relative stays relative), **not** opened again,
  and **not** URL-resolved — it's citation metadata, not a filesystem
  handle. Validation ensures it's log-safe (no NUL, no ANSI escapes, no
  Trojan Source characters — see [metadata-and-paths.md](metadata-and-paths.md)).
- **`filename`** is the derived basename.

### `metadata` — first-class + `extra`

```json
{
  "title": "Q4 Earnings Report",
  "author": "Investor Relations",
  "page_count": 2,
  "word_count": 35,
  "extra": {
    "pdf:producer": "...",
    "pdf:permissions": "print,modify,copy,annotate,form,accessibility,assemble,print_high_res",
    "pdf:outline_count": 0,
    "pdf:is_form_pdf": false
  }
}
```

- Title / author come from the PDF `Info` dict. In production PDFs, XMP
  metadata (parsed via `defusedxml` with a `max_xmp_bytes` size cap)
  wins when present.
- **`pdf:permissions`** decodes the PDF permission bitmask into tokens.
  This particular PDF has no restrictions (all 8 flags set). A DRM'd
  PDF might report `"print,copy"` only.
- **`pdf:outline_count`** is `len(doc.get_toc())` — a cheap signal for
  chunk-aware ingestion.
- Every scalar in `extra` flows through `_metadata.sanitize_scalar`
  before storage — hostile document metadata (NUL bytes, control chars,
  bidi-override / Trojan Source, over-length) gets dropped or truncated
  with a counted, content-free warning in `warnings`.

For the full per-format `extra` reference, see
[metadata-and-paths.md](metadata-and-paths.md).

### `content.text` — the canonical string

```
Q4 Earnings Report\nRevenue grew 12% year-over-year. Operating margin\nexpanded to 22%. Free cash flow was 340M USD.\n\nOutlook\nThe board reaffirmed guidance. We expect continued\nstrength in the enterprise segment. Risks remain in EMEA.
```

This is the string every other coordinate resolves against. PDF pages
are joined with `\n\n` — the blank separator line is what makes page 2
start at line 5 (page 1's text ends at line 3, line 4 is blank, page 2
begins at line 5).

Line endings are always `\n` after canonicalization, regardless of
platform.

### `content.pages` — per-page granularity + line coords

```json
[
  {"index": 0, "text": "...", "line_start": 1, "line_end": 3},
  {"index": 1, "text": "...", "line_start": 5, "line_end": 7}
]
```

- **`index`** is 0-based (matches `Sentence.page_index`).
- **`line_start` / `line_end`** are 1-based indexes into
  `content.text`. Page 0 covers lines 1-3; page 1 covers lines 5-7
  (line 4 is the blank separator).

Only PDF populates `content.pages`. HTML, DOCX, EML, MSG, MD, RTF, TXT
have `pages: null`.

### `content.markdown` — sanitized whole-doc Markdown

```
# Q4 Earnings Report

Revenue grew 12% year-over-year. Operating margin
expanded to 22%. Free cash flow was 340M USD.

-----

## Outlook

The board reaffirmed guidance. We expect continued
strength in the enterprise segment. Risks remain in EMEA.

-----
```

Produced by `pymupdf4llm` and post-processed via
`_markdown.apply_url_allowlist` (which strips `javascript:`, `data:`,
`file:` URLs from any annotation-derived links). The `-----` are
`pymupdf4llm`'s page separators.

For hostile PDFs, annotation URLs with disallowed schemes would appear
as counted warnings like `"markdown: 2 javascript URLs dropped"` — the
scheme name is aggregated, never the payload.

### `content.sentences` — the citation coordinate

The 10 sentences carry everything you need to cite:

| # | Text | Page | Line | Char offset |
|---|---|---:|---:|---:|
| 0 | "Q4 Earnings Report" | 1 | 1 | 0..18 |
| 1 | "Revenue grew 12% year-over-year." | 1 | 2 | 19..51 |
| 2 | "Operating margin" | 1 | 2 | 52..68 |
| 3 | "expanded to 22%." | 1 | 3 | 69..85 |
| 4 | "Free cash flow was 340M USD." | 1 | 3 | 86..114 |
| 5 | "Outlook" | 2 | 5 | 116..123 |
| 6 | "The board reaffirmed guidance." | 2 | 6 | 124..154 |
| 7 | "We expect continued" | 2 | 6 | 155..174 |
| 8 | "strength in the enterprise segment." | 2 | 7 | 175..210 |
| 9 | "Risks remain in EMEA." | 2 | 7 | 211..232 |

Notice:

- Sentence 0 is the title line ("Q4 Earnings Report"). pysbd treats it
  as its own sentence because it ends the line with no continuation.
- Sentences 6 and 7 both land on line 6 because the line wraps
  mid-sentence in the PDF but pysbd correctly splits on the sentence
  boundary. The line window `[6, 6]` is the same for both; use
  `char_start`/`char_end` to disambiguate exactly.
- **Page↔sentence linkage is total**: sentences 0-4 all have
  `page_number = 1`; sentences 5-9 all have `page_number = 2`. Never
  fabricated for non-PDF formats — you can rely on `page_number` being
  `null` for HTML / DOCX / etc.
- **`section_index` is `null` everywhere** because this PDF has no
  extracted sections (`sections: null`). If the PDF had a TOC or you
  were extracting DOCX with headings, this would point to the innermost
  enclosing `Section`.

### `warnings` — always a list, never a string

```json
"warnings": []
```

Empty here because the PDF is clean. When present, warnings are always
**counted aggregates** — never the offending content. For example, a
hostile PDF might produce:

```json
[
  "PDF embedded JavaScript ignored (never executed)",
  "metadata: 3 values dropped for NUL / control / bidi-override characters",
  "pdf: xmp metadata exceeded max_xmp_bytes; skipped",
  "markdown: 2 javascript URLs dropped"
]
```

Each warning is safe to log, safe to display, safe to feed into a
metrics system. See [SECURITY.md](../SECURITY.md) promise #7.

### `extractor` — provenance

```json
{"name": "knovas-extract-python", "version": "0.1.3"}
```

Store this alongside your citations so you can invalidate cached char
offsets when the producer version changes and might tokenize differently.

---

## Verifying the invariants yourself

Every claim in `docs/citations.md` holds on this exact output. You can
paste this into a Python REPL:

```python
from knovas_extract import extract

r = extract("sample_earnings.pdf", emit_sentences=True, emit_markdown=True)

# Contract 1: exact retrieval.
for s in r.content.sentences:
    assert r.content.text[s.char_start : s.char_end] == s.text

# Contract 2: line-window retrieval.
lines = r.content.text.split("\n")
for s in r.content.sentences:
    window = "\n".join(lines[s.line_start - 1 : s.line_end])
    assert s.text in window

# Contract 3: non-overlapping ordering.
for i in range(1, len(r.content.sentences)):
    assert r.content.sentences[i].char_start >= r.content.sentences[i - 1].char_end

# Contract 4: monotonic 0-based index.
assert [s.index for s in r.content.sentences] == list(range(len(r.content.sentences)))

# Contract 5: page↔sentence linkage total (PDF has pages).
for s in r.content.sentences:
    assert s.page_index is not None
    assert s.page_number == s.page_index + 1

# Contract 6: determinism.
r2 = extract("sample_earnings.pdf", emit_sentences=True, emit_markdown=True)
assert r.to_dict() == r2.to_dict()

print("All contracts hold.")
```

Every one of these is also a test in `tests/unit/`. If any of them ever
regresses, the CI unit suite fails loudly.

---

## The citation shape a downstream index would store

Given the extraction above, one row per sentence in your retrieval
index might look like:

```python
citations = []
for s in r.content.sentences:
    citations.append({
        "sha256": r.source.sha256,
        "path": r.source.path,
        "spec_version": r.spec_version,
        "producer_version": r.extractor.version,
        "sentence_index": s.index,
        "char_range": [s.char_start, s.char_end],
        "line_range": [s.line_start, s.line_end],
        "page_number": s.page_number,
        "text": s.text,
    })
```

At query time you can:

1. **Render the citation** as `f"{path}, page {page_number}, line {line_start}"`.
2. **Re-fetch exact bytes** with `content.text[char_start:char_end]`
   after re-extracting (guarded by the sha256 match).
3. **Highlight** in a document viewer by mapping char offsets to the
   viewer's coordinate space.

That's the whole loop: extract → cite → embed → retrieve → re-cite. The
extraction step is what this library is for; everything else is your
application code.

---

## Cleaning up

The `sample_earnings.pdf` used for this example was generated with
`fitz` (PyMuPDF's low-level API); the source is preserved in
`tests/fixtures/` for reproducibility.

To rebuild the exact PDF:

```python
import fitz
doc = fitz.open()
p1 = doc.new_page()
p1.insert_text((72, 72),  "Q4 Earnings Report", fontsize=18)
p1.insert_text((72, 108), "Revenue grew 12% year-over-year. Operating margin", fontsize=11)
p1.insert_text((72, 122), "expanded to 22%. Free cash flow was 340M USD.", fontsize=11)
p2 = doc.new_page()
p2.insert_text((72, 72),  "Outlook", fontsize=14)
p2.insert_text((72, 108), "The board reaffirmed guidance. We expect continued", fontsize=11)
p2.insert_text((72, 122), "strength in the enterprise segment. Risks remain in EMEA.", fontsize=11)
doc.set_metadata({
    "title": "Q4 Earnings Report",
    "author": "Investor Relations",
    "subject": "Quarterly financial results",
    "keywords": "earnings, Q4, revenue, guidance",
    "producer": "knovas-extract-python demo",
})
doc.save("sample_earnings.pdf")
doc.close()
```

Rerun the extraction; the same output falls out byte-for-byte
(determinism contract).

---

## See also

- [citations.md](citations.md) — full contract reference (guarantees,
  retrieval formulas, chunk-and-cite recipe).
- [metadata-and-paths.md](metadata-and-paths.md) — every `<format>:<key>`
  in `Metadata.extra`, the `Source.path` policy, security posture.
- [../SECURITY.md](../SECURITY.md) — the security promises, including
  the sanitization contract that produced the counted-warnings example
  above.
