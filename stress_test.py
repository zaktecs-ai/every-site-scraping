#!/usr/bin/env python3
"""Stress-test driver for the Every-Site Structured Scraper.

Runs the scraper across a large URL list and emits a rich per-site diagnostic
report so bugs, failures, and data loss are visible at a glance. It does not
change the scraper — it only exercises it and reports field health.

For each site it summarizes:
  * fetch result (ok/error + status)
  * which of the high-value fields are populated
  * word/character counts (a proxy for "did we actually get the content")
  * any obvious anomalies (title missing, 0 words on an ok fetch, missing OG)

Usage:
    python stress_test.py STRESS_URLS.txt -o stress_results.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiscrape import fetch_page, row_from_fetch, write_csv  # noqa: E402
from scraper import SCHEMA  # noqa: E402

# High-value fields we care about being populated on a healthy site.
KEY_FIELDS = [
    "page_title", "meta_description", "og_title", "h1_text",
    "canonical_url", "technology", "org_name", "jsonld_types",
]

ANOMALY_CHECKS = {
    "empty_title": lambda r: r["fetch_status"] == "ok" and not r["page_title"],
    "zero_words_on_ok": lambda r: r["fetch_status"] == "ok" and r["word_count"] == "0",
    "no_h1": lambda r: r["fetch_status"] == "ok" and r["h1_count"] == "0",
    "no_title_meta": lambda r: r["fetch_status"] == "ok" and not r["meta_description"],
    "no_og": lambda r: r["fetch_status"] == "ok" and not r["og_title"],
    "missing_canonical": lambda r: r["fetch_status"] == "ok" and not r["canonical_url"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url_file")
    ap.add_argument("-o", "--output", default="stress_results.csv")
    ap.add_argument("-t", "--timeout", type=int, default=30)
    args = ap.parse_args()

    urls = [
        ln.strip() for ln in Path(args.url_file).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    rows = []
    diag = []
    t_start = time.monotonic()

    for i, url in enumerate(urls, 1):
        fetch = fetch_page(url, timeout=args.timeout)
        row = row_from_fetch(fetch)
        rows.append(row)

        status = row["fetch_status"]
        populated = sum(1 for k in KEY_FIELDS if row[k])
        anomalies = [name for name, fn in ANOMALY_CHECKS.items() if fn(row)]

        flag = "OK " if status == "ok" else "FAIL"
        notes = ""
        if anomalies:
            notes = " | anomalies: " + ",".join(anomalies)

        line = (f"[{i:2d}/{len(urls)}] {flag} {url[:45]:45s} "
                f"s={row['http_status']:>3s} title={'Y' if row['page_title'] else 'N'} "
                f"words={row['word_count']:>5s} fields={populated}/{len(KEY_FIELDS)}"
                f"{notes}")
        print(line, file=sys.stderr)

        diag.append({
            "url": row["url"], "fetch_status": status,
            "http_status": row["http_status"], "fetch_error": row["fetch_error"],
            "words": row["word_count"], "fields_populated": populated,
            "anomalies": ";".join(anomalies),
        })

    write_csv(rows, args.output)

    elapsed = time.monotonic() - t_start
    ok = sum(1 for r in rows if r["fetch_status"] == "ok")
    fail = len(rows) - ok

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"STRESS TEST COMPLETE — {len(rows)} sites in {elapsed:.1f}s", file=sys.stderr)
    print(f"  ok:   {ok}", file=sys.stderr)
    print(f"  fail: {fail}", file=sys.stderr)
    for d in diag:
        if d["fetch_status"] != "ok":
            print(f"    FAIL {d['url']} -> {d['fetch_status']} {d['fetch_error']}",
                  file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Write the diagnostic summary as a second CSV for easy analysis.
    diag_path = str(Path(args.output).with_name("stress_diagnostics.csv"))
    with open(diag_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "url", "fetch_status", "http_status", "fetch_error",
            "words", "fields_populated", "anomalies",
        ])
        w.writeheader()
        for d in diag:
            w.writerow(d)

    print(f"Diagnostics written to: {diag_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
