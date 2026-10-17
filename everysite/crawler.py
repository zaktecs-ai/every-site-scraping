"""Crawl front-end: turn seed URLs into a discovered list of in-scope URLs.

This module adds *discovery* on top of the existing one-row-per-URL scraper.
The scraper answers "given a URL, what is on the page?". This answers the
question every real bulk job actually starts with: "given a site, which pages
should I scrape at all?"

Design (feature parity with the parts of Katana / Scrapy that matter here,
without pulling in a browser engine or an async framework):

  * **Breadth-first by default, depth-first optional** — a single front queue
    with an explicit depth counter, so both are the same code path.
  * **Scope control** — a page is only followed if it stays inside the allowed
    scope: same registrable domain by default, exact host optionally, and
    always subject to an explicit include/exclude regex.
  * **robots.txt** — fetched once per host, parsed for `Disallow`/`Crawl-delay`
    and honoured for both discovery and scraping. `--ignore-robots` opts out
    loudly rather than silently.
  * **sitemap discovery** — `/sitemap.xml`, `/sitemap_index.xml` and any
    `Sitemap:` lines in robots.txt are expanded, including nested indexes.
  * **Politeness** — per-host minimum interval between requests, so a crawl
    cannot hammer one origin while still running many hosts in parallel.
  * **Deterministic** — discovery order is stable for a given seed list, so a
    crawl is reproducible (same input -> same URL list).

The crawler returns an ordered, deduplicated URL list that feeds straight into
`runner.run`, so crawling and scraping share one queue, one writer and one
crash-safe resume path.
"""

from __future__ import annotations

import re
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 is a hard dependency
    BeautifulSoup = None  # type: ignore

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Extensions that are never worth crawling as a page.
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif",
    ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".exe", ".dmg", ".pkg",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ogg", ".wav",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".csv", ".sql", ".db", ".log", ".map",
}

# Query parameters that identify a tracker/session rather than content. Dropping
# them massively improves dedupe (the same page appears with endless variants).
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "twclid", "igshid",
    "mc_cid", "mc_eid", "_ga", "_gl", "ref_src", "ref_url",
    "sessionid", "session_id", "phpsessid", "jsessionid", "aspsessionid",
}

_MULTI_SLASH_RE = re.compile(r"/{2,}")


# --------------------------------------------------------------------------
# URL normalisation (crawl-side; stricter than the scrape-side version)
# --------------------------------------------------------------------------

def canonicalize(url: str, *, drop_tracking: bool = True,
                 drop_fragment: bool = True) -> str:
    """Return a canonical form of ``url`` for dedupe purposes.

    Lowercases scheme/host, strips a default port, removes trailing slashes on
    non-root paths, sorts the remaining query string and (by default) removes
    fragments and known tracking parameters. Returns ``""`` for anything that
    is not an http(s) URL.
    """
    url = (url or "").strip()
    if not url:
        return ""

    # Normalise the scheme *before* deciding anything else: an uppercase
    # "HTTPS://" or a "MAILTO:" must be classified the same as its lowercase
    # form, otherwise a prefixed scheme gets mistaken for a host.
    lowered = url.lower()
    for bad in ("mailto:", "tel:", "javascript:", "data:", "ftp:", "file:",
                "sms:", "callto:", "whatsapp:", "skype:"):
        if lowered.startswith(bad):
            return ""
    if lowered.startswith(("http://", "https://")):
        url = lowered[:lowered.index("://")] + url[len(lowered[:lowered.index("://")]):]
        scheme, rest = url.split("://", 1)
        url = scheme.lower() + "://" + rest
    elif "://" in url and not lowered.startswith(("http://", "https://")):
        return ""  # some other explicit scheme
    else:
        url = "https://" + url

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80)
                     or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = _MULTI_SLASH_RE.sub("/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = parts.query
    if query and drop_tracking:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS]
        kept.sort()
        query = urlencode(kept)

    fragment = "" if drop_fragment else parts.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1. Falls back to the host itself.

    Deliberately dependency-free: treats a small set of two-level public
    suffixes (``co.uk``, ``com.pk``, ...) as one label. Good enough for scope
    control, and never worse than the old behaviour of comparing full hosts.
    """
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    two_level = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne",
                 "go", "in", "info", "biz", "mil", "sch"}
    if labels[-2] in two_level and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _has_skip_extension(url: str) -> bool:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:] in SKIP_EXTENSIONS


def same_scope(url: str, *, allowed_domains: Set[str],
               exact_hosts: bool = False) -> bool:
    """Is ``url`` inside the crawl scope defined by ``allowed_domains``?"""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if exact_hosts:
        return host in allowed_domains
    return registrable_domain(host) in allowed_domains


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

@dataclass
class RobotsPolicy:
    """Cache of parsed robots.txt files, one per scheme+host."""

    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 15
    ignored: bool = False
    _cache: Dict[str, Optional[robotparser.RobotFileParser]] = field(
        default_factory=dict)
    _crawl_delay: Dict[str, Optional[float]] = field(default_factory=dict)
    fetched: Set[str] = field(default_factory=set)

    def _key(self, url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def policy_for(self, url: str) -> Optional[robotparser.RobotFileParser]:
        if self.ignored:
            return None
        key = self._key(url)
        if key in self._cache:
            return self._cache[key]
        rp: Optional[robotparser.RobotFileParser] = None
        try:
            resp = requests.get(key + "/robots.txt", timeout=self.timeout,
                                headers={"User-Agent": self.user_agent})
            if resp.status_code == 200 and resp.text.strip():
                rp = robotparser.RobotFileParser()
                rp.parse(resp.text.splitlines())
            # 404 / 5xx -> no rules; treat as allowed (standards behaviour)
        except Exception:
            rp = None
        self._cache[key] = rp
        self.fetched.add(key)
        # Crawl-delay, if the file declared one for us (or for *)
        delay: Optional[float] = None
        if rp is not None:
            for agent in (self.user_agent, "*"):
                try:
                    d = rp.crawl_delay(agent)  # type: ignore[arg-type]
                except Exception:
                    d = None
                if d is not None:
                    delay = float(d)
                    break
        self._crawl_delay[key] = delay
        return rp

    def allowed(self, url: str) -> bool:
        if self.ignored:
            return True
        rp = self.policy_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def delay_for(self, url: str) -> Optional[float]:
        if self.ignored:
            return None
        self.policy_for(url)
        return self._crawl_delay.get(self._key(url))

    def sitemaps(self, url: str) -> List[str]:
        """Sitemap URLs declared in robots.txt for this host."""
        rp = self.policy_for(url)
        if rp is None:
            return []
        out: List[str] = []
        try:
            for sm in rp.site_maps() or []:
                if sm and sm.startswith(("http://", "https://")):
                    out.append(sm)
        except Exception:
            pass
        return out


# --------------------------------------------------------------------------
# Politeness: one timer per host
# --------------------------------------------------------------------------

class HostRateLimiter:
    """Enforce a minimum interval between requests to the same host.

    Many hosts in parallel, one slow lane per host — the shape that keeps a
    crawl welcome while still saturating a wide seed list.
    """

    def __init__(self, min_interval: float = 0.0,
                 per_host_overrides: Optional[Dict[str, float]] = None):
        self.min_interval = float(min_interval or 0.0)
        self.per_host = dict(per_host_overrides or {})
        self._last: Dict[str, float] = {}

    def wait(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower()
        interval = self.per_host.get(host, self.min_interval)
        if interval <= 0:
            return 0.0
        now = time.monotonic()
        last = self._last.get(host)
        slept = 0.0
        if last is not None:
            delta = now - last
            if delta < interval:
                slept = interval - delta
                time.sleep(slept)
        self._last[host] = time.monotonic()
        return slept


# --------------------------------------------------------------------------
# Sitemap expansion
# --------------------------------------------------------------------------

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


def parse_sitemap_xml(text: str) -> Tuple[List[str], List[str]]:
    """Split a sitemap document into (page_urls, nested_sitemap_urls).

    Handles both `<urlset>` (pages) and `<sitemapindex>` (nested sitemaps)
    without an XML dependency, and tolerates the malformed XML real sites ship.
    """
    if not text:
        return [], []
    head = text[:2000].lower()
    locs = [m.group(1) for m in _SITEMAP_LOC_RE.finditer(text)]
    locs = [u for u in (x.strip() for x in locs) if u]
    if "<sitemapindex" in head:
        return [], locs
    return locs, []


def expand_sitemaps(seed_urls: Sequence[str], *, robots: RobotsPolicy,
                    user_agent: str, timeout: int = 15,
                    max_sitemaps: int = 50,
                    limiter: Optional[HostRateLimiter] = None) -> List[str]:
    """Discover page URLs from /sitemap.xml and robots-declared sitemaps."""
    found: List[str] = []
    seen: Set[str] = set()
    queue: List[str] = []

    for seed in seed_urls:
        base = f"{urlsplit(seed).scheme}://{urlsplit(seed).netloc}"
        queue.append(base + "/sitemap.xml")
        queue.append(base + "/sitemap_index.xml")
        for sm in robots.sitemaps(seed):
            queue.append(sm)

    processed = 0
    while queue and processed < max_sitemaps:
        sm = queue.pop(0)
        csm = canonicalize(sm)
        if not csm or csm in seen:
            continue
        seen.add(csm)
        processed += 1
        if limiter is not None:
            limiter.wait(sm)
        try:
            resp = requests.get(sm, timeout=timeout,
                                headers={"User-Agent": user_agent})
            if resp.status_code != 200:
                continue
            pages, nested = parse_sitemap_xml(resp.text)
        except Exception:
            continue
        for p in pages:
            cp = canonicalize(p)
            if cp and cp not in seen:
                seen.add(cp)
                found.append(cp)
        for n in nested:
            if len(queue) + processed < max_sitemaps:
                queue.append(n)
    return found


# --------------------------------------------------------------------------
# Discovery crawl
# --------------------------------------------------------------------------

@dataclass
class CrawlResult:
    urls: List[str]
    pages_fetched: int
    discovered: int
    skipped_robots: int
    skipped_scope: int
    skipped_extension: int
    depth_reached: int
    errors: Dict[str, int]
    elapsed_s: float
    sitemap_urls: int = 0

    def as_dict(self) -> Dict:
        return {
            "urls_discovered": self.discovered,
            "pages_fetched": self.pages_fetched,
            "sitemap_urls": self.sitemap_urls,
            "skipped_by_robots": self.skipped_robots,
            "skipped_out_of_scope": self.skipped_scope,
            "skipped_extension": self.skipped_extension,
            "depth_reached": self.depth_reached,
            "errors": self.errors or None,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def discover(
    seeds: Sequence[str],
    *,
    max_depth: int = 2,
    max_urls: int = 1000,
    same_domain_only: bool = True,
    exact_hosts: bool = False,
    include_pattern: Optional[str] = None,
    exclude_pattern: Optional[str] = None,
    use_sitemaps: bool = True,
    respect_robots: bool = True,
    strategy: str = "breadth",
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: int = 15,
    delay: float = 0.0,
    progress=None,
) -> CrawlResult:
    """Crawl the seeds and return an ordered, deduplicated, in-scope URL list."""
    t0 = time.monotonic()
    robots = RobotsPolicy(user_agent=user_agent, timeout=timeout,
                          ignored=not respect_robots)
    limiter = HostRateLimiter(min_interval=delay)

    inc = re.compile(include_pattern, re.I) if include_pattern else None
    exc = re.compile(exclude_pattern, re.I) if exclude_pattern else None

    # ---- establish scope from the seeds ----
    allowed: Set[str] = set()
    for s in seeds:
        c = canonicalize(s)
        if not c:
            continue
        host = (urlsplit(c).hostname or "").lower()
        allowed.add(host if exact_hosts else registrable_domain(host))

    # ---- seed the frontier ----
    frontier: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    for s in seeds:
        c = canonicalize(s)
        if c and c not in seen:
            seen.add(c)
            frontier.append((c, 0))

    # ---- optional sitemap expansion (depth 0, still in scope) ----
    sitemap_count = 0
    if use_sitemaps:
        sm_urls = expand_sitemaps(
            [c for c, _ in frontier], robots=robots, user_agent=user_agent,
            timeout=timeout, limiter=limiter)
        for u in sm_urls:
            if u in seen:
                continue
            seen.add(u)
            frontier.append((u, 1))
            sitemap_count += 1

    out: List[str] = []
    fetched = 0
    depth_reached = 0
    skipped_robots = skipped_scope = skipped_ext = 0
    errors: Dict[str, int] = {}

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent,
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9"})

    # depth-first = LIFO frontier, breadth-first = FIFO frontier
    pop_index = -1 if strategy == "depth" or strategy == "depth-first" else 0

    while frontier and len(out) < max_urls:
        url, depth = frontier.pop(pop_index)
        if depth > max_depth:
            continue
        if inc is not None and not inc.search(url):
            continue
        if exc is not None and exc.search(url):
            continue
        if not robots.allowed(url):
            skipped_robots += 1
            continue

        out.append(url)
        depth_reached = max(depth_reached, depth)
        if progress:
            try:
                progress(len(out), max_urls, f"crawl d{depth} {url}")
            except Exception:
                pass

        if depth >= max_depth:
            continue

        limiter.wait(url)
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            fetched += 1
        except Exception as e:
            key = type(e).__name__
            errors[key] = errors.get(key, 0) + 1
            continue
        if resp.status_code != 200 or "html" not in resp.headers.get(
                "Content-Type", "").lower():
            continue

        try:
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception:
            errors["parse_error"] = errors.get("parse_error", 0) + 1
            continue

        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:",
                                            "javascript:", "data:")):
                continue
            nxt = canonicalize(urljoin(resp.url, href))
            if not nxt or nxt in seen:
                continue
            if _has_skip_extension(nxt):
                skipped_ext += 1
                continue
            if same_domain_only and not same_scope(
                    nxt, allowed_domains=allowed, exact_hosts=exact_hosts):
                skipped_scope += 1
                continue
            seen.add(nxt)
            frontier.append((nxt, depth + 1))

    return CrawlResult(
        urls=out[:max_urls],
        pages_fetched=fetched,
        discovered=len(out),
        skipped_robots=skipped_robots,
        skipped_scope=skipped_scope,
        skipped_extension=skipped_ext,
        depth_reached=depth_reached,
        errors=errors,
        elapsed_s=time.monotonic() - t0,
        sitemap_urls=sitemap_count,
    )


# --------------------------------------------------------------------------
# Simple probes that make the tool useful without a full crawl
# --------------------------------------------------------------------------

def fetch_robots(url: str, *, user_agent: str = DEFAULT_USER_AGENT,
                 timeout: int = 15) -> Dict:
    """Return the parsed robots.txt facts for one site (never raises)."""
    rp = RobotsPolicy(user_agent=user_agent, timeout=timeout)
    host_root = f"{urlsplit(canonicalize(url)).scheme}://{urlsplit(canonicalize(url)).netloc}"
    text = ""
    status = None
    try:
        resp = requests.get(host_root + "/robots.txt", timeout=timeout,
                            headers={"User-Agent": user_agent})
        status = resp.status_code
        text = resp.text if resp.status_code == 200 else ""
    except Exception:
        status = None

    disallow: List[str] = []
    allow: List[str] = []
    sitemaps: List[str] = []
    delay = None
    if text:
        current_agent = None
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "user-agent":
                current_agent = val.lower()
            elif key == "disallow" and current_agent in ("*", user_agent.lower()) and val:
                disallow.append(val)
            elif key == "allow" and current_agent in ("*", user_agent.lower()) and val:
                allow.append(val)
            elif key == "sitemap" and val:
                sitemaps.append(val)
            elif key == "crawl-delay" and val:
                try:
                    delay = float(val)
                except ValueError:
                    pass
    return {
        "root": host_root,
        "robots_status": status,
        "robots_present": bool(text),
        "disallow_rules": disallow[:50],
        "allow_rules": allow[:50],
        "sitemaps": sitemaps[:20],
        "crawl_delay": delay,
        "note": "robots.txt is advisory to well-behaved crawlers; it is not "
                "a legal authorisation and does not override a site's terms.",
    }


__all__ = [
    "canonicalize", "registrable_domain", "same_scope", "RobotsPolicy",
    "HostRateLimiter", "parse_sitemap_xml", "expand_sitemaps", "CrawlResult",
    "discover", "fetch_robots",
]
