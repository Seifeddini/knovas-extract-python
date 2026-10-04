"""Minimal CLI — `knovas-extract <path>` prints the JSON ExtractionResult.

Intentionally tiny; richer subcommands (sandbox, validate, dump-schema) land
in 0.2.0+.
"""

from __future__ import annotations

import argparse
import json
import sys

from knovas_extract import OcrOptions, extract
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
    parser.add_argument(
        "--emit-markdown",
        action="store_true",
        help=(
            "Populate content.markdown with sanitized Markdown output "
            "(requires the [markdown] extra for HTML-shaped inputs and "
            "the [pdf-markdown] extra for PDFs)."
        ),
    )
    parser.add_argument(
        "--emit-sentences",
        action="store_true",
        help=(
            "Populate content.sentences with deterministic tokenization "
            "(char offsets, line numbers, page/section back-pointers). "
            "Requires the [sentences] extra (pysbd)."
        ),
    )
    parser.add_argument(
        "--ocr-engine",
        choices=["auto", "tesserocr", "cli", "mupdf"],
        default=None,
        help=(
            "PDF OCR engine: auto (tesserocr, then the tesseract CLI, then MuPDF), "
            "or force one. Scanned pages are OCR'd per page; born-digital pages are kept."
        ),
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=None,
        help="Render resolution for OCR (default: native, capped at 300).",
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=None,
        help="Parallel OCR workers (default: available CPUs - 1, capped by Limits).",
    )
    parser.add_argument(
        "--ocr-psm",
        type=int,
        default=None,
        help="Tesseract page segmentation mode (default 3).",
    )
    parser.add_argument(
        "--text-mode",
        choices=["plain", "layout"],
        default="plain",
        help=(
            "PDF and DOCX text rendering: plain (default) or layout. PDF: "
            "markdown-lite ('#' headings, one ' | ' row per table line, "
            "'Key: value' forms; unstructured pages stay byte-identical to "
            "plain). DOCX: tables rendered in place as ' | ' rows. Other "
            "formats emit plain text with a warning."
        ),
    )
    args = parser.parse_args(argv)

    ocr: OcrOptions | None = None
    if any(v is not None for v in (args.ocr_engine, args.ocr_dpi, args.ocr_workers, args.ocr_psm)):
        try:
            ocr = OcrOptions(
                engine=args.ocr_engine or "auto",
                dpi=args.ocr_dpi,
                workers=args.ocr_workers,
                psm=args.ocr_psm if args.ocr_psm is not None else 3,
            )
        except ValueError as exc:
            print(f"ValueError: {exc}", file=sys.stderr)
            return 2

    try:
        result = extract(
            args.path,
            mime=args.mime,
            emit_markdown=args.emit_markdown,
            emit_sentences=args.emit_sentences,
            ocr=ocr,
            text_mode=args.text_mode,
        )
    except ExtractError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Path validation rejection — log-safe message from _paths.
        print(f"ValueError: {exc}", file=sys.stderr)
        return 2

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
