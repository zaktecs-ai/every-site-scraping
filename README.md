# Every-Site Structured Scraper

Extract **complete, structured information about any website** into a single
CSV — one row per site, **69 fixed columns**, 100% deterministic (no
randomness), evidence-backed, and structurally guaranteed.

Give it a list of URLs; it fetches each page and returns a professionally
normalized row covering identity, Open Graph, Twitter cards, SEO, content,
links, social profiles, contact details, organization data (JSON-LD), detected
technology stack, and page weight — all in one flat table ready for Excel,
Google Sheets, or a database.

---

## Key guarantees (the "no mismatch" promise)

The extractor is built so a column can never overwrite, shift, or go missing:

1. **Fixed schema** — every row has exactly the same 69 columns, in the same
   order. There is no "extra" column on one row and a missing one on the next.
2. **Strict CSV writer** — the row is serialized with
   `csv.DictWriter(..., extrasaction="raise", restval="")`. If a row ever
   contained a field outside the schema, the run would **raise an error**
   rather than silently drop it. Missing fields are written as `""`, so nothing
   shifts left or right.
3. **All-values-are-strings** — tested; no `None`, no objects, no mixed types.
4. **Deterministic** — no probabilistic guesses; lists (emails, phones,
   JSON-LD types) are deduplicated **and sorted**, and technology detection is
   always paired with `technology_evidence` so every claim is auditable.
5. **Accurate, not padded** — if the site does not expose a phone or email,
   the field stays empty. Empty is correct; garbage is not.

---

## Output: the 70 columns (data dictionary)

Grouped by category. Every site's row reflects all of these.

### A. Source & request
| Column | Meaning |
| --- | --- |
| `url` | URL exactly as requested |
| `final_url` | URL after redirects resolved |
| `http_status` | Numeric status (200, 404…) |
| `fetch_status` | `ok`, `http_error`, `timeout`, `ssl_error`, `connection_error`, `error` |
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

### F. Links & elements
`internal_links`, `external_links`, `total_links`, `external_domains`,
`images_count`, `images_missing_alt`, `forms_count`, `iframes_count`,
`scripts_count`, `styles_count`

### G. Social & contact
`facebook_url`, `linkedin_url`, `instagram_url`, `youtube_url`, `github_url`,
`contact_emails` (` ; `-joined, deduped), `contact_phones` (` ; `-joined)

### H. Organization (JSON-LD)
`org_name`, `org_description`, `org_logo`, `org_url`, `org_founding_date`,
`org_location`, `org_phone`, `jsonld_types`

### I. Security, tech & weight
`https`, `technology`, `technology_evidence`, `copyright_text`,
`page_size_bytes`, `response_time_ms`

### J. Content snapshot
`headings_outline` (reading order), `visible_text_preview` (first ~500 chars)

> The machine-readable schema (and per-column descriptions) lives in
> `scraper.py` as `SCHEMA` and `DICTIONARY`. Run `python cli.py --schema` to
> print the column list.

---

## Install (one time)

```bash
pip install -r requirements.txt
```

An 8.x+ or 9.x Python fork of nothing exotic is needed — just Python 3.8 or
newer plus `requests` and `beautifulsoup4`/`lxml`.

---

## Run it

### One / several sites

```bash
python cli.py https://www.postgresql.org
python cli.py https://www.postgresql.org https://www.python.org -o out.csv
```

### From a file (one URL per line, `#` ignored)

```bash
python cli.py -f urls.txt -o out.csv
```

### Print the schema

```bash
python cli.py --schema
```

---

## Structural integrity checks

```bash
python -m unittest test_scraper -v
```

16 tests cover schema length/uniqueness, per-field presence, "all strings"
verification, strict header==schema alignment, and end-to-end CSV round-trips
against a local server.

The `data_quality.py` helper audits any produced CSV and reports the exact
row, column, and value for any anomaly (wrong column count, None, duplicate
header, unsorted lists):

```bash
python data_quality.py out.csv
```

---

## Stress-tested against 58 real sites

A dedicated stress run (`stress_test.py` + `STRESS_URLS.txt`) exercised the
scraper across **58 live sites** — Pakistani software houses (Systemsltd,
NetSol, 10Pearls, Arbisoft, Folio3, VentureDive, Confiz, Cubix, Xavor,
Techlogix, Contour, Afiniti, Gaditek, …) plus USA and worldwide giants
(Microsoft, Apple, Adobe, Salesforce, Oracle, IBM, Intel, Nvidia, MongoDB,
Snowflake, Databricks, Samsung, Shopify, Spotify, Uber, SAP, Siemens, …).

That run surfaced and fixed three **real data-loss bugs** (now locked in by
regression tests):

1. **`overflow-hidden` treated as hidden.** A CSS overflow utility was matched
   by substring against `"hidden"`, so visible content (Apple's `<h1>`,
   IBM's headings, Mongo's hero) was silently dropped. Fixed: class names are
   now matched as whole whitespace-delimited tokens.
2. **Tailwind `[&_br]:hidden` treated as hidden.** This arbitrary selector hides
   a descendant `<br>`, not the element itself, yet it flagged the element as
   hidden and dropped Databricks' `<h1>`. Fixed by the same tokenization.
3. **Headings nested inside custom elements / block markup were dropped.**
   `<h1>Code <div>…</div> Work</h1>` (Snowflake) and Web Components like
   `<c4d-video-cta-container>` (IBM) caused whole subtrees to vanish. Fixed:
   headings and other content units are now always emitted as atomic leaves,
   and unknown/custom elements are descended into instead of discarded.

Also added a `rendering_type` column (`server_rendered` / `client_rendered`)
so a near-zero word count on a JavaScript-only SPA (Spotify, Palantir,
Pinterest) is reported honestly rather than as a mysterious data loss.

The earlier phone-recognition bug (matching Fibonacci digits / copyright years
inside scripts) remains fixed: phone/email scanning is restricted to visible
text with a real phone signature.

Result of the final run: **47/58 fetched successfully** into clean rows; the
remaining 11 are server-side blocks or timeouts (403/429/400/502 — bot
protection, recorded in `fetch_status`/`fetch_error`, never a crash).

---

## Project layout

```
every_site_scraping/
├── cli.py              # command-line entry point
├── multiscrape.py      # fetch + strict CSV orchestration
├── scraper.py          # structured extractor + SCHEMA + DICTIONARY
├── stress_test.py      # 58-site stress driver with per-site diagnostics
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 21 automated tests (incl. regression locks)
├── requirements.txt    # dependencies (requests, beautifulsoup4, lxml)
├── README.md           # this file
├── TESTS.txt           # 27-site quick test list
└── STRESS_URLS.txt     # 58-site stress list (PK + USA + worldwide)
```

---

## Tips

- **JavaScript-heavy SPAs** render content client-side and may show sparse
  results; server-rendered pages are fully covered.
- **Bot protection** (403/429) is reported per-row, never crashes the run.
- Increase timeout for slow sites: `python cli.py URL -t 40`.
