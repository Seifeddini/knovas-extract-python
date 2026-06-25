"""atheris fuzz target for the DOCX extractor.

Run locally:
    python tests/fuzz/fuzz_docx.py -atheris_runs=100000

Fuzzing DOCX exercises the full guard chain: zip integrity + zip-slip name
guard + decompression-ratio cap + per-entry size cap + python-docx XML parse
+ defusedxml core.xml parse. Any non-ExtractError exception escaping is a bug.
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

    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    LIMITS = Limits(
        max_input_bytes=2 << 20,
        max_text_bytes=2 << 20,
        max_decompression_ratio=100,
    )

    def TestOneInput(data: bytes) -> None:
        with contextlib.suppress(ExtractError):
            extract(data, mime=DOCX_MIME, limits=LIMITS)

    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
