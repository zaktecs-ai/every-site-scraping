# Reproducible demo

Real inputs and the real output they produced — run it yourself and compare.
Nothing here is hand-written data.

## What's here

| File | What it is |
| --- | --- |
| `urls.txt` | 5 public URLs used as input |
| `leads.csv` | the actual 90-column CSV produced from those 5 URLs |
| `leads.jsonl` | the same 5 rows as append-only typed JSON Lines |
| `discovered_urls.txt` | the 15 URLs the crawler discovered on `quotes.toscrape.com` (depth 1) |

## Reproduce it

```bash
pip install -r requirements.txt

# 1. scrape the list
python -m everysite scrape -f demo/urls.txt -o demo/leads.csv -w 5
python -m everysite audit demo/leads.csv

# 2. the same rows as typed JSONL
python -m everysite scrape -f demo/urls.txt -o demo/leads.jsonl -w 5

# 3. crawl a site and see what it discovers
python -m everysite discover https://quotes.toscrape.com -d 1 -n 15

# 4. prove the crash-safety claim on your own machine
#    (kill it part-way, then resume — row count must be complete, with no dupes)
python -m everysite scrape -f demo/urls.txt -o /tmp/resume.csv -w 5 &
sleep 1; kill -9 %1
python -m everysite scrape -f demo/urls.txt -o /tmp/resume.csv -w 5 --resume
python -m everysite audit /tmp/resume.csv
```

## What to look at in `leads.csv`

Open it in Excel or pandas — 90 columns, one row per URL. A few to spot-check:

- `page_title`, `meta_description`, `og_*`, `twitter_*` — metadata extraction
- `contact_emails`, `contact_phones`, `social_links` — contact harvesting
- `internal_links_list` / `external_links_list` — every link, absolute and sorted
- `word_count` vs `internal_links` — count columns equal their list lengths
- `fetch_status` — `ok` only when a real page body was parsed

## A note on the numbers

Your throughput will differ from the figures in the main README — it depends on
the sites, your network and your CPU. What does **not** vary is the shape of the
result: 90 columns, `ok` meaning a real page, and a complete row set after
`--resume`.
