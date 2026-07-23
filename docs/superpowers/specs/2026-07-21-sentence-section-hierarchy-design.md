# Sentence → section hierarchy

**Date:** 2026-07-21
**Status:** approved design, not yet implemented
**Spec impact:** `spec_version` 1.3.0 → 1.4.0 (additive, non-breaking)
**Package impact:** `knovas-extract` 0.2.0 → 0.3.0 (default behaviour change)

---

## Problem

Clients consuming `knovas-extract` output need to know **which section and
subsection a sentence belongs to**, and to submit that context to the Knovas
API so it can be stored and used in search.

Today the output is one level short of that:

- `Sentence.section_index` already exists and resolves to the *innermost*
  enclosing section (`_sentences.py::attach_section_indices`, wired at
  `dispatch.py:366`). That part works.
- `content.sections` is a **flat** list carrying only `heading`, `level`, and a
  line window. Nothing records that section 4 nests under section 2, which
  nests under section 0.
- So a consumer receiving `section_index: 4` sees `"Scope"` at `level: 3` and
  cannot recover the trail `Introduction › Requirements › Scope` without
  re-deriving a heading stack itself — precisely the work clients are asking
  us to do for them.

Three further gaps compound it:

1. **PDF emits no sections at all.** `pdf.py` calls `doc.get_toc()` only to
   count entries (`pdf.py:532`); `sections` is always `None`, so every PDF
   sentence has `section_index: null`. For the dominant format, the feature
   would do nothing.
2. **DOCX section windows do not nest.** `md.py:102` and `html.py:415` close a
   section at the next heading of level **≤ current**, so a parent's window
   contains its children. `docx.py:281` closes at the next heading of **any**
   level, producing flat disjoint windows. Innermost lookup survives this by
   accident; any hierarchy derived from windows would be wrong for DOCX.
3. **EML / MSG** produce markdown from HTML bodies but never derive sections
   from it.

## Goals

- Every sentence exposes its full section ancestry, not just the innermost hit.
- The hierarchy is available for PDF, DOCX, HTML, Markdown, EML, and MSG.
- The payload stays proportionate: heading strings are stored **once per
  section**, never repeated per sentence.
- Additive schema change — a 1.3.0-shaped consumer keeps working.

## Non-goals

- Chunking. Sentences remain the atomic citable unit; grouping stays in the
  ingestion layer (`docs/citations.md` → "What sentences are not").
- Semantic section classification. Headings are taken as authored, not
  interpreted.
- Section support for TXT and RTF. Neither preserves headings; `striprtf`
  discards all structure. They keep `sections: null`.

---

## Design

### 1. `emit_markdown` becomes tri-state, defaulting to best-effort

Sections for PDF, EML, and MSG are derived from markdown, so markdown must be
produced by default. A naive `False → True` flip is unsafe: the optional-extras
layout exists for **license segregation** (`minimal` is the permissive-only
install path; `markdown` = markdownify; `pdf` pulls AGPL pymupdf). Flipping the
default hard would make `knovas-extract[html]` without `[markdown]` raise
`DependencyMissingError` on every HTML document, and would make `rtf.py:83`
append its "markdown requested but striprtf preserves no structure" warning to
every RTF result even though no caller asked.

Signature across `dispatch.extract`, `IExtractor.extract`, and all eight
extractor implementations:

```python
emit_markdown: bool | None = None
```

| Value | Behaviour |
|---|---|
| `None` *(new default)* | **Best-effort.** Emit markdown when the backend is installed. If it is missing, leave `content.markdown = null`, continue without raising, and add no warning. |
| `True` | **Required.** Today's explicit-opt-in semantics, unchanged: a missing backend raises `DependencyMissingError`, and RTF still warns. |
| `False` | Off. `content.markdown = null`, no backend import attempted. |

CLI gains `--no-markdown` (maps to `False`) alongside the existing
`--emit-markdown` (maps to `True`). Bare invocation gets `None`.

Rationale: markdown and sections arrive by default, no install profile
regresses, and callers who genuinely require markdown keep a way to say so.

### 2. New module — `src/knovas_extract/_sections.py`

`md.py:61 _extract_sections` moves here verbatim as the shared primitive. It
already does exactly what every other format needs: take markdown source plus
the canonical plain text, return `Section`s whose line coordinates are resolved
against `content.text`.

```python
def sections_from_markdown(markdown: str, canonical_text: str) -> list[Section]:
    """ATX-heading sections with line coords resolved against canonical_text."""

def attach_hierarchy(sections: list[Section] | None) -> None:
    """Fill parent_index + heading_path by walking a level stack."""
```

`sections_from_markdown` keeps the current `_extract_sections(body,
canonical_text)` signature — no `limits`. `max_sections` is enforced centrally
in dispatch instead (§7), because HTML and DOCX build sections through their own
paths and a cap inside this helper would silently not apply to them.

`attach_hierarchy` derives ancestry from `level` and document order — **not**
from line windows — because window semantics are not uniform across extractors
(see gap 2 above) and because a level stack degrades sanely when a document
skips levels (`h1` → `h3`).

```
stack: list[tuple[int, int]]        # (level, index)
for i, sec in enumerate(sections):
    while stack and stack[-1][0] >= sec.level:
        stack.pop()
    sec.parent_index = stack[-1][1] if stack else None
    sec.heading_path = (
        sections[sec.parent_index].heading_path + [sec.heading]
        if sec.parent_index is not None else [sec.heading]
    )
    stack.append((sec.level, i))
```

### 3. Per-format wiring

| Format | Change |
|---|---|
| **PDF** | **New.** Derive sections from the pymupdf4llm markdown via `sections_from_markdown`. pymupdf4llm infers ATX headings from font size. Line coords resolve against `content.text` by locating the heading string — the same `find()` approach md/html/docx already use. Biggest coverage win in this change. |
| **EML / MSG** | **New.** Derive sections from their markdown when the body was HTML. Plain-text-only bodies yield no sections. |
| **DOCX** | **Fix.** Add the missing `<= level` filter at `docx.py:281` so a section's window contains its subsections, matching md/html. Required for the hierarchy to be sound; also corrects `section_index` for a sentence sitting under an `h2` that is followed by an `h3`. |
| **HTML / MD** | Unchanged. Their DOM/ATX extraction is already level-correct and more reliable than a markdown round-trip. HTML keeps reading the DOM directly. |
| **TXT / RTF** | Unchanged. No headings to derive; `sections` stays `null`. |

### 4. Type changes

```python
@dataclass(slots=True)
class Section:
    heading: str
    level: int
    text: str
    line_start: int | None = None
    line_end: int | None = None
    parent_index: int | None = None                         # NEW
    heading_path: list[str] = field(default_factory=list)   # NEW

@dataclass(slots=True)
class Sentence:
    ...
    section_index: int | None = None
    section_path: list[int] | None = None                   # NEW
```

`section_path` is the materialized ancestor chain, root → innermost, ending in
`section_index`. It is `None` exactly when `section_index` is `None` — matching
the existing no-fabrication posture for `page_index` / `page_number`.

Materializing the path (rather than making consumers walk `parent_index`) is
what turns "every sentence under section 2" into a containment test on the
Knovas side instead of a recursive join.

Serialized shape:

```jsonc
"sections": [
  { "heading": "Scope", "level": 3, "text": "…",
    "line_start": 88, "line_end": 141,
    "parent_index": 2,
    "heading_path": ["Introduction", "Requirements", "Scope"] }
],
"sentences": [
  { "index": 87, "text": "…", "char_start": 4210, "char_end": 4295,
    "line_start": 92, "line_end": 92,
    "page_index": null, "page_number": null,
    "section_index": 4,
    "section_path": [0, 2, 4] }
]
```

### 5. Dispatch wiring

In `dispatch.extract`, after the extractor returns and markdown scrubbing runs:

1. **Whenever `content.sections` is non-empty** (not gated on `emit_sentences`,
   since sections are part of the base contract):
   - enforce `Limits.max_sections`;
   - `attach_hierarchy(result.content.sections)`;
   - `_assert_section_contracts(result)`.
2. **Additionally when `emit_sentences=True`** — unchanged from today:
   - `attach_section_indices(sentences, sections)`, extended to fill
     `section_path` alongside `section_index` by walking `parent_index` up from
     the innermost match;
   - `_assert_consumer_contracts(result)`.

The existing `_assert_consumer_contracts` runs only under `emit_sentences`, so
section-side invariants go in a separate `_assert_section_contracts` that runs
whenever sections exist. Otherwise a malformed hierarchy would go unchecked for
every caller who wants sections but not sentences.

### 6. Invariants

Raised as `RuntimeError` on violation, consistent with the existing
producer-bug posture.

**Sections** — in `_assert_section_contracts`
- `parent_index is None or 0 <= parent_index < i` — a parent always precedes
  its child in document order.
- `heading_path[-1] == heading`.
- `len(heading_path) == 1 + (0 if parent_index is None else len(sections[parent_index].heading_path))`.
- `parent_index is None or sections[parent_index].level < level`.

**Sentences** — in `_assert_consumer_contracts`
- `(section_path is None) == (section_index is None)`.
- `section_path[-1] == section_index`.
- `section_path` is strictly increasing, every element in `[0, len(sections))`.
- Levels strictly increase along the path.

### 7. Limits

New: `Limits.max_sections = 10_000`. Every other unbounded array in the result
has an explicit DoS cap (`max_sentences`, `max_pages`, `max_text_bytes`);
sections currently have none, and `heading_path` makes each section carry
O(depth) strings. Overflow raises `ResourceExhaustedError("section count", …)`,
matching the `max_sentences` pattern.

Enforced in dispatch (§5), immediately before `attach_hierarchy` — the single
chokepoint every extractor's sections pass through. Enforcing it inside
`sections_from_markdown` would miss HTML and DOCX, which build sections from
the DOM and mammoth HTML respectively.

### 8. Spec 1.4.0

`schema.json` sets `additionalProperties: false`, so the new fields must land in
`knovas-extract-spec` or conformant output stops validating.

- `content.sections[].parent_index` — `["integer", "null"]`, `minimum: 0`
- `content.sections[].heading_path` — `array` of `string`
- `content.sentences[].section_path` — `["array", "null"]` of `integer`,
  `minimum: 0`
- Description of `content.markdown` updated: null now also means "format has no
  structure to convert" (the RTF case, no longer warned about under the default).
- `MANIFEST.yaml::spec_version` → 1.4.0; both CHANGELOGs; `_version.py`
  `SPEC_VERSION = "1.4.0"`, `__version__ = "0.3.0"`.
- Corpus `*.expected.json` regenerated. The diff will show both the new section
  fields **and** newly-populated `content.markdown` from the default flip; it
  gets reviewed before commit, since these are the cross-language conformance
  fixtures.

---

## Testing

Following the existing convention that every documented contract has an
executable counterpart in `tests/unit/`.

**`tests/unit/test_section_hierarchy.py`** (new)
- Flat document — every section is root, `parent_index is None`,
  `heading_path == [heading]`.
- Nested `h1`/`h2`/`h3` — parents resolve, paths accumulate.
- Skipped level (`h1` → `h3`) — `h3` parents to the `h1`, path has 2 entries.
- Level *decrease* mid-document (`h3` → `h2`) — stack unwinds correctly.
- Document opening at `h3` — root at non-1 level is still a root.
- `max_sections` overflow raises `ResourceExhaustedError`.

**`tests/unit/test_sentence_contracts.py`** (extend)
- `section_path[-1] == section_index` across every section-bearing format.
- `section_path is None` iff `section_index is None`.
- Sentence before the first heading → both `None`.
- Round-trip: `ExtractionResult.from_dict(r.to_dict())` preserves both fields.

**Per-format**
- `test_extractors_pdf.py` — a PDF with headings yields non-null `sections`
  with correct hierarchy; sentences carry `section_path`.
- `test_extractors_docx.py` — regression test pinning the `<= level` window
  fix: a sentence under `h2` followed by `h3` resolves to the `h2`.
- `test_extractors_eml.py` / `_msg.py` — HTML-bodied mail yields sections;
  plain-text-bodied mail yields none.

**Default-flip regression**
- `content.markdown` is populated by default for TXT/MD/HTML/DOCX.
- RTF default extraction produces **no** markdown warning; `emit_markdown=True`
  still does.
- Missing backend under the `None` default degrades to `markdown: null` without
  raising; under `True` it still raises `DependencyMissingError`.

**Golden corpus** — `tests/golden/test_corpus.py` passes against the
regenerated fixtures and the 1.4.0 schema.

---

## Documentation

- `docs/citations.md` — `Sentence` dataclass listing, contract §6 (rewritten
  for the ancestry guarantee), the per-format fidelity table (PDF gains a ✅ in
  the `section_index` column), and the `cite()` recipe updated to emit
  `heading_path`.
- `docs/markdown-output.md` — the tri-state default.
- `knovas-extract-spec/docs/schema-fields.md` — the three new fields.
- `README.md` — quickstart reflects markdown-by-default.
- Both CHANGELOGs.

---

## Risks

| Risk | Mitigation |
|---|---|
| Default flip slows every PDF extraction (`to_markdown()` ≫ `get_text()`) | Accepted deliberately — it is the price of sections on the dominant format. `emit_markdown=False` restores the fast path and is documented. |
| pymupdf4llm heading inference is font-size heuristic, so PDF sections are fuzzier than DOCX/MD/HTML | Documented as such in the fidelity table. Line coords still resolve against `content.text` via exact string match, and a heading that cannot be located gets `None` coords rather than a fabricated window — the existing failure mode. |
| Corpus regeneration masks an unintended behaviour change | Diff reviewed before commit, per approval. |
| DOCX window fix changes existing `section_index` values | Intended — current values are wrong for nested headings. Called out explicitly in the CHANGELOG. |
