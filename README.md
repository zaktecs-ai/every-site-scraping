# stdout scrawler

Turn **any website into a CSV** of its visible text — no setup, no site-specific
rules. Give it one URL or a hundred; it fetches each page, finds every visible
piece of text (headings, paragraphs, list items, tables, quotes, code), and
writes it all to a single CSV file that opens in Excel, Google Sheets, or any
spreadsheet program.

You do **not** need to know anything about programming to use it.

---

## What you get

A CSV with four columns:

| Column | What it means |
| --- | --- |
| `url` | The page the text came from |
| `block_type` | What kind of text it is (`heading`, `paragraph`, `list_item`, `table_cell`, …) |
| `element_path` | The element's position in the page (a CSS-like path, handy for finding where the text lives) |
| `text` | The visible text itself |

---

## 1. Install (one time, ~1 minute)

You need Python (3.8 or newer) installed. If you don't have it, download it from
[python.org](https://www.python.org/downloads/) — during install, tick
**"Add Python to PATH"**.

Then open a terminal / command prompt and run:

```bash
pip install -r requirements.txt
```

That's the only setup step.

---

## 2. Run it

### One website

```bash
python cli.py https://example.com
```

### Several websites at once

```bash
python cli.py https://example.com https://openai.com https://stripe.com
```

### Read URLs from a file (best for long lists)

Create a plain text file, one URL per line (lines starting with `#` are ignored):

```
# urls.txt
https://example.com
https://openai.com
https://stripe.com
```

Then:

```bash
python cli.py -f urls.txt -o results.csv
```

### Choose the output file name

```bash
python cli.py https://example.com -o my_results.csv
```

When it finishes it prints how many sites succeeded and where the CSV was written.
By default the file is named `output.csv` in the same folder.

---

## 3. What it does under the hood

1. Downloads the HTML of each page, one page at a time.
2. Follows normal redirects **and** JavaScript-style redirects (a
   `<meta http-equiv="refresh">` tag).
3. Retries transient failures (a busy server, a slowdown) a couple of times
   before giving up on that site.
4. Strips out non-visible content (scripts, styles, hidden elements, page
   metadata).
5. Walks the visible page and collects each block of text, in reading order.
6. Removes exact duplicate blocks (very common on modern sites).
7. Writes everything — every site, every block — to one CSV.

If one site is down or blocked, the tool records the error and keeps going with
the rest. It never crashes on a single bad URL.

---

## 4. Project layout

```
every_site_scraping/
├── cli.py              # the command you actually run
├── multiscrape.py      # fetches pages and drives the scraper
├── scraper.py          # the text-extraction engine
├── test_scraper.py     # automated tests
├── requirements.txt    # the packages to install
├── TESTS.txt           # the 20+ tech/software sites used for the full test
├── README.md           # this file
└── .gitignore
```

---

## 5. Run the tests

```bash
python -m unittest test_scraper -v
```

You should see every test pass with `OK`.

To re-run the full 20+ site test:

```bash
python cli.py -f TESTS.txt -o test_output.csv
```

---

## 6. Test plan (20+ real sites)

`TESTS.txt` lists 27 technology and software-house websites. The scraper was run
against all of them to verify completeness and shake out bugs. Result: **26 of
27 scraped cleanly** into 2,436 text blocks; the one failure (`npmjs.com`) is a
server-side bot block, which the tool reports rather than crashing.

Bugs found and fixed during this pass:

1. **Whitespace before punctuation** — `<a>` links rendered as `"about page ."`
   with a stray space. Fixed by collecting text from the source DOM instead of
   inserting synthetic separators.
2. **Double-counted nested text** — `div > p` structures emitted parent *and*
   child. Fixed with a container-vs-leaf model in the tree walker.
3. **JS redirects ignored** — `ruby-lang.org` serves a one-line redirect page.
   Fixed by following `<meta http-equiv="refresh">` tags (now returns 61 blocks).
4. **Transient 5xx/timeouts** — `ubuntu.com` returned 503 mid-run. Fixed by
   adding bounded retries with backoff (now returns 340 blocks).

Remaining failures are environmental (bot protection / throttled datacenter
traffic), not scraper defects.

Re-run the full check at any time:

```bash
python cli.py -f TESTS.txt -o test_output.csv
```

---

## 7. Tips

- **A site shows few or no results**: some sites require JavaScript, logins, or
  block automated tools. The scraper works on classic server-rendered HTML; for
  JavaScript-only pages you'd need a browser-based tool (out of scope here).
- **Too many duplicates?** They're removed by default. If you want to see
  everything, including repeats, add `-d` (or `--keep-duplicates`).
- **Slow or failing network?** Increase the timeout: `python cli.py URL -t 60`.
