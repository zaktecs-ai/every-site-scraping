# Every-Site Structured Scraper

Extract **complete, structured information about any website** into a single
CSV — one row per site, **90 fixed columns**, 100% deterministic (no
randomness), evidence-backed, and structurally guaranteed.

Give it a list of URLs; it fetches each page and returns a professionally
normalized row covering identity, Open Graph, Twitter cards, SEO, content, the
**actual links and URLs** (internal, external, images, documents, media,
scripts, styles, feeds, nav, social — every URL resolved to absolute), social
profiles, contact details, organization data (JSON-LD), detected technology
stack, and page weight — all in one flat table ready for Excel, Google Sheets,
or a database.

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

The extractor is built so a column can never overwrite, shift, or go missing:

1. **Fixed schema** — every row has exactly the same 90 columns, in the same
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

## Output: the 90 columns (data dictionary)

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

Plus (list-only or count-only): `script_urls`, `stylesheet_urls`, `iframe_urls`,
`rss_feeds`, `hreflang_urls`, `nav_links` (primary navigation), and the element
counts `images_count`, `images_missing_alt`, `iframes_count`, `scripts_count`,
`styles_count`, `forms_count`.

### G. Social & contact
`social_links` (ALL social profile URLs, newline list), `facebook_url`,
`linkedin_url`, `instagram_url`, `youtube_url`, `github_url` (first of each),
`mailto_links`, `tel_links`, `contact_emails` (visible text **+** `mailto:`,
deduped), `contact_phones` (visible text **+** `tel:`, deduped).

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
verification, strict header==schema alignment, link-harvest correctness, the
visibility/walker regression locks, and end-to-end CSV round-trips against a
local server. (37 tests total.)

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

## Stress-tested against a further 60 small/medium sites

A second round (`STRESS_URLS2.txt`) ran the scraper across **60 more small-to-
medium sites** — indie SaaS & dev tools (Fly.io, Render, Supabase, PlanetScale,
Neon, Prisma, Turso), open-source projects (Flask, FastAPI, SQLAlchemy,
Pydantic, pytest, mypy), frameworks (Vue, Svelte, Astro, Remix, Solid, Qwik,
Lit, Preact, Ember), design tools (Figma, Canva, Dribbble, Behance, Font
Awesome, Coolors, Google Fonts), product startups (Notion, Linear, Airtable,
Framer, Webflow, Cal, Resend, Clerk, Retool) and publishers (Smashing
Magazine, CSS-Tricks, A List Apart, Sitepoint, dev.to, DigitalOcean, Linode).

That round surfaced and fixed three more **real bugs** (all now regression-tested):

1. **`sr-only` / `visually-hidden` were treated as hidden** — these
   accessibility classes hide content *visually* but deliberately keep it in
   the DOM for crawlers, and are commonly placed on a page's real `<h1>`
   (e.g. sitepoint.com). Dropping them was data loss. Now kept.
2. **A heading inside a `<li>`/`<td>` was swallowed.** python.org's homepage
   `<h1>` lives inside `<li class="slide">`; because `li` was in the "atomic"
   set, the whole list item was emitted and the five real `<h1>`s vanished.
   Fix: **only headings** are atomic leaves; other elements use the
   container-vs-leaf rule (so a `<li>` containing an `<h1>` descends and the
   heading is captured — 5 h1s recovered on python.org).
3. **HTTP 202 (and empty 200) reported as success.** dribbble.com's bot
   defence returns `202 Accepted` with an **empty body**; the scraper marked it
   "ok" and wrote a blank row. Now 202 is retried then reported honestly, and
   an empty body becomes `fetch_status=empty_response` — never a false "ok".

Result: **56/60 fetched** into clean rows (the rest are 403/502 bot-blocks,
reported truthfully). Count↔list consistency: **0 mismatches** across all rows.

**Unit tests: 37 passing** (link-harvest, visibility/walker regression locks,
schema/integrity, end-to-end).

---

## Project layout

```
every_site_scraping/
├── cli.py              # command-line entry point
├── multiscrape.py      # fetch + strict CSV orchestration
├── scraper.py          # structured extractor + SCHEMA + DICTIONARY
├── stress_test.py      # 58-site stress driver with per-site diagnostics
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 34 automated tests (incl. link-harvest + regression locks)
├── requirements.txt    # dependencies (requests, beautifulsoup4, lxml)
├── README.md           # this file
├── TESTS.txt           # 27-site quick test list
├── STRESS_URLS.txt     # 58-site stress list (PK + USA + worldwide)
└── STRESS_URLS2.txt    # 60-site stress list (small/medium sites)
```

---

## Tips

- **JavaScript-heavy SPAs** render content client-side and may show sparse
  results; server-rendered pages are fully covered.
- **Bot protection** (403/429) is reported per-row, never crashes the run.
- Increase timeout for slow sites: `python cli.py URL -t 40`.
