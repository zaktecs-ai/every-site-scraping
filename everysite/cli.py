#!/usr/bin/env python3
"""Command-line interface for Every-Site Scraping.

Subcommands (run ``cli.py --help`` for the full list):

    scrape    scrape URLs into a structured dataset (csv/json/jsonl/sqlite)
    crawl     discover URLs by following links + sitemaps, then scrape them
    discover  crawl only — print/emit the discovered URL list, scrape nothing
    probe     inspect one site's robots.txt, sitemap and HTTP behaviour
    schema    print the column schema
    audit     validate an output file (any supported format)
    init      write a commented scraper.toml
    config    print the effective configuration

Configuration is layered: built-in defaults < ``scraper.toml`` < ``SCRAPER_*``
environment variables < explicit CLI flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import checkpoint as cp  # noqa: E402
from . import config as config_mod  # noqa: E402
from . import crawler as crawler_mod  # noqa: E402
from . import sinks as sink_mod  # noqa: E402
from .runner import run  # noqa: E402
from .scraper import SCHEMA  # noqa: E402

PROG = "every-site-scraping"
VERSION = "3.0.0"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _read_input(path: str) -> list:
    """Load URLs from a file, or from stdin when the path is ``-``."""
    if path == "-":
        data = sys.stdin.read()
        return [ln.strip() for ln in data.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    p = Path(path)
    if not p.exists():
        print(f"Error: input file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    urls = cp.load_urls_any(path)
    if not urls:
        print(f"Error: no URLs found in {path} (a CSV needs a url/website "
              f"column or URL-looking cells).", file=sys.stderr)
        raise SystemExit(2)
    return urls


def _add_common_input(p: argparse.ArgumentParser) -> None:
    p.add_argument("urls", nargs="*", help="One or more website URLs.")
    p.add_argument("-f", "--file", metavar="URLS_FILE",
                   help="Input file: .txt (one URL per line) or .csv "
                        "(URL column auto-detected). Use '-' for stdin.")
    p.add_argument("-i", "--input", metavar="URLS_FILE",
                   help="Alias of --file (convenience for scripts).")


def _add_output(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--output", metavar="PATH", default=None,
                   help="Output path (default: out.csv). The extension picks "
                        "the format unless --format is given.")
    p.add_argument("--format", choices=list(sink_mod.SUPPORTED_FORMATS),
                   default=None,
                   help="Output format: csv (default), json, jsonl, sqlite.")
    p.add_argument("--json", nargs="?", const="~auto~", metavar="PATH",
                   help="Also write a typed JSON document "
                        "(default <output>.json).")


def _add_network(p: argparse.ArgumentParser) -> None:
    p.add_argument("-t", "--timeout", type=int, default=None,
                   help="Per-request timeout in seconds (default: 20).")
    p.add_argument("--retries", type=int, default=None,
                   help="Retries on 202/429/5xx and timeouts (default: 2).")
    p.add_argument("--delay", type=float, default=None,
                   help="Minimum seconds between requests to the SAME host "
                        "(default: 0). robots.txt Crawl-delay is added on top.")
    p.add_argument("--proxy", default=None,
                   help="Single proxy URL used for all requests.")
    p.add_argument("--proxy-file", default=None, metavar="FILE",
                   help="File of proxies (one per line), rotated round-robin.")
    p.add_argument("--ignore-robots", action="store_true", default=None,
                   help="Do NOT honour robots.txt. Off by default; enabling it "
                        "is your legal call, not the tool's.")
    p.add_argument("--no-verify-tls", action="store_true", default=None,
                   help="Skip TLS certificate verification (insecure).")
    p.add_argument("--user-agent", default=None,
                   help="Override the default browser User-Agent.")


def _add_resume(p: argparse.ArgumentParser) -> None:
    p.add_argument("--resume", action="store_true", default=None,
                   help="Continue a previous run: skip URLs already in the "
                        "output and append the rest.")
    p.add_argument("--overwrite", action="store_true", default=None,
                   help="Start fresh even if the output exists.")


def _add_crawl_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("-d", "--max-depth", type=int, default=None,
                   help="Link-following depth (default: 2). 0 = seeds only.")
    p.add_argument("-n", "--max-urls", type=int, default=None,
                   help="Hard cap on discovered URLs (default: 1000).")
    p.add_argument("--exact-hosts", action="store_true", default=None,
                   help="Stay on the exact host instead of the whole "
                        "registrable domain.")
    p.add_argument("--no-sitemaps", action="store_true", default=None,
                   help="Do not expand /sitemap.xml or robots-declared "
                        "sitemaps.")
    p.add_argument("-s", "--strategy", choices=["breadth", "depth"],
                   default=None, help="Crawl order (default: breadth).")
    p.add_argument("--include", dest="include_pattern", default=None,
                   metavar="REGEX", help="Only keep URLs matching this regex.")
    p.add_argument("--exclude", dest="exclude_pattern", default=None,
                   metavar="REGEX", help="Drop URLs matching this regex.")


def _progress(enabled: bool):
    if not enabled:
        return None

    def cb(n_done: int, n_total: int, status: str) -> None:
        pct = (100.0 * n_done / n_total) if n_total else 100.0
        sys.stderr.write(f"\r[{n_done}/{n_total}] {pct:5.1f}%  {status[:70]}   ")
        sys.stderr.flush()
        if n_done and n_done == n_total:
            sys.stderr.write("\n")
    return cb


def _resolve(cfg: dict, args) -> dict:
    """One merged options dict: explicit CLI flag > config > default."""
    merged = dict(config_mod.to_cli_defaults(cfg))
    for key in list(merged):
        if hasattr(args, key):
            val = getattr(args, key)
            if val is not None:
                merged[key] = val
    if getattr(args, "workers", None) is not None:
        merged["workers"] = args.workers
    if getattr(args, "quiet", None):
        merged["quiet"] = True
    return merged


def _pick_format(args, opts, output_path: str) -> str:
    """--format wins; otherwise infer from the output extension."""
    if args.format:
        return args.format
    if opts.get("format") and opts["format"] != "csv":
        return opts["format"]
    return sink_mod.detect_format(output_path)


def _run_scrape(urls, args, opts, output, fmt, banner=None) -> int:
    output = str(output)
    out = Path(output)
    existed = out.exists() and out.stat().st_size > 0

    if existed:
        if args.resume:
            pass
        elif args.overwrite:
            out.unlink()
            for side in (".json", ".summary.json", ".log.jsonl",
                         ".checkpoint.json"):
                sp = Path(output + side)
                if sp.exists():
                    sp.unlink()
        else:
            print(f"Error: {output} already exists. Use --resume to continue "
                  f"it, or --overwrite to replace it.", file=sys.stderr)
            return 2

    if opts["workers"] < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        return 2

    json_path = None
    if args.json:
        json_path = (args.json if args.json != "~auto~" else f"{output}.json")

    quiet = bool(opts.get("quiet"))
    try:
        result = run(
            urls,
            output=output,
            workers=opts["workers"],
            timeout=opts["timeout"],
            resume=bool(args.resume),
            user_agent=opts.get("user_agent"),
            quiet=quiet,
            progress=_progress(not quiet),
            json_output=json_path,
            output_format=fmt,
            retries=opts["retries"],
            delay=opts["delay"],
            proxies=([opts["proxy"]] if opts.get("proxy") else None),
            proxy_file=opts.get("proxy_file"),
            respect_robots=not opts.get("ignore_robots", False),
            verify_tls=not bool(opts.get("no_verify_tls")),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if banner:
        print(banner)
    print(f"\nDone. {result['ok']}/{result['rows']} sites scraped successfully "
          f"({result['workers']} workers, {result['elapsed_s']}s).")
    print(f"{len(SCHEMA)} columns -> {output}  [{fmt}]")
    if json_path:
        print(f"Structured JSON: {json_path}")
    print(f"Summary: {output}.summary.json | Log: {output}.log.jsonl")
    return 0 if result["ok"] else 1


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_scrape(args) -> int:
    cfg = config_mod.load_config(args.config)
    opts = _resolve(cfg, args)

    urls = list(args.urls)
    src = args.file or args.input
    if src:
        urls.extend(_read_input(src))
    if not urls:
        print("Error: provide at least one URL (or -f URLS_FILE, or -f - for "
              "stdin).", file=sys.stderr)
        return 2

    output = args.output or opts.get("output") or "out.csv"
    fmt = _pick_format(args, opts, output)
    return _run_scrape(urls, args, opts, output, fmt)


def cmd_crawl(args) -> int:
    cfg = config_mod.load_config(args.config)
    opts = _resolve(cfg, args)

    seeds = list(args.urls)
    src = args.file or args.input
    if src:
        seeds.extend(_read_input(src))
    if not seeds:
        print("Error: provide at least one seed URL.", file=sys.stderr)
        return 2

    output = args.output or opts.get("output") or "out.csv"
    fmt = _pick_format(args, opts, output)

    quiet = bool(opts.get("quiet"))
    print(f"Crawling {len(seeds)} seed(s): depth<={opts['max_depth']}, "
          f"cap<={opts['max_urls']}, strategy={opts['strategy']}, "
          f"sitemaps={'off' if opts['no_sitemaps'] else 'on'}, "
          f"robots={'IGNORED' if opts.get('ignore_robots') else 'honoured'}",
          file=sys.stderr)

    cr = crawler_mod.discover(
        seeds,
        max_depth=opts["max_depth"],
        max_urls=opts["max_urls"],
        exact_hosts=opts["exact_hosts"],
        include_pattern=opts.get("include_pattern"),
        exclude_pattern=opts.get("exclude_pattern"),
        use_sitemaps=not opts["no_sitemaps"],
        respect_robots=not opts.get("ignore_robots", False),
        strategy=opts["strategy"],
        user_agent=opts.get("user_agent") or crawler_mod.DEFAULT_USER_AGENT,
        delay=opts["delay"],
        timeout=opts["timeout"],
        progress=_progress(not quiet),
    )
    if not quiet:
        sys.stderr.write("\n")

    stats = cr.as_dict()
    banner = (f"Crawl: {stats['urls_discovered']} URLs from "
              f"{stats['pages_fetched']} page(s) in {stats['elapsed_s']}s "
              f"(sitemap={stats['sitemap_urls']}, "
              f"out-of-scope={stats['skipped_out_of_scope']}, "
              f"robots-skipped={stats['skipped_by_robots']})")

    if not cr.urls:
        print(f"{banner}\nError: crawl discovered no URLs.", file=sys.stderr)
        return 1

    if args.no_scrape:
        print(banner, file=sys.stderr)
        for u in cr.urls:
            print(u)
        return 0

    return _run_scrape(cr.urls, args, opts, output, fmt, banner=banner)


def cmd_discover(args) -> int:
    cfg = config_mod.load_config(args.config)
    opts = _resolve(cfg, args)
    seeds = list(args.urls)
    src = args.file or args.input
    if src:
        seeds.extend(_read_input(src))
    if not seeds:
        print("Error: provide at least one seed URL.", file=sys.stderr)
        return 2

    quiet = bool(opts.get("quiet"))
    cr = crawler_mod.discover(
        seeds,
        max_depth=opts["max_depth"],
        max_urls=opts["max_urls"],
        exact_hosts=opts["exact_hosts"],
        include_pattern=opts.get("include_pattern"),
        exclude_pattern=opts.get("exclude_pattern"),
        use_sitemaps=not opts["no_sitemaps"],
        respect_robots=not opts.get("ignore_robots", False),
        strategy=opts["strategy"],
        user_agent=opts.get("user_agent") or crawler_mod.DEFAULT_USER_AGENT,
        delay=opts["delay"],
        timeout=opts["timeout"],
        progress=_progress(not quiet),
    )
    if not quiet:
        sys.stderr.write("\n")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"stats": cr.as_dict(), "urls": cr.urls}, indent=2),
            encoding="utf-8")
        print(f"Wrote {len(cr.urls)} URLs -> {args.json_out}", file=sys.stderr)
    else:
        for u in cr.urls:
            print(u)
    print(f"\n{cr.discovered} URLs discovered in {cr.elapsed_s:.1f}s "
          f"({cr.pages_fetched} pages fetched).", file=sys.stderr)
    return 0 if cr.urls else 1


def cmd_probe(args) -> int:
    cfg = config_mod.load_config(args.config)
    opts = _resolve(cfg, args)
    urls = list(args.urls)
    if args.file or args.input:
        urls.extend(_read_input(args.file or args.input))
    if not urls:
        print("Error: provide at least one URL to probe.", file=sys.stderr)
        return 2

    ua = opts.get("user_agent") or crawler_mod.DEFAULT_USER_AGENT
    for u in urls:
        info = crawler_mod.fetch_robots(u, user_agent=ua,
                                        timeout=opts["timeout"])
        print(f"\n=== {u} ===")
        status = info["robots_status"]
        print(f"robots.txt : HTTP {status}"
              f"{'' if info['robots_present'] else ' (absent -> no rules)'}")
        if info["crawl_delay"] is not None:
            print(f"crawl-delay: {info['crawl_delay']}s (honoured on top of "
                  f"--delay)")
        if info["sitemaps"]:
            print(f"sitemaps   : {len(info['sitemaps'])} declared")
            for sm in info["sitemaps"][:5]:
                print(f"             {sm}")
        if info["disallow_rules"]:
            print(f"disallow   : {len(info['disallow_rules'])} rule(s)")
            for r in info["disallow_rules"][:8]:
                print(f"             {r}")
        if args.deep:
            import netlayer
            f = netlayer.Fetcher(timeout=opts["timeout"], user_agent=ua)
            r = f.fetch(u)
            print(f"fetch      : {r['fetch_status']} (HTTP {r['http_status']}, "
                  f"{r['timing']}ms, {len(r.get('html') or '')} bytes, "
                  f"{r['attempts']} attempt(s))")
            if r.get("fetch_error"):
                print(f"error      : {r['fetch_error']}")
    print()
    return 0


def cmd_schema(args) -> int:
    if getattr(args, "json", False):
        print(json.dumps(SCHEMA, indent=2))
        return 0
    for i, c in enumerate(SCHEMA, 1):
        print(f"{i:2d}. {c}")
    print(f"\n{len(SCHEMA)} columns total.")
    return 0


def cmd_audit(args) -> int:
    path = args.target
    fmt = args.format or sink_mod.detect_format(path)
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 2

    if fmt == "csv":
        from . import data_quality
        return data_quality.audit(path)

    if fmt in ("json", "jsonl"):
        from collections import Counter
        rows = []
        if fmt == "json":
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except ValueError as e:
                print(f"FAIL: {path} is not valid JSON — {e}", file=sys.stderr)
                return 1
            rows = doc.get("sites", [])
        else:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith('{"_meta"'):
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        cols = set()
        for r in rows:
            cols.update(r.keys())
        print(f"OK: {path} — {len(rows)} site object(s), "
              f"{len(cols)} distinct field(s).")
        for k, v in sorted(Counter(
                str(r.get("fetch_status", "?")) for r in rows).items(),
                key=lambda kv: -kv[1]):
            print(f"    {k:<20} {v}")
        return 0

    if fmt == "sqlite":
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sites)")]
            print(f"OK: {path} — {n} row(s), {len(cols)} column(s).")
            for status, cnt in conn.execute(
                    'SELECT "fetch_status", COUNT(*) FROM sites '
                    'GROUP BY "fetch_status" ORDER BY 2 DESC'):
                print(f"    {str(status):<20} {cnt}")
            conn.close()
        except sqlite3.Error as e:
            print(f"FAIL: {path} — {e}", file=sys.stderr)
            return 1
        return 0

    print(f"Error: cannot audit format {fmt}", file=sys.stderr)
    return 2


def cmd_init(args) -> int:
    target = Path(args.path or config_mod.CONFIG_FILENAME)
    if target.exists() and not args.force:
        print(f"Error: {target} already exists (use --force to replace).",
              file=sys.stderr)
        return 2
    target.write_text(config_mod.SAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote {target}.\nEdit it, then run e.g.:\n"
          f"  python cli.py crawl https://example.com -d 2 -n 500")
    return 0


def cmd_config(args) -> int:
    cfg = config_mod.load_config(args.config)
    print(json.dumps(cfg, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Scrape or crawl any list of websites into a structured "
                    "dataset — 90 columns per site, crash-safe, parallel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s scrape https://a.com https://b.com -w 20\n"
               "  %(prog)s scrape -f urls.txt -o leads.csv -w 30\n"
               "  %(prog)s crawl https://example.com -d 3 -n 500 -o site.csv\n"
               "  %(prog)s crawl -f seeds.txt -o sites.sqlite --format sqlite\n"
               "  cat urls.txt | %(prog)s scrape -f -\n"
               "  %(prog)s scrape -f urls.txt -o out.csv --resume\n"
               "  %(prog)s discover https://example.com -d 2 > urls.txt\n"
               "  %(prog)s probe https://example.com --deep\n"
               "  %(prog)s init\n",
    )
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    p.add_argument("--config", metavar="PATH", default=None,
                   help="Path to scraper.toml (default: nearest one upward).")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    sp = sub.add_parser("scrape", help="Scrape URLs into a structured dataset.")
    _add_common_input(sp)
    _add_output(sp)
    sp.add_argument("-w", "--workers", type=int, default=None,
                    help="Parallel workers (default: 10).")
    _add_network(sp)
    _add_resume(sp)
    sp.add_argument("-q", "--quiet", action="store_true", default=None,
                    help="Suppress per-site progress lines.")

    cpp = sub.add_parser("crawl",
                         help="Discover URLs by crawling, then scrape them.")
    _add_common_input(cpp)
    _add_output(cpp)
    _add_crawl_flags(cpp)
    cpp.add_argument("-w", "--workers", type=int, default=None,
                     help="Parallel workers (default: 10).")
    cpp.add_argument("--no-scrape", action="store_true", default=None,
                     help="Only discover and print URLs; do not scrape.")
    _add_network(cpp)
    _add_resume(cpp)
    cpp.add_argument("-q", "--quiet", action="store_true", default=None)

    dp = sub.add_parser("discover",
                        help="Crawl only: print the discovered URL list.")
    _add_common_input(dp)
    _add_crawl_flags(dp)
    _add_network(dp)
    dp.add_argument("--json-out", metavar="PATH", default=None,
                    help="Write {stats, urls} JSON instead of plain lines.")
    dp.add_argument("-q", "--quiet", action="store_true", default=None)

    pp = sub.add_parser("probe",
                        help="Inspect robots.txt / sitemaps / HTTP behaviour.")
    _add_common_input(pp)
    _add_network(pp)
    pp.add_argument("--deep", action="store_true",
                    help="Also perform a real fetch and report the outcome.")

    sch = sub.add_parser("schema", help="Print the column schema.")
    sch.add_argument("--json", action="store_true", help="Emit JSON.")

    ap = sub.add_parser("audit", help="Validate an output file.")
    ap.add_argument("target", help="Path to a csv/json/jsonl/sqlite output.")
    ap.add_argument("--format", choices=list(sink_mod.SUPPORTED_FORMATS),
                    default=None, help="Force the format (default: by ext).")

    ip = sub.add_parser("init", help="Write a commented scraper.toml.")
    ip.add_argument("--path", default=None, help="Where to write it.")
    ip.add_argument("--force", action="store_true", help="Overwrite.")

    sub.add_parser("config", help="Print the effective configuration.")

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not argv:
        parser.print_help()
        return 0

    # Back-compat: `cli.py URL...` / `cli.py -f x` with no subcommand = scrape.
    # Only prepend when there is a real URL-like positional to scrape — flags
    # like --version / --help must reach the top-level parser untouched.
    known = {"scrape", "crawl", "discover", "probe", "schema", "audit",
             "init", "config"}
    top_level_flags = {"--version", "-h", "--help"}
    positionals = [a for a in argv if not a.startswith("-")]
    has_known_sub = bool(argv) and argv[0] in known
    if not has_known_sub:
        if not any(f in argv for f in top_level_flags):
            urlish = [a for a in positionals
                      if a.startswith(("http://", "https://")) or "." in a]
            if urlish or "-f" in argv or "--file" in argv or "-i" in argv \
                    or "--input" in argv:
                argv = ["scrape"] + argv

    args = parser.parse_args(argv)

    # Legacy `--schema` flag support
    if getattr(args, "schema", False):
        return cmd_schema(args)

    handler = {
        "scrape": cmd_scrape,
        "crawl": cmd_crawl,
        "discover": cmd_discover,
        "probe": cmd_probe,
        "schema": cmd_schema,
        "audit": cmd_audit,
        "init": cmd_init,
        "config": cmd_config,
    }.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
