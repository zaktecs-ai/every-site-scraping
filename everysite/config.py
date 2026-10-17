"""Layered configuration: defaults <- config file <- environment <- CLI flags.

Precedence (low to high), matching the convention Scrapy users expect:

  1. built-in defaults
  2. ``scraper.toml`` (or ``--config PATH``), searched upward from the CWD
  3. ``SCRAPER_*`` environment variables
  4. explicit command-line flags

The point is that a team can commit one ``scraper.toml`` describing how *their*
scrapes should behave (workers, politeness, scope, output format) and every
invocation — human or CI — inherits it, while any single run can still override
anything on the command line.

TOML is parsed with ``tomllib`` on 3.11+ and a tiny built-in fallback parser for
3.8–3.10, so the package keeps its "only requests/bs4/lxml" dependency promise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore
except ImportError:  # pragma: no cover - exercised on 3.8-3.10
    _toml = None  # type: ignore

CONFIG_FILENAME = "scraper.toml"
ENV_PREFIX = "SCRAPER_"

#: Section -> option -> default. Sections mirror the CLI's mental model.
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "scrape": {
        "workers": 10,
        "timeout": 20,
        "user_agent": None,
        "retries": 2,
        "delay": 0.0,
        "respect_robots": True,
        "max_response_bytes": 4_194_304,
    },
    "crawl": {
        "max_depth": 2,
        "max_urls": 1000,
        "same_domain_only": True,
        "exact_hosts": False,
        "use_sitemaps": True,
        "strategy": "breadth",
        "include_pattern": None,
        "exclude_pattern": None,
    },
    "output": {
        "format": "csv",
        "path": "out.csv",
        "json": False,
        "append_meta": True,
    },
    "network": {
        "proxy": None,
        "proxy_file": None,
        "headers": {},
        "verify_tls": True,
    },
}

#: env var name (without prefix) -> (section, option) and a coercer
_ENV_MAP: Dict[str, tuple] = {
    "WORKERS": ("scrape", "workers", int),
    "TIMEOUT": ("scrape", "timeout", int),
    "USER_AGENT": ("scrape", "user_agent", str),
    "RETRIES": ("scrape", "retries", int),
    "DELAY": ("scrape", "delay", float),
    "RESPECT_ROBOTS": ("scrape", "respect_robots", lambda v: v.lower() in ("1", "true", "yes", "on")),
    "MAX_DEPTH": ("crawl", "max_depth", int),
    "MAX_URLS": ("crawl", "max_urls", int),
    "SAME_DOMAIN_ONLY": ("crawl", "same_domain_only", lambda v: v.lower() in ("1", "true", "yes", "on")),
    "EXACT_HOSTS": ("crawl", "exact_hosts", lambda v: v.lower() in ("1", "true", "yes", "on")),
    "USE_SITEMAPS": ("crawl", "use_sitemaps", lambda v: v.lower() in ("1", "true", "yes", "on")),
    "STRATEGY": ("crawl", "strategy", str),
    "INCLUDE_PATTERN": ("crawl", "include_pattern", str),
    "EXCLUDE_PATTERN": ("crawl", "exclude_pattern", str),
    "FORMAT": ("output", "format", str),
    "OUTPUT": ("output", "path", str),
    "PROXY": ("network", "proxy", str),
    "PROXY_FILE": ("network", "proxy_file", str),
    "VERIFY_TLS": ("network", "verify_tls", lambda v: v.lower() in ("1", "true", "yes", "on")),
}


def _mini_toml(text: str) -> Dict[str, Any]:
    """Minimal TOML subset: [section] headers, key = value, inline comments.

    Handles strings, booleans, integers, floats, arrays of scalars and
    single-level inline tables — everything the config schema uses. Not a
    general TOML parser; deliberately small so 3.8-3.10 need no dependency.
    """
    out: Dict[str, Any] = {}
    section = out
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().strip('"').strip("'")
            section = out.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().strip('"').strip("'")
        section[key] = _mini_value(val.strip())
    return out


def _mini_value(val: str) -> Any:
    if not val:
        return ""
    if val[0] in "\"'" and val[-1] == val[0] and len(val) >= 2:
        return val[1:-1]
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_mini_value(p.strip()) for p in inner.split(",")]
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        table: Dict[str, Any] = {}
        if inner:
            for part in inner.split(","):
                k, _, v = part.partition("=")
                if k.strip():
                    table[k.strip().strip('"').strip("'")] = _mini_value(v.strip())
        return table
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_toml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if _toml is not None:
        try:
            return _toml.loads(text)
        except Exception:
            pass  # fall through to the tolerant parser
    return _mini_toml(text)


def find_config(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from ``start`` looking for ``scraper.toml``."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(12):
        cand = cur / CONFIG_FILENAME
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None,
                start: Optional[Path] = None) -> Dict[str, Any]:
    """Build the effective config: defaults <- file <- environment."""
    cfg = _deep_merge(DEFAULTS, {})

    cfg_path: Optional[Path]
    if path:
        cfg_path = Path(path)
        if not cfg_path.is_file():
            raise FileNotFoundError(f"config not found: {path}")
    else:
        cfg_path = find_config(start)

    if cfg_path is not None:
        try:
            file_cfg = load_toml(cfg_path)
        except Exception as e:
            raise ValueError(f"could not parse {cfg_path}: {e}") from e
        cfg = _deep_merge(cfg, file_cfg)
        cfg["_source"] = str(cfg_path)
    else:
        cfg["_source"] = None

    # environment overrides
    for env_key, (section, option, cast) in _ENV_MAP.items():
        raw = os.environ.get(ENV_PREFIX + env_key)
        if raw is None or raw == "":
            continue
        try:
            cfg.setdefault(section, {})[option] = cast(raw)
        except (TypeError, ValueError):
            pass  # ignore a malformed env value rather than crash the run

    return cfg


def to_cli_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the config into ``argparse`` defaults."""
    scrape = cfg.get("scrape", {})
    crawl = cfg.get("crawl", {})
    output = cfg.get("output", {})
    network = cfg.get("network", {})
    return {
        "workers": scrape.get("workers", 10),
        "timeout": scrape.get("timeout", 20),
        "user_agent": scrape.get("user_agent"),
        "retries": scrape.get("retries", 2),
        "delay": scrape.get("delay", 0.0),
        "ignore_robots": not scrape.get("respect_robots", True),
        "max_depth": crawl.get("max_depth", 2),
        "max_urls": crawl.get("max_urls", 1000),
        "exact_hosts": crawl.get("exact_hosts", False),
        "no_sitemaps": not crawl.get("use_sitemaps", True),
        "strategy": crawl.get("strategy", "breadth"),
        "include_pattern": crawl.get("include_pattern"),
        "exclude_pattern": crawl.get("exclude_pattern"),
        "format": output.get("format", "csv"),
        "output": output.get("path", "out.csv"),
        "proxy": network.get("proxy"),
        "proxy_file": network.get("proxy_file"),
    }


SAMPLE_CONFIG = """\
# Every-Site Scraping configuration.
# Every value here can be overridden by a CLI flag or a SCRAPER_* env var.

[scrape]
workers = 20            # parallel workers
timeout = 25            # seconds per request
retries = 2             # retries on 202/429/5xx and timeouts
delay = 0.5             # min seconds between requests to the SAME host
respect_robots = true   # honour robots.txt (+ its Crawl-delay)
# user_agent = "MyBot/1.0 (+https://example.com/bot)"

[crawl]
max_depth = 2           # link-following depth (0 = seeds only)
max_urls = 2000         # hard cap on discovered URLs
exact_hosts = false     # true = stay on the exact host, not the whole domain
use_sitemaps = true     # expand /sitemap.xml + robots-declared sitemaps
strategy = "breadth"    # "breadth" or "depth"
# include_pattern = "/blog/"
# exclude_pattern = "/(tag|category)/"

[output]
format = "csv"          # csv | json | jsonl | sqlite
path = "out.csv"

[network]
# proxy = "http://user:pass@host:port"
# proxy_file = "proxies.txt"
verify_tls = true
"""


__all__ = [
    "CONFIG_FILENAME", "DEFAULTS", "SAMPLE_CONFIG", "load_config",
    "find_config", "to_cli_defaults", "load_toml",
]
