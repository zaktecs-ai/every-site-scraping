#!/usr/bin/env python3
"""Command-line interface for the Every-Site Structured Scraper (v2.0).

Usage (parallel by default — 10 workers):
    python cli.py https://example.com
    python cli.py -f sites.csv -o out.csv -w 30
    python cli.py -f sites.csv -o out.csv -w 30 --resume     # continue a crashed run
    python cli.py --schema                                     # print the 90 columns

Input  : URLs as arguments and/or a file (-f). A CSV is auto-detected:
         the URL column is found by header name or content, so your
         exported lead/CRM sheet works as-is. .txt = one URL per line.
Output : one CSV row per site (90 fixed columns, flushed after every
         row), plus <output>.log.jsonl, <output>.checkpoint.json and a
         rich <output>.summary.json (resources, timing, per-worker stats).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkpoint as cp  # noqa: E402
from runner import run  # noqa: E402
from scraper import SCHEMA  # noqa: E402


def _read_urls(path: str) -> list:
    """Load URLs from any input file (CSV with auto URL-column, or TXT)."""
    p = Path(path)
    if not p.exists():
        print(f"Error: input file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    urls = cp.load_urls_any(path)
    if not urls:
        print(f"Error: no URLs found in {path} "
              f"(CSV needs a url/website column or URL-looking cells).",
              file=sys.stderr)
        raise SystemExit(2)
    return urls


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrawler",
        description="Scrape many sites in parallel into one structured CSV "
                    "(one row per site, 90 fixed columns).",
        epilog="Examples:\n"
               "  python cli.py https://a.com https://b.com -w 20\n"
               "  python cli.py -f urls.txt -o leads.csv -w 30\n"
               "  python cli.py -f sites.csv -o out.csv -w 30 --resume\n"
               "  python cli.py --schema\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("urls", nargs="*", help="One or more website URLs.")
    p.add_argument("-f", "--file", metavar="URLS_FILE",
                   help="Input file: .txt (one URL per line) or .csv "
                        "(URL column auto-detected).")
    p.add_argument("-o", "--output", metavar="CSV_PATH", default="output.csv",
                   help="Output CSV path (default: output.csv). Side files: "
                        "<output>.log.jsonl, .checkpoint.json, .summary.json.")
    p.add_argument("--json", metavar="JSON_PATH", nargs="?", const="~auto~",
                   help="Also write a structured JSON file (typed site "
                        "objects: real arrays, ints, bools). Default path: "
                        "<output>.json (e.g. output.csv.json); pass a path "
                        "to choose your own.")
    p.add_argument("-w", "--workers", type=int, default=10,
                   help="Parallel workers (default: 10). All N workers scrape "
                        "concurrently; effective parallelism is bounded by "
                        "CPU cores and network.")
    p.add_argument("-t", "--timeout", type=int, default=20,
                   help="Per-request timeout in seconds (default: 20).")
    p.add_argument("--resume", action="store_true",
                   help="Continue a previous run: skip URLs already present "
                        "in the output CSV and append the rest.")
    p.add_argument("--overwrite", action="store_true",
                   help="Start fresh even if the output CSV exists "
                        "(default without --resume: error if it exists).")
    p.add_argument("--user-agent", metavar="UA", default=None,
                   help="Override the default Chrome User-Agent.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the per-site progress lines.")
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
        print("\nError: provide at least one URL (or -f URLS_FILE).",
              file=sys.stderr)
        return 2

    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        return 2

    # Existing output handling: --resume appends, --overwrite starts fresh,
    # otherwise an existing file is a hard error (never destroy data).
    out = Path(args.output)
    if out.exists() and out.stat().st_size > 0:
        if args.resume:
            pass
        elif args.overwrite:
            out.unlink()
        else:
            print(f"Error: {args.output} already exists. Use --resume to "
                  f"continue it, or --overwrite to replace it.",
                  file=sys.stderr)
            return 2

    def progress(n_done: int, n_total: int, status: str) -> None:
        pct = (100.0 * n_done / n_total) if n_total else 100.0
        sys.stderr.write(f"\r[{n_done}/{n_total}] {pct:5.1f}%  {status[:70]}   ")
        sys.stderr.flush()
        if n_done == n_total:
            sys.stderr.write("\n")

    json_path = None
    if args.json:
        json_path = (args.json if args.json != "~auto~"
                     else f"{args.output}.json")

    result = run(
        urls,
        output=args.output,
        workers=args.workers,
        timeout=args.timeout,
        resume=args.resume,
        user_agent=args.user_agent,
        quiet=args.quiet,
        progress=None if args.quiet else progress,
        json_output=json_path,
    )

    ok = result["ok"]
    total = result["rows"]
    print(f"\nDone. {ok}/{total} sites scraped successfully "
          f"({result['workers']} workers, {result['elapsed_s']}s).")
    print(f"{len(SCHEMA)} columns written to: {args.output}")
    if json_path:
        print(f"Structured JSON: {json_path}")
    print(f"Summary: {args.output}.summary.json | Log: {args.output}.log.jsonl")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
