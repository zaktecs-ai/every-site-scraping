<div align="center">

<img src="docs/banner.jpg" alt="Every-Site Scraping — parallel web scraper and crawler" width="880"/>

[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![CI](https://github.com/zaktecs-ai/every-site-scraping/actions/workflows/ci.yml/badge.svg)](https://github.com/zaktecs-ai/every-site-scraping/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-163%20passing-brightgreen)](#-testing--validation)
[![Code size](https://img.shields.io/github/languages/code-size/zaktecs-ai/every-site-scraping?color=blueviolet)](https://github.com/zaktecs-ai/every-site-scraping)
[![Last commit](https://img.shields.io/github/last-commit/zaktecs-ai/every-site-scraping?color=success)](https://github.com/zaktecs-ai/every-site-scraping/commits/main)
[![Validated](https://img.shields.io/badge/validated-900%2B%20live%20sites-orange)](#-proving-it-works--the-numbers)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Scrape a list, or crawl a whole site → 90 columns per URL → CSV · JSON · JSONL · SQLite**

**Parallel · robots-aware · crash-safe resume · flat memory · zero API keys**

</div>

---

# Every-Site Scraping

**Turn any list of URLs — or a single seed site — into a structured dataset: one row per page across a fixed 90-column schema** (SEO metadata, Open Graph, Twitter cards, headings, every link and asset URL, contacts, socials, JSON-LD organisation data, tech stack, page weight), written to CSV, typed JSON, append-only JSONL or a queryable SQLite database.

Built for bulk. Survives crashes. Never loses a row already written.

**Keywords:** python web scraper · bulk web scraping · parallel scraper · website crawler · sitemap crawler · URL to CSV · SEO metadata scraper · contact scraper · crash-safe scraping · B2B website scraper · lead enrichment.

---

## ⚡ 60-second start

```bash
pip install -r requirements.txt

# scrape a list into CSV
python -m everysite scrape https://example.com https://www.iana.org

# crawl a site (links + sitemaps, robots-aware), then scrape every page found
python -m everysite crawl https://example.com -d 2 -n 500 -o site.csv

# same crawl straight into SQLite, then query it
python -m everysite crawl https://example.com -d 2 -o site.sqlite
sqlite3 site.sqlite 'SELECT "url", "word_count" FROM sites ORDER BY CAST("word_count" AS INT) DESC LIMIT 5'
```

Or install it as a command:

```bash
pip install -e .
every-site-scraping crawl https://example.com -d 2 -n 500 -o site.csv
```

Only `requests`, `beautifulsoup4` and `lxml` — no browser engine, no API key, no service.

---

## 🧭 What you can do

| Command | What it does |
| --- | --- |
| `scrape` | Scrape an explicit URL list into a dataset. |
| `crawl` | **Discover** URLs by following links + sitemaps, **then** scrape them. |
| `discover` | Crawl only — print the discovered URL list (pipe it anywhere). |
| `probe` | Inspect a site's `robots.txt`, sitemaps and real HTTP behaviour. |
| `audit` | Validate any output file (csv/json/jsonl/sqlite). |
| `schema` | Print the 90-column schema. |
| `init` | Write a commented `scraper.toml`. |
| `config` | Print the effective, merged configuration. |

```bash
# crawl-only discovery, piped into a scrape
python -m everysite discover https://example.com -d 3 -n 2000 > urls.txt
python -m everysite scrape -f urls.txt -o out.csv -w 40

# stdin works
cat urls.txt | python -m everysite scrape -f -

# see what a site actually allows before you touch it
python -m everysite probe https://example.com --deep
```

---

## 🚀 Proving it works — the numbers

Everything below is measured and reproducible. Not "blazing fast" — actual
counts, from a **2-core ARM box**.

### Bulk scrape — 126 real, distinct, live sites

```console
$ python -m everysite scrape -f real.txt -o real.csv -w 24 --quiet
Done. 122/126 sites scraped successfully (24 workers, 84.7s).
```

| Metric | Value |
| --- | --- |
| Targets / workers / time | 126 sites / 24 workers / **84.7 s** |
| Throughput | **89 sites/min** |
| Success | 122 ok (**96.8%**) — 4 honest failures (3× HTTP error, 1× connection) |
| Columns | **90** per row, schema-validated |
| Audit | `OK: real.csv is clean (90 columns, schema-aligned)` |

### Crash safety — `SIGKILL` mid-run, then `--resume`

```console
$ python -m everysite scrape -f crash.txt -o crash.csv -w 16      # killed at 8s
SIGKILL → 221 of 400 rows already flushed to disk

$ python -m everysite scrape -f crash.txt -o crash.csv -w 16 --resume
Done. 0/179 sites scraped successfully (it resumed exactly the missing 179)

FINAL: 400 rows | 400 unique | 0 missing | 0 duplicates
audit: OK: crash.csv is clean (90 columns, schema-aligned)
```

A `kill -9` lost **nothing already written**. Resume found exactly the 179 URLs
that were missing — and nothing else.

### Crawl — link + sitemap discovery

```console
$ python -m everysite discover https://quotes.toscrape.com -d 2 -n 300
{"urls_discovered": 195, "pages_fetched": 47, "skipped_out_of_scope": 94,
 "depth_reached": 2, "elapsed_s": 11.35}
```

195 in-scope URLs discovered from 47 fetched pages, 94 out-of-scope links
correctly rejected, in 11.3 s.

### Test suite

```console
$ python -m unittest discover
Ran 163 tests in 14.5s
OK
```

163 tests across Python 3.8–3.13 plus macOS and Windows on every push
(see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)). CI also builds the
wheel, installs it in a clean venv, and smoke-tests the console script.

---

## 🛡️ Guarantees

These are the invariants the test suite defends. They are the reason to pick
this over a shell loop.

| Guarantee | How it is enforced |
| --- | --- |
| **Zero data loss** | Every completed row is flushed + `fsync`ed the moment it finishes. `kill -9` loses at most the requests in flight. |
| **Resume after a crash** | `--resume` reads the done-set out of the output itself (any format), repairs a torn tail, and scrapes only what is missing. No duplicates, no gaps. |
| **Never lies about a result** | HTTP 200 with an empty body is reported as `empty_response`, not `ok` — the most common way a bulk scraper misleads you. Every failure gets a distinct `fetch_status`. |
| **Honest `robots.txt`** | Honoured by default, including the site's own `Crawl-delay`. Disallowed URLs are **counted** (`robots_disallowed`), never silently dropped. |
| **Fixed 90-column schema** | `csv.DictWriter(extrasaction="raise")` — a bad row raises, it can never shift or corrupt columns. |
| **Deterministic output** | List columns are deduped and sorted; every count column equals its list length. `audit` verifies this. |
| **Flat memory** | Rows stream to the sink and fold into small accumulators. 100 sites or 100,000 — same RAM at the same `--workers`. |
| **All workers actually work** | Per-worker counts are in the summary — verified, not claimed. |

---

## 📦 Output formats

Pick by extension, or force it with `--format`.

| Format | Path | Best for |
| --- | --- | --- |
| `csv` | `out.csv` | Excel, spreadsheets, CRM import |
| `json` | `out.json` | one typed document; lists are real arrays, numbers are ints, `https` is a bool |
| `jsonl` | `out.jsonl` | streaming / large runs — one typed object per line, append-only, corruptible only at the last line |
| `sqlite` | `out.sqlite` | SQL over hundreds of thousands of rows; indexed on `url` |

```bash
python -m everysite scrape -f urls.txt -o leads.csv
python -m everysite scrape -f urls.txt -o leads.jsonl
python -m everysite scrape -f urls.txt -o leads.sqlite
python -m everysite scrape -f urls.txt -o leads.csv --json      # CSV + typed JSON
```

Side files per run: `<out>.summary.json` (timing, throughput, CPU/RAM,
per-worker stats, error histogram, health verdict), `<out>.log.jsonl`
(structured event log), `<out>.checkpoint.json` (atomic progress).

### The 90 columns

<details>
<summary><b>Click to expand the full data dictionary</b></summary>

| Group | Columns |
| --- | --- |
| **Source & request** (8) | `url`, `final_url`, `http_status`, `fetch_status`, `fetch_error`, `page_title`, `domain`, `site_name` |
| **Meta & Open Graph** (9) | `meta_description`, `meta_keywords`, `og_title`, `og_description`, `og_type`, `og_image`, `og_url`, `og_site_name`, `og_locale` |
| **Twitter cards** (5) | `twitter_card`, `twitter_title`, `twitter_description`, `twitter_image`, `twitter_handle` |
| **SEO** (7) | `canonical_url`, `robots_meta`, `viewport`, `language`, `charset`, `favicon`, `generator` |
| **Content** (8) | `h1_text`, `h1_count`, `h2_count`, `h3_count`, `h4_count`, `word_count`, `character_count`, `rendering_type` |
| **Links & assets** (27) | internal/external/total links, external domains, `all_urls`, image/document/media URLs, scripts, stylesheets, iframes, RSS feeds, hreflang, nav links, element counts — every URL **absolute, deduped, sorted** |
| **Social & contact** (10) | `social_links` + first `facebook_url`, `linkedin_url`, `instagram_url`, `youtube_url`, `github_url`, `mailto_links`, `tel_links`, `contact_emails`, `contact_phones` (visible text + `mailto:`/`tel:` + JSON-LD) |
| **Organization** (8) | `org_name`, `org_description`, `org_logo`, `org_url`, `org_founding_date`, `org_location`, `org_phone`, `jsonld_types` |
| **Tech & weight** (6) | `https`, `technology`, `technology_evidence`, `copyright_text`, `page_size_bytes`, `response_time_ms` |
| **Snapshot** (2) | `headings_outline`, `visible_text_preview` |

</details>

In CSV, list columns are newline-separated (`cell.split("\n")` gives the array
back). In JSON / JSONL they are real arrays. Every count column equals its list
length, which is what `audit` checks.

---

## 🕸️ Crawling

```bash
# depth 2, at most 500 URLs, staying on the registrable domain
python -m everysite crawl https://example.com -d 2 -n 500 -o site.csv

# stay strictly on the exact host (exclude docs.example.com, blog.example.com)
python -m everysite crawl https://example.com --exact-hosts

# only blog pages, never tag or category indexes
python -m everysite crawl https://example.com --include '/blog/' --exclude '/(tag|category)/'

# depth-first instead of breadth-first
python -m everysite crawl https://example.com -s depth

# discover only, no scraping — pipe the URLs anywhere
python -m everysite discover https://example.com -d 3 -n 2000 > urls.txt
```

What the crawler does:

- **Scope control** — stays on the registrable domain (`example.co.uk` is
  understood as one site) or the exact host, your choice.
- **`robots.txt`** — fetched once per host, parsed for `Disallow` **and
  `Crawl-delay`**, and honoured for both discovery and scraping.
- **Sitemaps** — `/sitemap.xml`, `/sitemap_index.xml` and any `Sitemap:` lines in
  `robots.txt`, including nested indexes, with a tolerant parser that survives
  malformed XML.
- **URL canonicalisation** — lowercases the host, strips default ports, collapses
  `//`, drops fragments and known tracking parameters (`utm_*`, `fbclid`,
  `gclid`, session ids…), so the same page reached ten ways is scraped once.
- **Asset filtering** — images, CSS, JS, PDFs, archives, media and fonts are
  never queued as pages.
- **Deterministic** — same seeds, same URL list.

---

## 🔧 Configuration

Layered, so a team commits one `scraper.toml` and everyone inherits it:

**built-in defaults → `scraper.toml` → `SCRAPER_*` env vars → explicit CLI flags**

```bash
python -m everysite init      # writes a commented scraper.toml
python -m everysite config    # print the effective merged config
```

```toml
[scrape]
workers = 20            # parallel workers
timeout = 25            # seconds per request
retries = 2             # retries on 202/429/5xx and timeouts
delay = 0.5             # min seconds between requests to the SAME host
respect_robots = true   # honour robots.txt (+ its Crawl-delay)

[crawl]
max_depth = 2
max_urls = 2000
exact_hosts = false
use_sitemaps = true
strategy = "breadth"

[output]
format = "csv"          # csv | json | jsonl | sqlite
path = "out.csv"

[network]
# proxy = "http://user:pass@host:port"
# proxy_file = "proxies.txt"
verify_tls = true
```

```bash
SCRAPER_WORKERS=40 SCRAPER_FORMAT=sqlite python -m everysite scrape -f urls.txt
```

---

## 🌐 Politeness, proxies, retries

```bash
# be gentle: never more than one request per 2s to the SAME host
python -m everysite crawl https://example.com --delay 2

# rotate a proxy pool (one per line; a dead proxy is quarantined, not retried forever)
python -m everysite scrape -f urls.txt --proxy-file proxies.txt

# retry transient failures
python -m everysite scrape -f urls.txt --retries 3 --timeout 40
```

Requests to the same host are serialised to at most one per `--delay`, while
different hosts run in parallel — the shape that keeps a wide crawl welcome.

---

## 📚 Use it as a library

```python
from everysite import scrape, crawl, discover, SCHEMA, canonicalize

# scrape an explicit list
result = scrape(["https://a.com", "https://b.com"], output="out.csv", workers=20)

# crawl then scrape, in one call
result = crawl(["https://example.com"], output="site.sqlite",
               max_depth=2, max_urls=500, workers=20)
print(result["discovered"], result["ok"])

# or drive the two steps yourself
found = discover(["https://example.com"], max_depth=2, max_urls=500)
print(len(found.urls), found.pages_fetched, found.skipped_scope)
result = scrape(found.urls, output="out.jsonl")
```

`scrape()` accepts every knob the CLI exposes (`resume`, `output_format`,
`retries`, `delay`, `proxies`, `respect_robots`, `progress`…).

---

## 🐳 Docker

```bash
docker build -t every-site-scraping .

docker run --rm -v "$PWD:/data" every-site-scraping \
  crawl https://example.com -d 2 -n 500 -o /data/site.csv
```

Runs as a non-root user; `/data` is the working directory.

---

## 📁 Project layout

```
everysite/
├── cli.py            # subcommands and argument parsing
├── config.py         # layered configuration (defaults → toml → env → flags)
├── crawler.py        # discovery: scope, depth, robots, sitemaps, canonicalisation
├── netlayer.py       # the only module that touches the wire: fetch, retries, proxies, politeness
├── runner.py         # parallel engine: N workers → one collector → one sink
├── sinks.py          # CSV / JSON / JSONL / SQLite writers, all crash-safe
├── checkpoint.py     # atomic writes, resume, torn-tail repair, input loading
├── scraper.py        # the 90-column extraction engine (SCHEMA lives here)
├── runlog.py         # structured log, resource monitor, run summary
└── data_quality.py   # output integrity auditor
```

---

## 🧪 Testing

```bash
python -m unittest discover        # 163 tests
python -m everysite audit out.csv  # audit any output file
ruff check .                       # lint (optional)
make check                         # lint + tests — what CI runs
```

Tests never depend on the public internet — HTTP behaviour is exercised against
a local `http.server`. New contributors: see [CONTRIBUTING.md](CONTRIBUTING.md),
which documents the invariants above so you can keep them true.

---

## 💡 Notes & limits

- **JavaScript-only pages are not rendered.** No headless browser is bundled, by
  design. Such pages are flagged honestly (`rendering_type`, `empty_response`)
  rather than reported as successful scrapes.
- **Bot defences** (403/429/202) are retried with backoff, then reported per row
  with a distinct status — see `top_errors` in the summary.
- **RAM tight?** Lower `--workers` — memory scales with workers, not with the
  number of sites.
- **Slow sites?** `-t 40`. **Being rate-limited?** `--delay 1` redistributes load
  over time instead of hammering.

---

## ❓ FAQ

**How do I scrape a list of websites into a CSV with Python?**
```bash
pip install -r requirements.txt
python -m everysite scrape -f urls.txt -o out.csv -w 30
```
The URL column in an input CSV is auto-detected (by header name — `url`,
`website`, `site`, `link`, `domain`… — or by scanning which column holds URLs),
so CRM and lead exports work with zero preprocessing.

**Can it crawl a whole website, not just the URLs I give it?**
Yes: `python -m everysite crawl https://example.com -d 2 -n 500`. It follows
in-scope links, expands `/sitemap.xml` and robots-declared sitemaps, honours
`robots.txt`, then scrapes every discovered page. Use `discover` if you only want
the URL list.

**What happens if it crashes mid-run?**
Nothing already written is lost — every row is flushed and `fsync`ed as it
completes. Re-run with `--resume` and it skips every URL already in the output
(any format), repairs a torn trailing row, and continues. Verified with a real
`SIGKILL`: 221/400 rows survived, resume completed exactly the 179 remaining,
zero duplicates.

**Can I get the data into SQLite and query it?**
Yes — add `-o out.sqlite` (or `--format sqlite`). All 90 columns become queryable
text columns with an index on `url`, so you can `SELECT`, `JOIN`, and use
SQLite's JSON1 functions directly.

**Does it need an API key, a browser, or a paid service?**
No. It runs locally on `requests` + `BeautifulSoup` + `lxml`.

**Does it respect robots.txt?**
Yes, by default, including the site's `Crawl-delay`, and disallowed URLs are
counted in the summary. `--ignore-robots` exists because some operators have a
lawful reason to crawl their own or licensed sites — that choice and its
consequences are yours, and the flag is deliberately explicit.

**How many sites can it handle, and does memory grow with site count?**
Memory stays flat: 100 or 100,000 sites at the same `--workers` use the same RAM.
Measured: 126 real sites in 84.7 s at 96.8% success on a 2-core box.

---

## ⚖️ Responsible use

This tool fetches public pages. **You** are responsible for what you fetch and
what you do with it: respect each site's terms of service and applicable law
(including data-protection law), and keep your request rate reasonable
(`--delay`, `--workers`). `robots.txt` is honoured by default. Do not use this
project to overload a service, bypass authentication, or collect personal data
you have no lawful basis to hold.

Security issues: see [SECURITY.md](SECURITY.md). Community rules:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## 📄 License

MIT — free to use, modify and ship. Scraped data belongs to you; the
responsibility for how it was collected is yours too. See [LICENSE](LICENSE).

---

<div align="center">

**Built for bulk. Survives crashes. Never loses a row.**

**Docs:** https://zaktecs-ai.github.io/every-site-scraping/ · [GitHub](https://github.com/zaktecs-ai/every-site-scraping) · MIT

</div>
