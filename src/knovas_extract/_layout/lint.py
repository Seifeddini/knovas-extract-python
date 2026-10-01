"""Row lint (plan R8, decision D12) and the output invariants (plan R10).

The lint is applied **only** to lines the renderer itself classified as table
header / row / key-value lines — never by substring detection, so a prose
paragraph containing ``": "`` on a structured page is emitted exactly as its
words read.

Server facts the lint protects against (``information_object_manager.py``):
``_SENT_BOUNDARY = (?<=[.!?])\\s+(?=[A-ZÜÖÄ\\d"'(])`` splits inside a line, so
``Fr. 1'200`` would be cut between label and amount; chunks > 1800 chars are
hard-split; embed pieces are capped at 248 tokens before ``hc: `` is prepended.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

#: The server's sentence boundary (``_SENT_BOUNDARY``), reproduced verbatim.
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÜÖÄ\d\"'(])")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_WS = re.compile(r"[ \t  ]+")  # noqa: RUF001


def est_tokens(s: str) -> int:
    """Cheap Qwen3-tokenizer estimate (no tokenizer shipped in the client): a
    least-squares fit on renderer output against the real ``knovas_embedding_v1``
    tokenizer (MAPE ≈ 7 %) — digits and punctuation cost ≈ 1 token each."""
    d = p = a = 0
    for c in s:
        if c.isdigit():
            d += 1
        elif c.isalpha():
            a += 1
        elif not c.isspace():
            p += 1
    return int(math.ceil(1.07 * d + 1.07 * p + 0.24 * a + 0.49 * len(s.split())))


def clean_cell(s: str) -> str:
    """A cell never contains the separator or a tab; whitespace is collapsed."""
    return _WS.sub(" ", s.replace("|", "/").replace("\n", " ")).strip()


def lint_row_line(line: str, *, sentence_guard: bool = True) -> str:
    """Lint one renderer-classified row/header/kv line: no tabs, no server
    sentence boundary inside the line (the whitespace after an abbreviation such
    as ``Fr.``/``Ziff.`` is removed; digits are never altered)."""
    line = _WS.sub(" ", line.replace("\t", " ")).strip()
    if sentence_guard:
        line = SENT_SPLIT_RE.sub("", line)
    return line


def split_long_row(
    label: str,
    cells: Sequence[str],
    *,
    max_tokens: int = 200,
    max_chars: int = 1800,
    cont: str = " (Forts.)",
) -> list[str]:
    """Join ``label`` and ``cells`` with `` | ``; when the line exceeds the budget,
    split it into continuation lines that repeat the label plus ``(Forts.)``.
    An over-long single cell is split on word boundaries."""

    def fits(s: str) -> bool:
        return est_tokens(s) <= max_tokens and len(s) <= max_chars

    full = " | ".join(([label] if label else []) + list(cells))
    if fits(full):
        return [full] if full else []
    prefix_cont = (label + cont) if label else cont.strip()
    pieces: list[str] = []
    for cell in cells:  # explode cells that do not fit next to the continuation prefix
        if fits(f"{prefix_cont} | {cell}"):
            pieces.append(cell)
            continue
        acc = ""
        for w in cell.split(" "):
            cand = f"{acc} {w}" if acc else w
            if acc and not fits(f"{prefix_cont} | {cand}"):
                pieces.append(acc)
                acc = w
            else:
                acc = cand
        if acc:
            pieces.append(acc)
    out: list[str] = []
    cur = label
    bare = {label, prefix_cont}
    for piece in pieces:
        cand = f"{cur} | {piece}" if cur else piece
        if cur in bare or fits(cand):
            cur = cand
        else:
            out.append(cur)
            cur = f"{prefix_cont} | {piece}"
    out.append(cur)
    return [ln for ln in out if ln.strip()]


def check_invariants(text: str, *, plain_len: int | None = None) -> list[str]:
    """Return the list of violated R10 invariants for a rendered page (empty = clean)."""
    bad: list[str] = []
    if "\n\n\n" in text:
        bad.append("three consecutive newlines")
    if "\f" in text:
        bad.append("form feed emitted")
    if "\t" in text:
        bad.append("tab emitted")
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln and not ln.strip():
            bad.append(f"whitespace-only line {i}")
        if ln.startswith("#"):
            hashes = len(ln) - len(ln.lstrip("#"))
            if hashes > 4:
                bad.append(f"heading level > 4 at line {i}")
            elif not HEADING_RE.match(ln):
                bad.append(f"malformed heading line {i}: {ln[:40]!r}")
                continue
            if i + 1 < len(lines) and lines[i + 1] != "":
                bad.append(f"heading at line {i} not followed by a blank line")
    if plain_len is not None and len(text) > 1.5 * plain_len + 4096:
        bad.append("expansion > 1.5 × plain + 4096")  # noqa: RUF001
    return bad
