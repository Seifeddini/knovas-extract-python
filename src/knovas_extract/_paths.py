"""Source.path validation — Trojan Source, log injection, DoS.

Callers may pass a `path=` string into `extract(...)`; the CLI auto-populates
it from argv. We store the value verbatim on `Source.path` — but before we
do, we reject inputs that would corrupt logs / terminals / downstream
renderers when the value flows into a log line, a Slack message, or a code
review comment.

Rejected:
  - NUL bytes (truncate C strings downstream)
  - ASCII control characters 0x00-0x1F (except tab) — includes CR/LF used
    for log-injection and ANSI escapes (0x1B) used to hijack terminals
  - Unicode bidirectional-override / isolate characters (CVE-2021-42574
    "Trojan Source": U+202A..E, U+2066..9). These render a path as
    something different than its bytes actually contain — a common review
    bypass.
  - Anything over `Limits.max_path_length` (default 4096, POSIX PATH_MAX)

Rejection is a `ValueError` — a stdlib exception separate from our
`ExtractError` hierarchy — because this is caller misuse, not document
corruption. Error messages describe the *class* of violation without
including the offending value, so re-logging the exception does not
re-introduce the attack.

Not done here (deliberate):
  - No `os.path.realpath` / canonicalization: leaks filesystem topology
    and diverges caller-supplied vs. canonical.
  - No existence check: the path is metadata, not a filesystem handle.
  - No network / open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knovas_extract.result import Limits


# CVE-2021-42574 (Trojan Source) bidi override + isolate characters.
# LRE U+202A, RLE U+202B, PDF U+202C, LRO U+202D, RLO U+202E,
# LRI U+2066, RLI U+2067, FSI U+2068, PDI U+2069.
_BIDI_OVERRIDES = frozenset(
    {
        "‪",
        "‫",
        "‬",
        "‭",
        "‮",
        "⁦",
        "⁧",
        "⁨",
        "⁩",
    }
)


def validate_source_path(path: str | None, limits: Limits) -> str | None:
    """Return `path` unchanged if safe; raise `ValueError` otherwise.

    Errors describe the class of violation, never include the payload —
    log-safe.
    """
    if path is None:
        return None
    if len(path) > limits.max_path_length:
        raise ValueError(f"Source.path exceeds max_path_length ({limits.max_path_length} chars)")
    if "\x00" in path:
        raise ValueError("Source.path contains NUL byte")
    if any(c in _BIDI_OVERRIDES for c in path):
        raise ValueError(
            "Source.path contains Unicode bidirectional-override character "
            "(CVE-2021-42574 Trojan Source)"
        )
    if any(ord(c) < 0x20 and c != "\t" for c in path):
        raise ValueError("Source.path contains ASCII control character")
    return path
