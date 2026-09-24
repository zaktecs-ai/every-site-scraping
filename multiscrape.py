"""Batch scraping engine for stdout scrawler.

Turns a list of URLs into a single CSV of text blocks. Uses only `requests`
to fetch pages and the local `scraper` module to extract text — no heavy
dependencies, no per-site configuration.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests

# Allow `python multiscrape.py` as well as import from cli.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraper import extract_text, write_csv  # noqa: E402

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Status codes we consider a successful fetch. Any others are recorded as
# errors in the output rather than crashing the run.
OK_STATUSES = {200, 201, 202, 204, 301, 302, 303, 307, 308}

# Status codes that are worth retrying (transient problems, not real errors).
RETRY_STATUSES = {429, 500, 502, 503, 504}

# A <meta http-equiv="refresh" content="0; url=..."> redirect inside the page.
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
    """Return the target URL of a <meta http-equiv=refresh> redirect, if any."""
    m = _META_REFRESH_RE.search(html)
    if not m:
        return None
    target = m.group(1).strip()
    if not target:
        return None
    return _join_url(base_url, target)


def _join_url(base: str, target: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, target)


def fetch_page(
    url: str,
    timeout: int = 20,
    user_agent: Optional[str] = None,
    retries: int = 2,
) -> dict:
    """Fetch one page and return {url, status, ok, html, error, final_url,...}.

    Performs up to `retries + 1` attempts, retrying on transient status codes
    (429/5xx) and timeouts with a short backoff. It also follows client-side
    redirects expressed as a <meta http-equiv="refresh"> tag (pages that
    redirect via JavaScript without a real 3xx response).
    """
    headers = dict(REQUEST_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent

    url = _normalize_url(url)
    result = {"url": url, "ok": False, "status": None, "html": "",
              "error": "", "final_url": url, "attempts": 0}

    session = requests.Session()
    session.headers.update(headers)

    attempt = 0
    while True:
        attempt += 1
        result["attempts"] = attempt
        try:
            resp = session.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                verify=True,
            )
            result["status"] = resp.status_code
            result["final_url"] = resp.url

            if resp.status_code in OK_STATUSES:
                resp.encoding = resp.encoding or "utf-8"
                html = resp.text

                # Follow meta-refresh redirects up to 3 hops.
                for _ in range(3):
                    target = _resolve_meta_refresh(html, resp.url)
                    if not target:
                        break
                    resp = session.get(target, timeout=timeout, allow_redirects=True)
                    result["status"] = resp.status_code
                    result["final_url"] = resp.url
                    if resp.status_code not in OK_STATUSES:
                        break
                    resp.encoding = resp.encoding or "utf-8"
                    html = resp.text

                result["html"] = html
                result["ok"] = True
                return result

            if resp.status_code in RETRY_STATUSES and attempt <= retries:
                time.sleep(2 * attempt)
                continue

            result["error"] = f"HTTP {resp.status_code}"
            return result

        except requests.exceptions.Timeout:
            if attempt <= retries:
                time.sleep(2 * attempt)
                continue
            result["error"] = "Timeout"
            return result
        except requests.exceptions.SSLError as e:
            result["error"] = f"SSL error: {e}"
            return result
        except requests.exceptions.ConnectionError as e:
            result["error"] = f"Connection error: {e}"
            return result
        except requests.exceptions.TooManyRedirects:
            result["error"] = "Too many redirects"
            return result
        except requests.exceptions.RequestException as e:
            result["error"] = f"Request error: {e}"
            return result
        except Exception as e:  # noqa: BLE001 — one URL must never kill the run
            result["error"] = f"Unexpected error: {e}"
            return result


def run(
    urls: List[str],
    output: str = "output.csv",
    timeout: int = 20,
    user_agent: Optional[str] = None,
    dedupe: bool = True,
    delay: float = 0.0,
) -> List[dict]:
    """Scrape every URL and write one combined CSV. Returns per-URL results."""
    results: List[dict] = []
    blocks: List[dict] = []
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    if user_agent:
        session.headers["User-Agent"] = user_agent

    for i, raw_url in enumerate(urls, 1):
        url = _normalize_url(raw_url)
        print(f"[{i}/{len(urls)}] Scraping {url} ...", file=sys.stderr)

        res = fetch_page(url, timeout=timeout, user_agent=user_agent)

        if res["ok"] and res["html"]:
            page_blocks = extract_text(
                res["html"], url=res.get("final_url", url), dedupe=dedupe
            )
            blocks.extend(page_blocks)
            res["block_count"] = len(page_blocks)
            results.append(res)
            print(f"    -> {len(page_blocks)} text blocks captured.",
                  file=sys.stderr)
        else:
            res["block_count"] = 0
            results.append(res)
            print(f"    -> FAILED: {res['error']}", file=sys.stderr)

        if delay > 0:
            time.sleep(delay)

    write_csv(blocks, output)
    total = len(blocks)
    print(f"\nTotal text blocks extracted: {total}", file=sys.stderr)
    return results


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Scrape a list of URLs into CSV.")
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

    raise SystemExit(
        0 if any(r["ok"] for r in run(urls, args.output, timeout=args.timeout)) else 1
    )
