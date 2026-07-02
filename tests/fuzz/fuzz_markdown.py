"""atheris fuzz target for the `_markdown.html_to_markdown` sanitizer.

Invariant: for any byte input decoded as UTF-8 with replacement, the
sanitizer either raises a typed ExtractError subclass OR returns a
canonicalized markdown string that contains none of the denylisted tag
literals or disallowed URL schemes. Emitted warnings match the
`markdown: N …` shape (no content leakage).

This is the security-critical target — start it first on any fuzz
budget. Extend the other per-format fuzz targets to cover the
emit_markdown path, but the sanitizer itself is where hostile HTML
lands.
"""

from __future__ import annotations

import re
import sys

_WARNING_SHAPE = re.compile(r"^markdown: \d+ .+$")
_DENYLIST_LITERALS = (
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<style",
    "javascript:",
    "data:text/html",
    "vbscript:",
    "file://",
    "chrome-extension:",
)


def main() -> None:
    try:
        import atheris  # type: ignore[import-not-found]
    except ImportError:
        print("atheris not installed", file=sys.stderr)
        sys.exit(1)

    from knovas_extract._markdown import html_to_markdown
    from knovas_extract.errors import ExtractError
    from knovas_extract.result import Limits

    LIMITS = Limits(
        max_input_bytes=1 << 20,
        max_text_bytes=1 << 20,
        max_markdown_expansion_ratio=100.0,  # noise-free; we test leakage, not blowup
    )

    def TestOneInput(data: bytes) -> None:
        html = data.decode("utf-8", errors="replace")
        warnings: list[str] = []
        try:
            md = html_to_markdown(html, LIMITS, warnings=warnings)
        except ExtractError:
            return  # typed error is acceptable
        # Invariants that must hold on any successful call.
        for literal in _DENYLIST_LITERALS:
            if literal in md:
                # Fail loudly for the fuzz harness.
                raise AssertionError(f"denylist literal {literal!r} leaked")
        for w in warnings:
            if not w.startswith("markdown:"):
                continue
            if not _WARNING_SHAPE.match(w):
                raise AssertionError(f"malformed warning: {w!r}")

    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
