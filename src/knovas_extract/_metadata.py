"""Metadata.extra scalar sanitizer — Trojan Source, log injection, length cap.

Every gap-filled scalar in `Metadata.extra` (per-format `<fmt>:<key>` entries)
flows through `sanitize_scalar` before being stored. This is the single
choke point that keeps hostile document metadata from corrupting
downstream logs, terminals, dashboards, and renderers.

Policy (mirrors `_paths.validate_source_path` but for extra values):
  - int / float / bool pass through unchanged (no injection surface).
  - str is stripped, then rejected if it contains NUL, ASCII control chars
    (except `\\t`), or Unicode bidi-override / isolate characters. Empty
    string → dropped (returns None). Over `max_metadata_value_length` →
    truncated with a counted warning.
  - Anything else (dict, list, bytes) → JSON-serialize, then sanitize as a
    string. Unserializable → dropped.

Warnings are **counted**, never content:
  - "metadata: 3 values truncated"            ✓
  - "metadata: dropped 2 values with control characters"    ✓
  - "metadata: dropped 'html:author' with value '<script>'" ✗ (leaks payload)

Extractors accumulate `Counter`-style state locally, then emit summary
warnings via `finalize_warnings(counts, warnings)` at the end of their
`extract()`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import Counter

    from knovas_extract.result import Limits


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


def _has_control(s: str) -> bool:
    return any(ord(c) < 0x20 and c != "\t" for c in s)


def _has_bidi(s: str) -> bool:
    return any(c in _BIDI_OVERRIDES for c in s)


def sanitize_scalar(
    value: object,
    *,
    limits: Limits,
    counts: Counter[str],
) -> str | int | float | bool | None:
    """Return a safe scalar for Metadata.extra, or None to drop the field.

    `counts` is mutated for counted warnings; caller emits them via
    `finalize_warnings`.

    Category keys used in `counts`:
      - "truncated": value was over max_metadata_value_length and cropped.
      - "control_chars": value contained NUL / ASCII control / bidi-override
        and was dropped.
      - "unserializable": non-scalar non-serializable object dropped.
    """
    if value is None:
        return None

    # bool is a subclass of int; check bool first so it passes through as-is.
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value

    if isinstance(value, bytes | bytearray):
        try:
            value = bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            counts["control_chars"] += 1
            return None

    if not isinstance(value, str):
        # dict / list / other — JSON-serialize, then treat as string.
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            counts["unserializable"] += 1
            return None

    s = value.strip()
    if not s:
        return None

    if "\x00" in s or _has_control(s) or _has_bidi(s):
        counts["control_chars"] += 1
        return None

    max_len = limits.max_metadata_value_length
    if len(s) > max_len:
        counts["truncated"] += 1
        s = s[:max_len]

    return s


def finalize_warnings(counts: Counter[str], warnings: list[str]) -> None:
    """Append counted, content-free warnings for any sanitizer drops / truncations.

    Deterministic order (sorted by category key) so goldens are stable.
    """
    labels = {
        "truncated": "values truncated",
        "control_chars": "values dropped for NUL / control / bidi-override characters",
        "unserializable": "values dropped as unserializable",
    }
    for key in sorted(counts):
        n = counts[key]
        if n <= 0:
            continue
        label = labels.get(key, key)
        warnings.append(f"metadata: {n} {label}")
