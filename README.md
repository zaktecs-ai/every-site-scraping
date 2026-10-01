<div align="center">

# 🕷️ Every-Site Scraper

**Scrape thousands of websites in parallel — one structured row per site.**

Parallel engine · 90-column schema · crash-safe resume · JSON + CSV output

[Install](#-install) · [Quick Start](#-quick-start) · [CLI Reference](#-cli-reference) · [Output](#-output) · [Summary](#-the-run-summary)

</div>

---

## What it does

Give it **any list of URLs** — a TXT file, a CSV with a website column, or command-line args. Every site gets **one row with 90 fixed columns**: identity, Open Graph, Twitter cards, SEO, content structure, every link & asset URL, social profiles, contacts, JSON-LD organization data, tech stack, and page weight.

**10–40+ workers scrape in parallel.** Every completed site is flushed to disk *immediately* — a crash loses nothing already written, and `--resume` continues exactly where it stopped.

```mermaid
input (CSV/TXT) ──► in-queue ──┬─► worker 1 ─┐
                               ├─► worker 2  │ completions
                               │   ...       │ flow to ONE
                               └─► worker N ─┘ collector
                                                │
                              CSV row + JSON site — flush + fsync
                              after EVERY site + atomic checkpoint
```

### Guarantees

| Guarantee | How |
| --- | --- |
| **All N workers actually work** | Every worker pulls from one queue; per-worker counts are in the summary — verified, not claimed |
| **Zero data loss** | Every row is flushed + fsynced the moment it completes; `kill -9` loses at most the sites in flight |
| **Resume after crash** | `--resume` skips every URL already in the output CSV and appends the rest; torn trailing rows are repaired |
| **No zombie traps** | A fatally broken worker ends the pool cleanly via poison pills — the run never hangs |
| **Fixed 90-column schema** | `csv.DictWriter(extrasaction="raise")` — a bad row *raises*, it can never shift or corrupt columns |
| **Deterministic** | All list columns are deduped and sorted; count columns always equal list lengths — the CSV is self-verifying |
| **Flat memory** | Rows are streamed to disk and folded into small accumulators — memory scales with `--workers`, not with site count |

---

## 📥 Install

```bash
git clone https://github.com/zaktecs-ai/every-site-scraping.git
cd every-site-scraping
pip install -r requirements.txt
```

> Python 3.8+ · just `requests`, `beautifulsoup4`, `lxml`.
> Optional: `pip install psutil` for richer CPU/memory stats in the summary.

---

## 🚀 Quick Start

```bash
# A few sites (10 workers by default)
python cli.py https://www.python.org https://www.postgresql.org

# 500 sites from a file — 30 workers in parallel, CSV + JSON output
python cli.py -f sites.csv -o out.csv -w 30 --json

# Run interrupted (Ctrl-C / crash / reboot)? Continue exactly where it stopped:
python cli.py -f sites.csv -o out.csv -w 30 --json --resume
```

**Input files:**

| Format | How it's read |
| --- | --- |
| `urls.txt` | One URL per line, `#` comments ignored |
| `leads.csv` | URL column **auto-detected** — by header name (`url`, `website`, `site`, `link`, `domain`…) or by scanning which column looks like URLs. Multi-column exports work as-is |

**Output files (per run):**

| File | Contents |
| --- | --- |
| `out.csv` | One row per site — 90 fixed columns, Excel-ready |
| `out.csv.json` | The same data as a **structured JSON document** — typed site objects (real arrays, ints, bools) |
| `out.csv.summary.json` | Rich run report — timing, throughput, CPU/RAM, per-worker stats, errors |
| `out.csv.log.jsonl` | Structured event log (one JSON object per line) |
| `out.csv.checkpoint.json` | Atomic progress checkpoint |

---

## ⌨️ CLI Reference

```
python cli.py [URLS...] [options]

  -f, --file URLS_FILE   Input file: .txt (one URL/line) or .csv (URL column auto-detected)
  -o, --output CSV_PATH  Output CSV (default: output.csv)
  -w, --workers N         Parallel workers (default: 10) — all N scrape concurrently
  -t, --timeout SECS     Per-request timeout (default: 20)
      --json [PATH]      Also write structured JSON (default: <output>.json)
      --resume           Continue a previous run — skip URLs already in the CSV
      --overwrite        Start fresh even if the output CSV exists
      --user-agent UA    Override the default Chrome User-Agent
      --quiet            No per-site progress lines
      --schema           Print the 90-column schema and exit
```

**Exit codes:** `0` all sites ok · `1` some failed · `2` usage error.
Existing output file without `--resume`/`--overwrite` is a hard error — data is never silently destroyed.

---

## 📤 Output

### CSV — the 90 columns

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
| **Social & contact** (10) | `social_links` + first `facebook_url`, `linkedin_url`, `instagram_url`, `youtube_url`, `github_url`, `mailto_links`, `tel_links`, `contact_emails`, `contact_phones` (visible text + mailto/tel + JSON-LD) |
| **Organization** (8) | `org_name`, `org_description`, `org_logo`, `org_url`, `org_founding_date`, `org_location`, `org_phone`, `jsonld_types` |
| **Tech & weight** (6) | `https`, `technology`, `technology_evidence`, `copyright_text`, `page_size_bytes`, `response_time_ms` |
| **Snapshot** (2) | `headings_outline`, `visible_text_preview` |

</details>

- Every list-valued column is **newline-separated** — Excel shows stacked items; `cell.split("\n")` gives the array back.
- Every count column **equals its list length** — self-verifying. `python data_quality.py out.csv` audits this.

### JSON — the same sites, typed

```json
{
  "meta": { "run_id": "…", "workers": 30, "schema_columns": 90 },
  "sites": [
    {
      "url": "https://example.com",
      "fetch_status": "ok",
      "http_status": 200,
      "https": true,
      "word_count": 42,
      "internal_links_list": ["https://example.com/about", "https://example.com/docs"],
      "contact_emails": ["info@example.com"],
      "...": "all 90 columns — lists are real arrays, numbers are ints, https is a bool"
    }
  ]
}
```

The JSON file is written **incrementally and crash-safely alongside the CSV**; on `--resume` it is healed (or rebuilt from the CSV, the source of truth) so CSV and JSON always match.

---

## 📊 The run summary

Every run ends with a rich `summary.json` — everything you need to analyze how the scrape went:

`meta` (run id, elapsed) · `input` (URL counts, resume) · `results` (ok / failed / success rate) · `throughput` (sites/sec) · `resources` (peak & mean CPU %, cores, RAM) · `workers` (per-worker sites/ok/failed/bytes/first-url — proves all N worked) · `timing` · `stability` · `checkpoint` · `output` · `data_quality` (fetch-status histogram, top errors, bytes scraped) · `domain_analysis` · `response_time_analysis` (min/median/p95/max) · `platform` · `health` (verdict + actionable warnings).

A sample from the live 213-site validation run:

| Metric | Value |
| --- | --- |
| Sites / workers / time | 213 / 30 workers / **84.1 s** |
| Throughput | **2.53 sites/s** (152 sites/min) on a 2-core box |
| Success | 191 ok (89.7%) — 22 honest failures (502/403/429 bot defences) |
| Workers active | **30/30** (2–13 sites each, zero idle) |
| Crash test | `SIGKILL` mid-run → 81 rows intact on disk → `--resume` → all 213, zero loss |

---

## 🧪 Testing & validation

```bash
python -m unittest discover -v        # 68 tests
python data_quality.py out.csv        # audit any output CSV
```

68 tests cover the schema, extraction, regression locks from live bug hunts, the parallel engine (all-workers-active, no double counts), crash-safety (torn-tail repair, atomic checkpoints), resume, URL-column detection, and the JSON output (typed, valid, resume-healed).

**Five live validation rounds, 380+ real websites** — small businesses, government portals, software houses, clinics, restaurants, universities, nonprofits, and global giants. Each round found and fixed real bugs:

1. **R1** (27 sites) — baseline extraction.
2. **R2** (58) — visibility bugs: `overflow-hidden` class falsely hiding Apple/IBM headings; headings inside custom elements dropped.
3. **R3** (60) — `sr-only` a11y headings kept; heading-in-`<li>` recovered (python.org); HTTP-202-empty reported honestly.
4. **R4** (65) — contacts living only in JSON-LD captured (recursive scan).
5. **R5** (213, 30 workers) — the parallel engine: throughput, SIGKILL+resume proof, a Unicode line-separator bug that silently dropped a 1.4 MB site from JSON (fixed + regression-locked), flat-memory streaming summaries.

---

## 📁 Project layout

```
every-site-scraping/
├── cli.py              # command-line entry point
├── runner.py           # parallel engine: N workers → one collector
├── checkpoint.py       # crash-safe CSV/JSON writers, resume, input loading
├── runlog.py           # JSONL log, resource monitor, run summary
├── multiscrape.py      # HTTP fetching (retries, meta-refresh, honesty)
├── scraper.py          # the 90-column extraction engine (SCHEMA lives here)
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 40 tests — schema, extraction, regressions
├── test_runner.py      # 28 tests — engine, resume, JSON, crash-safety
└── requirements.txt
```

## 💡 Tips

- **Slow sites?** `-t 40`. **Bot defences (403/429)?** Reported per-row — see `top_errors` in the summary.
- **RAM tight?** Lower `--workers` — memory scales with workers, not sites.
- **JavaScript-only SPAs** render client-side; `rendering_type` flags them honestly.

---

<div align="center">

**Built for bulk. Survives crashes. Never loses a row.**

</div>
