"""atheris fuzz target for the markdown extractor.

Run locally:
    python tests/fuzz/fuzz_md.py -atheris_runs=100000

See fuzz_txt.py for usage notes.
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

    LIMITS = Limits(max_input_bytes=1 << 20, max_text_bytes=1 << 20)

    def TestOneInput(data: bytes) -> None:
        with contextlib.suppress(ExtractError):
            extract(data, mime="text/markdown", limits=LIMITS)

    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
