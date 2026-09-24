#!/usr/bin/env python3
"""Command-line interface for the Every-Site Structured Scraper.

Usage:
    python cli.py https://example.com
    python cli.py https://a.com https://b.com -o out.csv
    python cli.py -f urls.txt -o out.csv

Produces one row per site, with a fixed set of structured columns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import multiscrape  # noqa: E402
from scraper import SCHEMA  # noqa: E402


def _read_urls(path) -> list:
    return [
        ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrawler",
        description="Extract structured data from websites into a CSV (one row per site).",
    )
    p.add_argument("urls", nargs="*", help="One or more website URLs.")
    p.add_argument("-f", "--file", metavar="URLS_FILE",
                   help="Read URLs from a text file (one per line).")
    p.add_argument("-o", "--output", metavar="CSV_PATH", default="output.csv",
                   help="Output CSV path (default: output.csv).")
    p.add_argument("-t", "--timeout", type=int, default=20,
                   help="Per-request timeout in seconds (default: 20).")
    p.add_argument("--schema", action="store_true",
                   help="Print the column schema and exit.")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.schema:
        for i, c in enumerate(SCHEMA, 1):
            print(f"{i:2d}. {c}")
        print(f"\n{len(SCHEMA)} columns total.")
        return 0

    urls = list(args.urls)
    if args.file:
        urls.extend(_read_urls(args.file))

    if not urls:
        build_parser().print_help()
        print("\nError: provide at least one URL (or -f URLS_FILE).", file=sys.stderr)
        return 2

    stats = multiscrape.run(urls, output=args.output, timeout=args.timeout)
    print(f"\nDone. {stats['ok']}/{stats['total']} sites scraped successfully.")
    print(f"{stats['columns']} columns written to: {args.output}")
    return 0 if stats["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
