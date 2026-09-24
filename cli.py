#!/usr/bin/env python3
"""Command-line interface for stdout scrawler.

Usage:
    python cli.py https://example.com
    python cli.py https://example.com https://site2.com -o output.csv
    python cli.py -f urls.txt -o output.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project's modules importable when run directly from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import multiscrape  # noqa: E402


def _read_urls_from_file(path) -> list:
    urls = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrawler",
        description="Extract all visible text from one or more websites into a CSV file.",
    )
    p.add_argument("urls", nargs="*", help="One or more website URLs to scrape.")
    p.add_argument(
        "-f", "--file", metavar="URLS_FILE",
        help="Read URLs from a text file (one per line).",
    )
    p.add_argument(
        "-o", "--output", metavar="CSV_PATH", default="output.csv",
        help="Where to write the CSV (default: output.csv).",
    )
    p.add_argument(
        "-t", "--timeout", type=int, default=20,
        help="Per-request timeout in seconds (default: 20).",
    )
    p.add_argument(
        "-u", "--user-agent", metavar="UA",
        default=multiscrape.DEFAULT_USER_AGENT,
        help="Custom User-Agent header.",
    )
    p.add_argument(
        "-d", "--keep-duplicates", action="store_true",
        help="Keep repeated text blocks instead of deduplicating.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    urls = list(args.urls)
    if args.file:
        urls.extend(_read_urls_from_file(args.file))

    if not urls:
        build_parser().print_help()
        print("\nError: provide at least one URL (or use -f URLS_FILE).",
              file=sys.stderr)
        return 2

    results = multiscrape.run(
        urls,
        output=args.output,
        timeout=args.timeout,
        user_agent=args.user_agent,
        dedupe=not args.keep_duplicates,
    )

    n_ok = sum(1 for r in results if r.get("ok"))
    print(f"\nDone. Scraped {n_ok}/{len(results)} sites successfully.")
    print(f"Results written to: {args.output}")
    return 0 if n_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
