"""Network layer: one fetch entry point with politeness, proxies and retries.

Everything that touches the wire lives here so the rest of the codebase never
imports ``requests`` directly. That keeps three cross-cutting concerns in one
reviewable place:

  * **Per-host politeness** — a minimum interval between requests to the same
    host (and the host's own ``Crawl-delay`` when it declares one), so a wide
    parallel run stays welcome.
  * **Proxy rotation** — a pool read from a file or a single URL, rotated
    round-robin with a per-proxy failure count so a dead proxy stops being
    tried instead of poisoning every request.
  * **Honest retries** — exponential backoff with jitter on 202/429/5xx and
    timeouts, and a *distinct* status for "HTTP 200 but no usable body"
    (a JS-only SPA or a bot challenge), because reporting that as ``ok`` is
    the single most misleading thing a bulk scraper can do.

Every response also records *which* status and *which* error, per row, so a
summary can report a real success rate rather than an optimistic one.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from .crawler import RobotsPolicy

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Only a real 200 delivers an HTML document worth parsing.
OK_STATUSES = {200}
# 202/204 are used by bot-defences to hand back an EMPTY body; 429/5xx are
# transient. All are retried, then reported honestly if they persist.
RETRY_STATUSES = {202, 429, 500, 502, 503, 504}


def normalize_url(url: str) -> str:
    """Add a scheme if missing; strip whitespace. No other rewriting."""
    url = (url or "").strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


_META_REFRESH_RE = None


def _meta_refresh_pattern():
    global _META_REFRESH_RE
    if _META_REFRESH_RE is None:
        import re
        _META_REFRESH_RE = re.compile(
            r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"']"
            r"\s*\d+\s*;\s*url\s*=\s*[\"']?([^\"'>\s]+)", re.I)
    return _META_REFRESH_RE


def resolve_meta_refresh(html: str, base_url: str) -> Optional[str]:
    """Find a ``<meta http-equiv=refresh>`` target, if any."""
    if not html:
        return None
    m = _meta_refresh_pattern().search(html)
    if not m:
        return None
    target = (m.group(1) or "").strip()
    if not target:
        return None
    return urljoin(base_url, target)


class ProxyPool:
    """Round-robin proxy pool with failure quarantine.

    A proxy that fails ``max_failures`` times is dropped for the rest of the
    run, so one dead entry in a list of 200 cannot stall the whole scrape.
    """

    def __init__(self, proxies: Optional[List[str]] = None,
                 max_failures: int = 3):
        self._proxies = [p.strip() for p in (proxies or []) if p and p.strip()]
        self._index = 0
        self._failures: Dict[str, int] = {}
        self._max_failures = max_failures
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._alive())

    def __len__(self) -> int:
        return len(self._proxies)

    def _alive(self) -> List[str]:
        return [p for p in self._proxies
                if self._failures.get(p, 0) < self._max_failures]

    def next(self) -> Optional[str]:
        with self._lock:
            alive = self._alive()
            if not alive:
                return None
            proxy = alive[self._index % len(alive)]
            self._index += 1
            return proxy

    def report(self, proxy: Optional[str], ok: bool) -> None:
        if not proxy:
            return
        with self._lock:
            if ok:
                self._failures.pop(proxy, None)
            else:
                self._failures[proxy] = self._failures.get(proxy, 0) + 1

    def stats(self) -> Dict:
        return {
            "pool_size": len(self._proxies),
            "alive": len(self._alive()),
            "quarantined": sorted(p for p, n in self._failures.items()
                                  if n >= self._max_failures),
        }

    @staticmethod
    def from_file(path: str) -> "ProxyPool":
        """Read one proxy per line; ``#`` comments and blanks ignored."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"proxy file not found: {path}")
        out: List[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            # accept "host:port", "user:pass@host:port", or a full URL
            if not line.startswith(("http://", "https://", "socks4://",
                                    "socks5://")):
                line = "http://" + line
            out.append(line)
        return ProxyPool(out)


class HostGuard:
    """Per-host minimum interval, shared across worker threads."""

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = float(min_interval or 0.0)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str, extra: float = 0.0) -> float:
        interval = max(self.min_interval, extra or 0.0)
        if interval <= 0:
            return 0.0
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            slept = 0.0
            if last is not None and (now - last) < interval:
                slept = interval - (now - last)
            self._last[host] = now + slept
        if slept > 0:
            time.sleep(slept)
            with self._lock:
                self._last[host] = time.monotonic()
        return slept


class Fetcher:
    """Thread-safe page fetcher: politeness + proxies + retries + meta-refresh.

    One instance is shared by every worker. ``requests.Session`` objects are
    per-thread (they are not documented as thread-safe), created lazily.
    """

    def __init__(self, *, timeout: int = 20, retries: int = 2,
                 user_agent: Optional[str] = None,
                 delay: float = 0.0,
                 proxy_pool: Optional[ProxyPool] = None,
                 respect_robots: bool = True,
                 verify_tls: bool = True,
                 max_response_bytes: int = 4_194_304):
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.pool = proxy_pool
        self.verify_tls = verify_tls
        self.max_response_bytes = max_response_bytes
        self.guard = HostGuard(delay)
        self.robots = RobotsPolicy(user_agent=self.user_agent, timeout=timeout,
                                   ignored=not respect_robots)
        self._local = threading.local()
        self.robots_blocked: List[str] = []
        self._blocked_lock = threading.Lock()
        self.retries_used = 0
        self._retry_lock = threading.Lock()

    # ---- per-thread session -------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(dict(REQUEST_HEADERS))
            s.headers["User-Agent"] = self.user_agent
            self._local.session = s
        return s

    def _count_retry(self) -> None:
        with self._retry_lock:
            self.retries_used += 1

    def fetch(self, url: str, *, respect_robots: Optional[bool] = None) -> Dict:
        """Fetch one URL. Never raises — every failure is a reported status."""
        url = normalize_url(url)
        result: Dict = dict(
            url=url, final_url=url, http_status=None, fetch_status="error",
            fetch_error="", html="", timing=0, headers={}, proxy=None,
            attempts=0, robots_checked=False,
        )

        check_robots = (self.robots.ignored is False if respect_robots is None
                        else respect_robots)
        if check_robots and not self.robots.ignored:
            result["robots_checked"] = True
            if not self.robots.allowed(url):
                result["fetch_status"] = "robots_disallowed"
                result["fetch_error"] = "Blocked by robots.txt"
                with self._blocked_lock:
                    self.robots_blocked.append(url)
                return result

        # host's own Crawl-delay adds to our configured floor
        crawl_delay = self.robots.delay_for(url) or 0.0
        self.guard.wait(url, extra=crawl_delay)

        session = self._session()
        attempt = 0
        while True:
            attempt += 1
            result["attempts"] = attempt
            proxy = self.pool.next() if self.pool else None
            result["proxy"] = proxy
            proxies = {"http": proxy, "https": proxy} if proxy else None

            t0 = time.monotonic()
            try:
                resp = session.get(url, timeout=self.timeout,
                                   allow_redirects=True,
                                   verify=self.verify_tls,
                                   proxies=proxies,
                                   stream=True)
                # bounded read: never buffer an unbounded body into RAM
                raw = b""
                for chunk in resp.iter_content(65536):
                    raw += chunk
                    if len(raw) >= self.max_response_bytes:
                        break
                result["timing"] = int((time.monotonic() - t0) * 1000)
                result["http_status"] = resp.status_code
                result["final_url"] = resp.url
                result["headers"] = dict(resp.headers)
                if self.pool:
                    self.pool.report(proxy, resp.status_code < 500)

                if resp.status_code in OK_STATUSES:
                    enc = resp.encoding or "utf-8"
                    html = raw.decode(enc, errors="replace")
                    html = self._follow_meta_refresh(session, html, resp.url,
                                                     result)
                    if not (html or "").strip():
                        if attempt <= self.retries:
                            self._count_retry()
                            self._sleep_backoff(attempt)
                            continue
                        result["fetch_status"] = "empty_response"
                        result["fetch_error"] = (
                            "HTTP 200 but an empty body — almost always a "
                            "JS-only page or a bot challenge, not real content")
                        result["html"] = ""
                        return result
                    result["html"] = html
                    result["fetch_status"] = "ok"
                    return result

                if resp.status_code in RETRY_STATUSES and attempt <= self.retries:
                    self._count_retry()
                    self._sleep_backoff(attempt)
                    continue

                result["fetch_status"] = "http_error"
                result["fetch_error"] = f"HTTP {resp.status_code}"
                result["html"] = raw[:200_000].decode("utf-8", errors="replace")
                return result

            except requests.exceptions.Timeout:
                if self.pool:
                    self.pool.report(proxy, False)
                if attempt <= self.retries:
                    self._count_retry()
                    self._sleep_backoff(attempt)
                    continue
                result["fetch_status"] = "timeout"
                result["fetch_error"] = f"Timed out after {self.timeout}s"
                return result
            except requests.exceptions.SSLError as e:
                result["fetch_status"] = "ssl_error"
                result["fetch_error"] = f"SSL error: {e}"
                return result
            except requests.exceptions.ProxyError as e:
                if self.pool:
                    self.pool.report(proxy, False)
                if attempt <= self.retries and self.pool:
                    self._count_retry()
                    self._sleep_backoff(attempt)
                    continue
                result["fetch_status"] = "proxy_error"
                result["fetch_error"] = f"Proxy error: {e}"
                return result
            except requests.exceptions.ConnectionError as e:
                if self.pool:
                    self.pool.report(proxy, False)
                if attempt <= self.retries:
                    self._count_retry()
                    self._sleep_backoff(attempt)
                    continue
                result["fetch_status"] = "connection_error"
                result["fetch_error"] = f"Connection error: {e}"
                return result
            except requests.exceptions.TooManyRedirects:
                result["fetch_status"] = "redirect_loop"
                result["fetch_error"] = "Too many redirects"
                return result
            except requests.exceptions.RequestException as e:
                if attempt <= self.retries:
                    self._count_retry()
                    self._sleep_backoff(attempt)
                    continue
                result["fetch_status"] = "error"
                result["fetch_error"] = f"Request error: {e}"
                return result
            except Exception as e:  # one URL must never kill a worker
                result["fetch_status"] = "error"
                result["fetch_error"] = f"Unexpected error: {e}"
                return result

    def _follow_meta_refresh(self, session, html: str, base: str,
                             result: Dict, max_hops: int = 3) -> str:
        """Follow up to ``max_hops`` meta-refresh redirects (JS-style)."""
        for _ in range(max_hops):
            target = resolve_meta_refresh(html, base)
            if not target:
                break
            try:
                resp = session.get(target, timeout=self.timeout,
                                   allow_redirects=True,
                                   verify=self.verify_tls)
                result["http_status"] = resp.status_code
                result["final_url"] = resp.url
                result["headers"] = dict(resp.headers)
                base = resp.url
                if resp.status_code not in OK_STATUSES:
                    break
                html = resp.text
            except Exception:
                break
        return html

    def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter (never a thundering herd)."""
        base = min(2 ** attempt, 16)
        time.sleep(base * (0.5 + random.random() * 0.5))


__all__ = [
    "DEFAULT_USER_AGENT", "REQUEST_HEADERS", "OK_STATUSES", "RETRY_STATUSES",
    "normalize_url", "resolve_meta_refresh", "ProxyPool", "HostGuard", "Fetcher",
]
