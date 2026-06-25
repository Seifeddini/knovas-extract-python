"""Minimal CLI — `knovas-extract <path>` prints the JSON ExtractionResult.

Intentionally tiny; richer subcommands (sandbox, validate, dump-schema) land
in 0.2.0+.
"""
from __future__ import annotations

import argparse
import json
import sys

from knovas_extract import extract
from knovas_extract.errors import ExtractError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knovas-extract",
        description="Extract text + metadata from a document (JSON to stdout).",
    )
    parser.add_argument("path", help="Path to a document file.")
    parser.add_argument(
        "--mime",
        help="Override MIME detection (e.g. application/pdf).",
        default=None,
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent JSON output for human reading.",
    )
    args = parser.parse_args(argv)

    try:
        result = extract(args.path, mime=args.mime)
    except ExtractError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    json.dump(
        result.to_dict(),
        sys.stdout,
        indent=2 if args.pretty else None,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
