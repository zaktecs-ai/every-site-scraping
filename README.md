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

39 tests cover schema length/uniqueness, per-field presence, "all strings"
verification, strict header==schema alignment, link-harvest correctness, the
visibility/walker regression locks, and end-to-end CSV round-trips against a
local server.

The `data_quality.py` helper audits any produced CSV and reports the exact
row, column, and value for any anomaly (wrong column count, None, duplicate
header, unsorted lists, count≠list-length):

```bash
python data_quality.py out.csv
```

---

## Validation history (real-world bug hunts)

The scraper has been hardened across three live test rounds totalling **180+
real websites**, each one followed by a deep analysis pass that found and fixed
genuine bugs. (The website lists themselves are not shipped in this repo.)

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
   returns `202 Accepted` with an empty body; the scraper wrote a blank row and
   called it "ok". Now 202 is retried then reported honestly, and an empty body
   becomes `fetch_status=empty_response`.

### Round 4 — 65 small software houses, service businesses & gov portals
(Pakistan & India software houses, municipal/government portals, dental &
medical clinics, restaurants, gyms, business directories.) Surfaced and fixed:

1. **Contacts that live only in JSON-LD were missed** — business/local pages
   frequently publish their canonical email/telephone inside schema.org
   structured data rather than visible text (e.g. caviar.pk). Added a
   **recursive** JSON-LD contact scan, now folded into `contact_emails` /
   `contact_phones` alongside visible text, `mailto:` and `tel:` links.

Count↔list consistency was **0 mismatches** across every round, and every
non-200 (403/429/500/502/timeout) is reported truthfully in
`fetch_status`/`fetch_error` — the scraper never crashes on a bad site.

---

## Project layout

```
every_site_scraping/
├── cli.py              # command-line entry point
├── multiscrape.py      # fetch + strict CSV orchestration
├── scraper.py          # structured extractor + SCHEMA + DICTIONARY
├── data_quality.py     # CSV integrity auditor
├── test_scraper.py     # 39 automated tests (incl. regression locks)
├── requirements.txt    # dependencies (requests, beautifulsoup4, lxml)
└── README.md           # this file
```

---

## Tips

- **JavaScript-heavy SPAs** render content client-side and may show sparse
  results; server-rendered pages are fully covered.
- **Bot protection** (403/429) is reported per-row, never crashes the run.
- Increase timeout for slow sites: `python cli.py URL -t 40`.
