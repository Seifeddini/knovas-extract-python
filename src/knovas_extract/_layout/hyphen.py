"""Dehyphenation of wrapped lines (plan R6).

``Steuerer-`` + ``klärungen`` → ``Steuererklärungen``; ``Treuhand-`` + ``Gesellschaft``
→ ``Treuhand-Gesellschaft`` (next word capitalised: keep the hyphen);
``Lieferungs-`` + ``und`` → ``Lieferungs- und`` (``HYPH_KEEP_NEXT``: a
suspended hyphen before a conjunction keeps both hyphen and space); a soft
hyphen (U+00AD) always joins. A document-level :class:`HyphenPreference`
resolves the capitalised case from the document's own vocabulary: if the
joined spelling occurs elsewhere in the document it wins, if the hyphenated
spelling occurs it wins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

HYPH_KEEP_NEXT = frozenset(
    {"und", "oder", "bzw.", "sowie", "als", "wie", "bis", "u.", "resp.", "et", "ou", "e", "o"}
)
_TRAIL_HYPHEN = re.compile(r"(\w+)[-­¬]$")
_WORD = re.compile(r"[\w­-]+")


class HyphenPreference:
    """Vocabulary of a document used to choose between joined and hyphenated spellings."""

    __slots__ = ("_vocab",)

    def __init__(self, words: Iterable[str] = ()) -> None:
        self._vocab: set[str] = set()
        self.add(words)

    def add(self, words: Iterable[str]) -> None:
        for w in words:
            w = w.strip().strip(".,;:!?()\"'«»").lower()
            if len(w) >= 3:
                self._vocab.add(w)

    def prefer(self, left: str, right: str) -> str | None:
        """``"join"`` / ``"hyphen"`` when the document shows one spelling, else ``None``."""
        head = right.split(" ", 1)[0].strip(".,;:!?()\"'«»").lower()
        joined, hyph = (left + head).lower(), f"{left.lower()}-{head}"
        if joined in self._vocab and hyph not in self._vocab:
            return "join"
        if hyph in self._vocab and joined not in self._vocab:
            return "hyphen"
        return None

    def __len__(self) -> int:
        return len(self._vocab)


def dehyphen_join(lines: Sequence[str], prefer: HyphenPreference | None = None) -> str:
    """Join wrapped lines of one paragraph into a single physical line."""
    out = ""
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        if not out:
            out = ln
            continue
        m = _TRAIL_HYPHEN.search(out)
        nxt = ln.split(" ", 1)[0]
        if m and nxt:
            soft = out[-1] in "­¬"
            if soft:
                out = out[:-1] + ln
            elif nxt.lower() in HYPH_KEEP_NEXT or nxt[0].isdigit():
                out = out + " " + ln
            else:
                pref = prefer.prefer(m.group(1), ln) if prefer is not None else None
                if pref == "join" and nxt[0].isupper():
                    out = out[:-1] + nxt[0].lower() + ln[1:]  # the document spells it as one word
                elif pref == "join" or (pref is None and not nxt[0].isupper()):
                    out = out[:-1] + ln
                else:
                    out = out[:-1] + "-" + ln
        else:
            out = out + " " + ln
    return out.replace("­", "")
