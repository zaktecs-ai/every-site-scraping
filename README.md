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

## Output: the 69 columns (data dictionary)

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
`character_count`

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

## Tested against 26 real sites

`TESTS.txt` lists 27 technology/software sites. Result: **26/27 scraped** into
clean 69-column rows (the one remaining → `npmjs.com` blocks automated
traffic with HTTP 403, which is recorded in `fetch_status`/`fetch_error`, not
a crash).

During development, real data surfaced and fixed a genuine accuracy bug:
phone detection initially matched digits inside scripts (Fibonacci numbers on
Python.org, copyright years). The fix restricts phone/email scanning to
**visible text only** and requires a real phone signature (`tel:` links, a
leading `+`, or `(XXX) XXX-XXXX`), so no more garbage values.

---

## Project layout

```
every_site_scraping/
├── cli.py              # command-line entry point
├── multiscrape.py      # fetch + strict CSV orchestration
├── scraper.py          # structured extractor + SCHEMA + DICTIONARY
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 16 automated tests
├── requirements.txt    # dependencies (requests, beautifulsoup4, lxml)
├── README.md           # this file
└── TESTS.txt           # 27-site test list
```

---

## Tips

- **JavaScript-heavy SPAs** render content client-side and may show sparse
  results; server-rendered pages are fully covered.
- **Bot protection** (403/429) is reported per-row, never crashes the run.
- Increase timeout for slow sites: `python cli.py URL -t 40`.
