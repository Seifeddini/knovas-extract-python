"""atheris fuzz target for the txt extractor.

Run locally:
    python tests/fuzz/fuzz_txt.py -atheris_runs=100000

Run in CIFuzz: see .github/workflows/cifuzz.yml.

Expected behavior: the only exceptions that escape are subclasses of ExtractError.
Anything else is a crash.
"""
from __future__ import annotations

import sys


def main() -> None:
    try:
        import atheris  # type: ignore[import-not-found]
    except ImportError:
        print("atheris not installed; install with `pip install atheris`.", file=sys.stderr)
        sys.exit(1)

    from knovas_extract import extract
    from knovas_extract.errors import ExtractError
    from knovas_extract.result import Limits

    LIMITS = Limits(max_input_bytes=1 << 20, max_text_bytes=1 << 20)

    def TestOneInput(data: bytes) -> None:
        try:
            extract(data, mime="text/plain", limits=LIMITS)
        except ExtractError:
            pass  # expected typed errors

    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
