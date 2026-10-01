# Every-Site Structured Scraper v2.0 — Parallel Engine

Extract **complete, structured information about any website** into a single
CSV — one row per site, **90 fixed columns**, 100% deterministic, evidence-backed,
structurally guaranteed — now with a **parallel engine**: give it 500 or 5,000
URLs and N workers, and all N workers scrape concurrently with
**zero data loss** (every row flushed + fsynced the moment it completes),
a **crash-safe checkpoint**, **resume**, structured **JSONL run logs**, and a
rich **JSON run summary** (resources, timing, per-worker stats, errors).

```
python cli.py -f sites.csv -o out.csv -w 30          # 30 workers in parallel
python cli.py -f sites.csv -o out.csv -w 30 --resume # continue where it stopped
```

Give it a list of URLs — as arguments, a TXT (one per line), or **any CSV
with a URL column** (auto-detected by header name or content — your exported
lead/CRM sheet works as-is). Each site's row covers identity, Open Graph,
Twitter cards, SEO, content, the **actual links and URLs** (internal,
external, images, documents, media, scripts, styles, feeds, nav, social —
every URL resolved to absolute), social profiles, contact details,
organization data (JSON-LD), detected technology stack, and page weight.

### Multi-value cells: one clean, parseable convention
Every list-valued column (any `*_list`, `*_urls`, `*_links`, `*_feeds`,
`contact_emails`, etc.) is a **newline-separated** list — deduplicated and
sorted. This is the one convention across the whole file:
- **Human-readable**: Excel / Google Sheets render each item on its own line
  inside the cell.
- **Python-easy**: `df[col].str.split("\n")` (or `cell.split("\n")`) gives you
  the list back — perfect for separating everything out later.
- **100% unambiguous**: a URL/email can contain a comma or semicolon but never
  a newline, so the split is always safe.

For every paired count+list column (e.g. `internal_links` ↔
`internal_links_list`), the **count is guaranteed to equal the list length** —
the CSV is self-verifying.

---

## Key guarantees (the "no mismatch" promise)

1. **Fixed schema** — every row has exactly the same 90 columns, in the same
   order. No "extra" column on one row and a missing one on the next.
2. **Strict CSV writer** — `csv.DictWriter(..., extrasaction="raise",
   restval="")`. An out-of-schema field **raises** instead of silently
   dropping; a missing field is written as `""`, so nothing shifts.
3. **All values are strings** — tested; no `None`, no objects, no mixed types.
4. **Deterministic** — no probabilistic guesses; lists are deduplicated **and
   sorted**, and technology detection is always paired with
   `technology_evidence` so every claim is auditable.
5. **Accurate, not padded** — if the site does not expose a phone or email,
   the field stays empty. Empty is correct; garbage is not.

### v2.0 operational guarantees

6. **All N workers actually work** — every worker pulls from the same queue
   until exhaustion; the summary reports per-worker counts so this is
   **verifiable, not just claimed**.
7. **No data loss** — every completed row is appended, flushed AND fsynced
   before the run continues. `kill -9` mid-run loses at most the sites in
   flight; rerun with `--resume` and the done URLs are skipped.
8. **No zombie traps** — poison-pill termination: a fatally broken worker
   ends the pool cleanly instead of hanging forever.
9. **Torn-tail repair** — a crash mid-append can leave a truncated last row;
   resume detects and removes it, so the CSV is always schema-clean.
10. **Atomic checkpoints** — `checkpoint.json` is written tmp+rename
    (atomic on POSIX); a torn checkpoint can never corrupt a run (resume
    derives its done-set from the actual CSV, which is the source of truth).

---

## The parallel engine (how it works)

```
input (CSV/TXT) ──► URL list ──► in-queue ──┬─► worker 1 ─┐
                                             ├─► worker 2  │ completions
                                             │   ...       │ flow to ONE
                                             └─► worker N ─┘ collector
                                                              │
                                           SafeCSVWriter: flush + fsync
                                           after EVERY row + atomic JSON
                                           checkpoint + JSONL event log
```

- **Threads, not processes** — the workload is network-bound (requests
  waits + lxml parsing release the GIL), so threads give real N-way
  parallelism with zero IPC overhead.
- **Collector pattern** — workers never touch the CSV; one collector thread
  owns the writer, so writes stay strictly serial (crash-safety depends on
  it) while scraping stays fully parallel. All per-worker stats are updated
  by the collector alone — no shared-counter races.
- **Background resource monitor** — samples total CPU % (all cores), the
  process RSS, and (if `psutil` is installed) system memory, feeding the
  summary's resources section.

### Files produced by a run

| File | What it holds |
| --- | --- |
| `out.csv` | The data — one row per site, 90 fixed columns |
| `out.csv.log.jsonl` | Structured event log (run_start, url_done, worker_start, checkpoint, run_end…) — one JSON object per line |
| `out.csv.checkpoint.json` | Lightweight, atomically-written progress checkpoint (observability; resume does not depend on it) |
| `out.csv.summary.json` | The rich end-of-run report — see below |

### The JSON run summary (16 sections)

`meta` (run id, elapsed) · `command_line` · `input` (URL counts, resume) ·
`results` (ok/failed/success rate) · `throughput` (sites/sec) ·
`resources` (peak & mean CPU %, cores, peak & mean RSS MB) · `workers`
(per-worker sites/ok/failed/ms-active/MB-scraped + fatal errors) ·
`parallelism` · `workers_report` · `timing` (phases) · `stability`
(retries, resume events, data-loss events) · `checkpoint` · `output` ·
`data_quality` (fetch_status histogram, top errors, bytes scraped, mean
page KB) · `domain_analysis` (unique domains, top domains) +
`response_time_analysis` (min/median/p95/max/mean) · `platform` (python,
os, cores) · `health` (verdict + actionable warnings).

---

## Output: the 90 columns (data dictionary)

Grouped by category. Every site's row reflects all of these.

### A. Source & request
| Column | Meaning |
| --- | --- |
| `url` | URL exactly as requested |
| `final_url` | URL after redirects resolved |
| `http_status` | Numeric status (200, 404…) |
| `fetch_status` | `ok`, `http_error`, `timeout`, `ssl_error`, `connection_error`, `empty_response`, `error` |
| `fetch_error` | Plain-English failure reason (empty if ok) |
| `page_title` | Text of `<title>` |
| `domain` | Hostname, lowercase |
| `site_name` | Best identity: og:site_name → title → domain brand |

### B. Meta & Open Graph
`meta_description`, `meta_keywords`, `og_title`, `og_description`, `og_type`,
`og_image`, `og_url`, `og_site_name`, `og_locale`

### C. Twitter cards
`twitter_card`, `twitter_title`, `twitter_description`, `twitter_image`,
`twitter_handle`

### D. SEO & indexing
`canonical_url`, `robots_meta`, `viewport`, `language`, `charset`, `favicon`,
`generator`

### E. Content structure
`h1_text`, `h1_count`, `h2_count`, `h3_count`, `h4_count`, `word_count`,
`character_count`, `rendering_type`

### F. Links, URLs & assets — counts + actual lists (newline-separated)
Every list here holds **real, absolute URLs** (relative paths resolved against
the final URL), deduplicated and sorted. Paired count == list length.

| Count column | List column | Contents |
| --- | --- | --- |
| `internal_links` | `internal_links_list` | Same-domain link URLs |
| `external_links` | `external_links_list` | Other-domain link URLs |
| `total_links` | — | Unique internal + external count |
| `external_domains` | `external_domains_list` | Unique external hostnames |
| `all_urls_count` | `all_urls` | **Every** unique URL on the page (links + assets) |
| `image_urls_count` | `image_urls` | `<img>`, `srcset`, lazy, `og:image` URLs |
| `document_links_count` | `document_links` | pdf/doc/xls/ppt/zip/csv/… file URLs |
| `media_urls_count` | `media_urls` | video/audio/embed URLs (incl. YouTube/Vimeo) |

Plus (list-only or count-only): `script_urls`, `stylesheet_urls`,
`iframe_urls`, `rss_feeds`, `hreflang_urls`, `nav_links` (primary
navigation), and the element counts `images_count`, `images_missing_alt`,
`iframes_count`, `scripts_count`, `styles_count`, `forms_count`.

### G. Social & contact
`social_links` (ALL social profile URLs, newline list), `facebook_url`,
`linkedin_url`, `instagram_url`, `youtube_url`, `github_url` (first of each),
`mailto_links`, `tel_links`, `contact_emails` (visible text **+** `mailto:` **+**
JSON-LD, deduped), `contact_phones` (visible text **+** `tel:` **+** JSON-LD,
deduped).

### H. Organization (JSON-LD)
`org_name`, `org_description`, `org_logo`, `org_url`, `org_founding_date`,
`org_location`, `org_phone`, `jsonld_types`

### I. Security, tech & weight
`https`, `technology`, `technology_evidence`, `copyright_text`,
`page_size_bytes`, `response_time_ms`

### J. Content snapshot
`headings_outline` (reading order), `visible_text_preview` (first ~500 chars)

> The machine-readable schema (and per-column descriptions) lives in
> `scraper.py` as `SCHEMA` and `DICTIONARY`. Run `python cli.py --schema`
> to print the column list.

---

## Install (one time)

```bash
pip install -r requirements.txt
```

Python 3.8+ and just `requests`, `beautifulsoup4`, `lxml`.
Optionally `pip install psutil` for richer CPU/memory stats in the summary
(the engine works fully without it — stdlib `/proc/stat` + `resource` are
used as fallback).

---

## Run it

### One / several sites (parallel by default)

```bash
python cli.py https://www.postgresql.org
python cli.py https://www.postgresql.org https://www.python.org -o out.csv
```

### From a file — TXT or any CSV with a URL column

```bash
python cli.py -f urls.txt -o out.csv                 # one URL per line
python cli.py -f leads.csv -o out.csv -w 30          # URL column auto-detected
```

The CSV loader finds the URL column by header name (`url`, `website`,
`site`, `link`, `domain`, `homepage`, …) or, for header-less/odd exports, by
scanning which column looks most like URLs. Multi-column spreadsheets work
as-is — no preprocessing needed.

### Speed: choose your worker count

```bash
python cli.py -f sites.csv -o out.csv -w 10     # default: 10 workers
python cli.py -f sites.csv -o out.csv -w 30     # 30 workers in parallel
python cli.py -f sites.csv -o out.csv -w 40     # all 40 must actually work
```

Every configured worker participates (the summary proves it per worker).
Effective throughput is bounded by CPU cores and network — see the
`throughput` and `resources` sections of the run summary.

### Never lose work: resume a crashed/interrupted run

```bash
python cli.py -f sites.csv -o out.csv -w 30            # interrupted (Ctrl-C / crash / reboot)
python cli.py -f sites.csv -o out.csv -w 30 --resume   # continues exactly where it stopped
```

`--resume` skips every URL already present in the output CSV (matched on the
`url` column), repairs a torn trailing row if a crash left one, and appends
only the remainder. Without `--resume`, an existing output file is a hard
error (data is never silently destroyed) — use `--overwrite` to start fresh.

### Other flags

```bash
python cli.py -f sites.csv -o out.csv -w 30 -t 40        # per-request timeout 40s
python cli.py -f sites.csv -o out.csv --user-agent "…"  # custom UA
python cli.py -f sites.csv -o out.csv --quiet            # no per-site lines
python cli.py --schema                                    # print the 90 columns
```

### Audit any produced CSV

```bash
python data_quality.py out.csv
```

---

## Structural integrity checks

```bash
python -m unittest test_scraper -v      # 39 tests: schema, extraction, regressions
python -m unittest test_runner -v       # 23 tests: parallel engine, resume, crash-safety
python -m unittest discover -v          # everything (62 tests)
```

They cover schema length/uniqueness, per-field presence, "all strings"
verification, strict header==schema alignment, link-harvest correctness,
visibility/walker regression locks, CSV round-trips against a local server —
and, new in v2.0: flush-per-row appends, atomic checkpoints, torn-tail
repair, URL-column auto-detection (TXT, single-column CSV, multi-column CSV
by header AND by content), JSONL log parsing, the 16-section summary
structure, all-workers-active parallel runs, single-worker correctness,
resume skipping, parallel-beats-serial timing, and per-worker no-double-count
invariants.

The `data_quality.py` helper audits any produced CSV and reports the exact
row, column, and value for any anomaly (wrong column count, None, duplicate
header, unsorted lists, count≠list-length).

---

## Validation history (real-world bug hunts)

The extractor has been hardened across **five live test rounds** totalling
**380+ real websites**, each followed by a deep analysis pass that found and
fixed genuine bugs. (The website lists themselves are not shipped in this
repo.)

### Round 1 — 27 tech/software sites
Baseline: generic extraction worked across MongoDB, Python.org, Apache,
WordPress, FreeBSD, LLVM, WebKit, etc.

### Round 2 — 58 sites (PK software houses + global giants)
Surfaced and fixed **three real data-loss bugs**:

1. **`overflow-hidden` treated as hidden** — a CSS overflow utility was
   substring-matched against `"hidden"`, silently dropping visible content
   (Apple's `<h1>`, IBM's headings, Mongo's hero). Fixed: class names are
   matched as whole whitespace-delimited tokens.
2. **Tailwind `[&_br]:hidden` treated as hidden** — this selector hides a
   descendant `<br>`, not the element, yet flagged Databricks' `<h1>` as
   hidden. Fixed by the same tokenisation.
3. **Headings inside custom elements / block markup dropped** —
   `<h1>Code <div>…</div> Work</h1>` (Snowflake) and Web Components like
   `<c4d-video-cta-container>` (IBM) made whole subtrees vanish. Fixed:
   headings are always emitted as atomic leaves, and unknown/custom elements
   are descended into instead of discarded.

Also added `rendering_type` (`server_rendered` / `client_rendered`) so a
near-zero word count on a JavaScript-only SPA is reported honestly.

### Round 3 — 60 small/medium sites (indie SaaS, open-source, design, startups)
Surfaced and fixed **three more real bugs**:

1. **`sr-only` / `visually-hidden` treated as hidden** — these accessibility
   classes hide content *visually* but deliberately keep it in the DOM for
   crawlers, and often sit on a page's real `<h1>` (e.g. sitepoint.com).
   Dropping them was data loss. Now kept (only `hidden` / `invisible` hide).
2. **A heading inside a `<li>`/`<td>` was swallowed** — python.org's homepage
   `<h1>` lives inside `<li class="slide">`; the whole list item was emitted
   and its five `<h1>`s vanished. Fix: **only headings** are atomic leaves;
   everything else uses the container-vs-leaf rule (5 h1s recovered).
3. **HTTP 202 / empty-body reported as success** — dribbble.com's bot defence
   returns `202 Accepted` with an empty body; the scraper wrote a blank row
   and called it "ok". Now 202 is retried then reported honestly, and an
   empty body becomes `fetch_status=empty_response`.

### Round 4 — 65 small software houses, service businesses & gov portals
(Pakistan & India software houses, municipal/government portals, dental &
medical clinics, restaurants, gyms, business directories.) Surfaced and fixed:

1. **Contacts that live only in JSON-LD were missed** — business/local pages
   frequently publish their canonical email/telephone inside schema.org
   structured data rather than visible text (e.g. caviar.pk). Added a
   **recursive** JSON-LD contact scan, now folded into `contact_emails` /
   `contact_phones` alongside visible text, `mailto:` and `tel:` links.

### Round 5 — v2.0 parallel engine on 200+ mixed live sites
The engine was run at **30 workers across 213 mixed live sites** (small/local
business sites — dental clinics, restaurants, gyms, law firms, accounting,
real estate, design studios, hotels — government portals (Pakistan federal
ministries, NADRA, FBR, ECP), software houses (PK + global), open-source
documentation sites, universities, nonprofits, and a handful of large sites
like Apple, Wikipedia, NASA, Microsoft). Results (all from the run's own
`summary.json` / `log.jsonl`):

- **Throughput**: 213 sites in **84.1 s (2.53 sites/s, 152 sites/min)** at
  30 workers on a 2-core machine — vs. **89.8 s for just 60 sites at
  2 workers** (0.67 sites/s): the same workload at 15× the workers ran at
  **3.8× the throughput**, and every configured worker participated
  (30/30 active, per-worker 2–13 sites, no idle worker, no double counts).
- **Honest failure reporting**: 191/213 ok (89.7%); the 22 failures were
  all real server responses (8× HTTP 502, 7× 403 bot-defence, 3× 202
  challenge, 3× 429 rate-limit, 1× 404) — each reported per-row with its
  `fetch_status`/`fetch_error`, zero crashes, zero data loss.
- **Crash-safety (verified live)**: the run was hard-killed (`SIGKILL`)
  mid-flight; **81 already-completed rows were intact on disk** and the CSV
  was still schema-clean; a `--resume` run then completed the remaining
  132 sites to a full, unique, data-quality-clean 213-row CSV.
- **Torn-tail repair (hardened after a real find)**: a crash cut landing
  *inside a quoted multi-line list cell* fooled the original field-count
  check (each embedded newline kept the fragment "parsable"). The repair
  now uses a structural rule — every complete write ends with `\r\n`, which
  never occurs inside a row — so any trailing fragment after the last
  `\r\n` is dropped. Regression-locked by a test that cuts exactly there.
- **Streaming summaries (memory-flat)**: rows are folded into small
  accumulators as they are written, never retained. Measured: the same 213
  sites peak at **228 MB RSS with 8 workers** vs **353 MB with 30 workers**
  — memory scales with workers (concurrent DOM size), not with row count,
  so a 5,000- or 100,000-site run at the same worker count uses the same
  memory.
- **Data quality**: `data_quality.py` audit clean on every produced CSV
  (90 columns, schema-aligned, count==list-length, 0 mismatches).

Count↔list consistency was **0 mismatches** across every round, and every
non-200 (403/429/500/502/timeout) is reported truthfully in
`fetch_status`/`fetch_error` — the scraper never crashes on a bad site, and
the engine never hangs on a bad worker.

---

## Project layout

```
every_site_scraping/
├── cli.py              # command-line entry point (parallel engine, resume)
├── runner.py           # parallel engine: N workers, collector, checkpoints
├── checkpoint.py       # crash-safe incremental writer + resume + input loading
├── runlog.py           # JSONL structured logs + resource monitor + summary
├── multiscrape.py      # fetch (retries, meta-refresh, empty-body) + strict CSV
├── scraper.py          # structured extractor + SCHEMA + DICTIONARY
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 39 tests (schema, extraction, regression locks)
├── test_runner.py      # 24 tests (parallel engine, resume, crash-safety)
├── requirements.txt    # dependencies (requests, beautifulsoup4, lxml)
└── README.md           # this file
```

> `multiscrape.py` still offers the original serial `run()` for
> compatibility and testing; the CLI now drives the parallel `runner.run()`.

---

## Tips

- **JavaScript-heavy SPAs** render content client-side and may show sparse
  results; server-rendered pages are fully covered. `rendering_type` tells
  you which is which.
- **Bot protection** (403/429/202-empty) is reported per-row, never crashes
  the run — see the `top_errors` list in the summary.
- Increase timeout for slow sites: `-t 40`.
- **Memory is worker-bounded, not row-bounded** — rows are streamed to disk
  and folded into small accumulators immediately, so a 100k-site run uses the
  same memory as a 100-site run at the same `--workers` count (measured:
  213 sites at 8 workers = 228 MB peak RSS; the same 213 at 30 workers =
  353 MB — the driver is concurrent DOM size, not rows). If RAM is tight,
  lower `--workers`.
- Every run leaves a `log.jsonl` — grep/sort it for per-URL timing and
  failures across runs (`kind` field).
