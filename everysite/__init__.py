"""Every-Site Scraping — parallel web scraper + crawler.

Turns any list of URLs (or a seed site to crawl) into one structured row per
site across a fixed 90-column schema, with crash-safe resume and flat memory.

Public API:
    from everysite import scrape, crawl, discover, SCHEMA

    # scrape an explicit list
    result = scrape(["https://a.com", "https://b.com"], output="out.csv")

    # discover pages by crawling, then scrape them
    urls = discover(["https://example.com"], max_depth=2, max_urls=500)
    result = scrape(urls, output="site.sqlite", output_format="sqlite")

Everything is also reachable from the command line:
    python -m everysite crawl https://example.com -d 2 -n 500
"""

from __future__ import annotations

__version__ = "3.0.0"
__all__ = [
    "SCHEMA", "scrape", "crawl", "discover", "extract_site",
    "canonicalize", "fetch_robots", "SUPPORTED_FORMATS", "__version__",
]

from .scraper import SCHEMA, extract_site  # noqa: E402
from .runner import run as scrape  # noqa: E402
from .crawler import discover, canonicalize, fetch_robots  # noqa: E402
from .sinks import SUPPORTED_FORMATS  # noqa: E402


def crawl(seeds, *, output="out.csv", output_format=None, max_depth=2,
          max_urls=1000, workers=10, timeout=20, **kwargs):
    """Discover URLs from ``seeds`` by crawling, then scrape them.

    A thin convenience wrapper over :func:`discover` + :func:`scrape` for
    library users; the CLI drives the same two steps explicitly.
    """
    found = discover(seeds, max_depth=max_depth, max_urls=max_urls,
                     **{k: v for k, v in kwargs.items()
                        if k in ("exact_hosts", "include_pattern",
                                 "exclude_pattern", "use_sitemaps",
                                 "respect_robots", "strategy", "user_agent")})
    if not found.urls:
        return {"total": 0, "ok": 0, "rows": 0, "discovered": 0,
                "elapsed_s": found.elapsed_s}
    result = scrape(found.urls, output=output, workers=workers,
                    timeout=timeout, output_format=output_format,
                    **{k: v for k, v in kwargs.items()
                       if k in ("retries", "delay", "proxies", "proxy_file",
                                "respect_robots", "verify_tls", "resume",
                                "user_agent", "quiet", "progress")})
    result["discovered"] = len(found.urls)
    result["crawl"] = found.as_dict()
    return result


def main(argv=None):
    """Entry point for the console scripts."""
    from .cli import main as _main
    return _main(argv)
