"""Batch orchestration for the Every-Site Structured Scraper.

Fetches a list of URLs and writes ONE row per site into a CSV whose columns are
fixed by `scraper.SCHEMA`. Structural integrity is guaranteed by Python's
`csv.DictWriter`:

  * `fieldnames=SCHEMA`     -> column order is locked to the canonical schema
  * `extrasaction="raise"`  -> any unknown key in a row RAISES instead of being
                                silently dropped (no silent data loss)
  * `restval=""`            -> any missing key is written as "" (no shifting)

Combined with `extract_site`, which returns a schema-complete dict, a row can
never overwrite or misalign a column.
"""

from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraper import SCHEMA, extract_site  # noqa: E402

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

OK_STATUSES = {200, 201, 202, 204}
RETRY_STATUSES = {429, 500, 502, 503, 504}

_META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"']"
    r"\s*\d+\s*;\s*url\s*=\s*[\"']?([^\"'>\s]+)",
    re.I,
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _resolve_meta_refresh(html: str, base_url: str) -> Optional[str]:
    m = _META_REFRESH_RE.search(html)
    if not m:
        return None
    target = m.group(1).strip()
    if not target:
        return None
    from urllib.parse import urljoin
    return urljoin(base_url, target)


def fetch_page(url: str, timeout: int = 20, user_agent: Optional[str] = None,
               retries: int = 2) -> dict:
    """Fetch one page, returning a result dict suited for `extract_site`."""
    headers = dict(REQUEST_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent

    url = _normalize_url(url)
    result = dict(url=url, final_url=url, http_status=None, fetch_status="error",
                  fetch_error="", html="", timing=0, headers={})

    session = requests.Session()
    session.headers.update(headers)

    attempt = 0
    while True:
        attempt += 1
        t0 = time.monotonic()
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True, verify=True)
            dt = int((time.monotonic() - t0) * 1000)
            result["timing"] = dt
            result["http_status"] = resp.status_code
            result["final_url"] = resp.url
            result["headers"] = dict(resp.headers)

            if resp.status_code in OK_STATUSES:
                resp.encoding = resp.encoding or "utf-8"
                html = resp.text
                # follow meta-refresh redirects (JS-style redirects)
                for _ in range(3):
                    target = _resolve_meta_refresh(html, resp.url)
                    if not target:
                        break
                    resp = session.get(target, timeout=timeout, allow_redirects=True)
                    result["http_status"] = resp.status_code
                    result["final_url"] = resp.url
                    result["headers"] = dict(resp.headers)
                    if resp.status_code not in OK_STATUSES:
                        break
                    resp.encoding = resp.encoding or "utf-8"
                    html = resp.text
                result["html"] = html
                result["fetch_status"] = "ok"
                return result

            if resp.status_code in RETRY_STATUSES and attempt <= retries:
                time.sleep(2 * attempt)
                continue
            result["fetch_status"] = "http_error"
            result["fetch_error"] = f"HTTP {resp.status_code}"
            result["html"] = resp.text[:200000]  # keep partial for debugging
            return result

        except requests.exceptions.Timeout:
            if attempt <= retries:
                time.sleep(2 * attempt)
                continue
            result["fetch_status"] = "timeout"
            result["fetch_error"] = "Request timed out"
            return result
        except requests.exceptions.SSLError as e:
            result["fetch_status"] = "ssl_error"
            result["fetch_error"] = f"SSL error: {e}"
            return result
        except requests.exceptions.ConnectionError as e:
            result["fetch_status"] = "connection_error"
            result["fetch_error"] = f"Connection error: {e}"
            return result
        except requests.exceptions.TooManyRedirects:
            result["fetch_status"] = "error"
            result["fetch_error"] = "Too many redirects"
            return result
        except requests.exceptions.RequestException as e:
            result["fetch_status"] = "error"
            result["fetch_error"] = f"Request error: {e}"
            return result
        except Exception as e:  # noqa: BLE001 — one URL must never kill the run
            result["fetch_status"] = "error"
            result["fetch_error"] = f"Unexpected error: {e}"
            return result


def row_from_fetch(fetch: dict) -> dict:
    """Convert a fetch result into a schema-complete row."""
    return extract_site(
        fetch.get("html", ""),
        url=fetch.get("url", ""),
        http_status=fetch.get("http_status"),
        fetch_status=fetch.get("fetch_status", "ok"),
        fetch_error=fetch.get("fetch_error", ""),
        final_url=fetch.get("final_url", ""),
        response_time_ms=fetch.get("timing"),
        headers=fetch.get("headers", {}),
        page_size_bytes=len(fetch.get("html", "").encode("utf-8", errors="replace")) if fetch.get("html") else 0,
    )


def run(urls: List[str], output: str = "output.csv", timeout: int = 20,
        user_agent: Optional[str] = None) -> dict:
    """Scrape every URL into rows and write a strict CSV. Returns stats."""
    rows: List[dict] = []
    ok_count = 0

    for i, raw_url in enumerate(urls, 1):
        url = _normalize_url(raw_url)
        print(f"[{i}/{len(urls)}] {url}", file=sys.stderr)
        fetch = fetch_page(url, timeout=timeout, user_agent=user_agent)
        row = row_from_fetch(fetch)
        rows.append(row)
        if row["fetch_status"] == "ok":
            ok_count += 1
            print(f"    -> ok  {row['page_title'][:60]}  "
                  f"({row['word_count']} words)", file=sys.stderr)
        else:
            print(f"    -> {row['fetch_status']}: {row['fetch_error']}",
                  file=sys.stderr)

    write_csv(rows, output)
    return {"total": len(rows), "ok": ok_count, "columns": len(SCHEMA)}


def write_csv(rows: List[dict], path) -> str:
    """Write rows with a strict DictWriter bound to the canonical schema."""
    close = False
    if isinstance(path, (str, Path)):
        f = open(str(path), "w", newline="", encoding="utf-8-sig")
        close = True
    else:
        f = path
    try:
        writer = csv.DictWriter(
            f, fieldnames=SCHEMA, extrasaction="raise", restval=""
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    finally:
        if close:
            f.close()
    return str(path)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Scrape sites into a structured CSV.")
    p.add_argument("urls", nargs="*")
    p.add_argument("-f", "--file")
    p.add_argument("-o", "--output", default="output.csv")
    p.add_argument("-t", "--timeout", type=int, default=20)
    args = p.parse_args()

    urls = list(args.urls)
    if args.file:
        urls.extend(
            ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )

    if not urls:
        p.print_help()
        raise SystemExit(2)

    stats = run(urls, args.output, timeout=args.timeout)
    print(f"\nDone. {stats['ok']}/{stats['total']} sites ok. "
          f"{stats['columns']} columns. Output: {args.output}", file=sys.stderr)
    raise SystemExit(0 if stats["ok"] else 1)
