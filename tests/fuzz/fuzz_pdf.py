"""atheris fuzz target for the PDF extractor.

Run locally (Linux/macOS only — atheris doesn't support Windows):
    python tests/fuzz/fuzz_pdf.py -atheris_runs=100000

Expected behavior: the only exceptions that escape are subclasses of ExtractError.
Anything else (UnicodeDecodeError from libmagic, RuntimeError from fitz, ...)
is a contract violation.
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        import atheris  # type: ignore[import-not-found]
    except ImportError:
        print("atheris not installed; install with `pip install atheris`.", file=sys.stderr)
        sys.exit(1)

    import contextlib

    from knovas_extract import extract
    from knovas_extract.errors import ExtractError
    from knovas_extract.result import Limits

    LIMITS = Limits(
        max_input_bytes=2 << 20,
        max_text_bytes=2 << 20,
        max_pages=100,
    )

    def TestOneInput(data: bytes) -> None:
        with contextlib.suppress(ExtractError):
            extract(data, mime="application/pdf", limits=LIMITS)

    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
