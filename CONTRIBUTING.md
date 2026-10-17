# Contributing to Every-Site Scraping

Thanks for taking the time to contribute. This project is built to be easy to
work on — one dependency-light Python package, no build step, no framework.

## Quick start

```bash
git clone https://github.com/zaktecs-ai/every-site-scraping.git
cd every-site-scraping
pip install -r requirements.txt

python -m unittest discover        # the whole suite must pass
python cli.py scrape https://example.com   # smoke test
```

Runs on Python 3.8+. Only `requests`, `beautifulsoup4` and `lxml` are required;
`psutil` is optional (richer resource stats in the summary).

## Layout

| File | Responsibility |
| --- | --- |
| `cli.py` | argument parsing and the subcommands (`scrape`, `crawl`, `discover`, `probe`, `schema`, `audit`, `init`, `config`) |
| `config.py` | layered configuration: defaults → `scraper.toml` → `SCRAPER_*` env → CLI flags |
| `crawler.py` | URL discovery: scope, depth, robots.txt, sitemaps, politeness, canonicalisation |
| `netlayer.py` | the only module that touches the wire: fetching, retries, proxies, rate limiting |
| `runner.py` | the parallel engine: N workers → one collector → one sink |
| `sinks.py` | output writers: CSV, JSON, JSONL, SQLite (all crash-safe) |
| `checkpoint.py` | atomic CSV/JSON writers, resume, torn-tail repair, input loading |
| `scraper.py` | the 90-column extraction engine (`SCHEMA` lives here) |
| `runlog.py` | structured JSONL log, resource monitor, run summary |
| `data_quality.py` | output integrity auditor |

## What we care about

These are the invariants the test suite defends. Please keep them true.

1. **Never lose a row.** Every completed row is flushed and fsynced before the
   run moves on. A crash may lose the sites in flight; it must never lose a row
   already written. `--resume` must continue with zero duplicates and zero gaps.
2. **Never lie about a result.** A fetch that returns an empty body, a bot
   challenge, or a 403 is reported honestly with a distinct `fetch_status`.
   An "ok" row must mean a real page was fetched and parsed.
3. **Report `robots.txt` honestly.** Honouring robots is the default. The opt-out
   flag is deliberately loud, and disallowed URLs are counted in the summary —
   never silently dropped.
4. **Flat memory.** Rows stream to the sink and fold into small accumulators.
   Memory scales with `--workers`, not with the number of sites.
5. **Deterministic output.** List columns are deduplicated and sorted; every
   count column equals the length of its list.

## Adding a feature

- Keep new dependencies out unless there is no reasonable alternative. TOML
  parsing, for example, ships as a small built-in fallback so 3.8–3.10 need
  nothing extra.
- Put network code in `netlayer.py`. If a new module needs HTTP, import the
  `Fetcher` rather than `requests` directly.
- Add tests next to the behaviour you changed. `test_v3.py` shows the pattern for
  crawler/sink/config tests, including a local `http.server` fixture so no test
  ever depends on the public internet.
- Run `ruff check .` if you have it installed (CI enforces it).

## Pull requests

1. One logical change per PR.
2. Update `README.md` if you change user-visible behaviour or flags.
3. Make sure `python -m unittest discover` is green — CI runs it on Python
   3.8–3.13 plus macOS and Windows.
4. Describe *why*, not just *what*. If you fixed a bug, include the input that
   reproduced it.

## Reporting a bug

A useful report includes the command you ran, the output, and the site (or a
minimal reproduction) that triggered it. If a site produced a wrong row,
attaching the relevant columns is far more useful than a description.

## Legal and ethical use

This tool fetches public pages. You are responsible for what you fetch and what
you do with it: respect each site's terms of service and applicable law, and
honour `robots.txt` unless you have a specific reason not to. Do not use this
project to overload a service, bypass authentication, or collect personal data
you have no right to hold. Contributions that make the tool *less* honest about
what it fetched will not be merged.

## License

By contributing you agree that your work is licensed under the MIT License.
