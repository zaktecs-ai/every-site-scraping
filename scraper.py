"""Every-Site Structured Scraper — extraction engine.

This module turns a single HTML page (plus its request metadata) into ONE
dictionary row with a FIXED, canonical set of columns. Every column is always
present — either with a real value or an empty string — so output can never
"shift", overwrite, or mismatch between rows.

The schema (`SCHEMA`) lives here as the single source of truth. The CSV writer
uses `csv.DictWriter(..., extrasaction="raise")` against this exact list, which
guarantees:
  * column order is always identical across runs,
  * a row can never contain an unknown field (that raises, instead of silently
    dropping data),
  * a row can never be missing a field (they are all pre-filled to "").

Design goals:
  * Deterministic: no randomness, no probabilistic guesses. Technology
    detection is evidence-based and always paired with the evidence string.
  * Auditable: list-valued fields (emails, phones, JSON-LD types, social URLs)
    are deduplicated and sorted so the same page always yields the same row.
  * Dependency-light: requests + beautifulsoup4 + lxml only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# CANONICAL SCHEMA  (69 columns — the single source of truth)
# ---------------------------------------------------------------------------

SCHEMA: List[str] = [
    # --- A. Source & request (8) ---
    "url",                    # the URL exactly as requested
    "final_url",              # URL after any redirects
    "http_status",            # HTTP status code (string)
    "fetch_status",           # ok | http_error | timeout | ssl_error | connection_error | error
    "fetch_error",            # human-readable error (empty when ok)
    "page_title",             # <title> text
    "domain",                 # hostname only (lowercase)
    "site_name",              # best identity: og:site_name -> JSON-LD name -> title
    # --- B. Meta & Open Graph (9) ---
    "meta_description",
    "meta_keywords",
    "og_title",
    "og_description",
    "og_type",
    "og_image",
    "og_url",
    "og_site_name",
    "og_locale",
    # --- C. Twitter cards (5) ---
    "twitter_card",
    "twitter_title",
    "twitter_description",
    "twitter_image",
    "twitter_handle",
    # --- D. SEO & indexing (7) ---
    "canonical_url",
    "robots_meta",
    "viewport",
    "language",               # <html lang>
    "charset",
    "favicon",
    "generator",              # <meta name="generator">
    # --- E. Content structure (7) ---
    "h1_text",                # primary <h1> (first, stripped)
    "h1_count",
    "h2_count",
    "h3_count",
    "h4_count",
    "word_count",             # words in visible text
    "character_count",        # characters in visible text
    "rendering_type",         # server_rendered | client_rendered (JS shell) | empty
    # --- F. Links & elements (10) ---
    "internal_links",
    "external_links",
    "total_links",
    "external_domains",       # count of UNIQUE external hostnames
    "images_count",
    "images_missing_alt",
    "forms_count",
    "iframes_count",
    "scripts_count",
    "styles_count",
    # --- G. Social & contact (7) ---
    "facebook_url",
    "linkedin_url",
    "instagram_url",
    "youtube_url",
    "github_url",
    "contact_emails",         # deduped, " ; "-joined
    "contact_phones",         # deduped, " ; "-joined
    # --- H. Organization / structured data (8) ---
    "org_name",
    "org_description",
    "org_logo",
    "org_url",
    "org_founding_date",
    "org_location",
    "org_phone",
    "jsonld_types",           # deduped list of JSON-LD @type values
    # --- I. Security, tech & weight (6) ---
    "https",                  # "true" / "false"
    "technology",             # best-guess platform/CMS/framework
    "technology_evidence",    # the specific clues that led to `technology`
    "copyright_text",         # first © / "All rights reserved" line
    "page_size_bytes",
    "response_time_ms",
    # --- J. Content snapshot (2) ---
    "headings_outline",       # h1..h6 in reading order, "tag: text" joined by " | "
    "visible_text_preview",   # first ~500 chars of visible text
]

# Ensures schema edits are caught: exactly 70 columns.
assert len(SCHEMA) == 70, f"SCHEMA must be exactly 70 columns, got {len(SCHEMA)}"
_assert_no_dup = [c for c, n in Counter(SCHEMA).items() if n > 1]
assert not _assert_no_dup, f"Duplicate column names: {_assert_no_dup}"

# Human-readable data dictionary: column -> "category | what it holds".
DICTIONARY: dict = {
    "url": "Source | The URL exactly as provided by the caller.",
    "final_url": "Source | The URL after server redirects were followed.",
    "http_status": "Source | The numeric HTTP status code (e.g. 200, 404).",
    "fetch_status": "Source | ok, http_error, timeout, ssl_error, connection_error, or error.",
    "fetch_error": "Source | Plain-English reason the fetch failed (empty if ok).",
    "page_title": "Source | Text of the HTML <title> element.",
    "domain": "Source | Hostname in lowercase (e.g. example.com).",
    "site_name": "Identity | Best available site name: og:site_name, then JSON-LD name, then title.",
    "meta_description": "Meta | <meta name=description> content.",
    "meta_keywords": "Meta | <meta name=keywords> content (often empty on modern sites).",
    "og_title": "Open Graph | og:title meta.",
    "og_description": "Open Graph | og:description meta.",
    "og_type": "Open Graph | og:type meta (website, article, product…).",
    "og_image": "Open Graph | og:image URL.",
    "og_url": "Open Graph | og:url meta.",
    "og_site_name": "Open Graph | og:site_name meta.",
    "og_locale": "Open Graph | og:locale meta.",
    "twitter_card": "Twitter | twitter:card meta (summary, summary_large_image).",
    "twitter_title": "Twitter | twitter:title meta.",
    "twitter_description": "Twitter | twitter:description meta.",
    "twitter_image": "Twitter | twitter:image meta.",
    "twitter_handle": "Twitter | twitter:site / twitter:creator handle (with @).",
    "canonical_url": "SEO | <link rel=canonical> URL.",
    "robots_meta": "SEO | <meta name=robots> content.",
    "viewport": "SEO | <meta name=viewport> content.",
    "language": "SEO | <html lang> attribute.",
    "charset": "SEO | Declared character set (meta charset or Content-Type).",
    "favicon": "SEO | First <link rel=icon / apple-touch-icon> URL.",
    "generator": "SEO | <meta name=generator> content (often reveals the CMS).",
    "h1_text": "Content | Primary <h1> text (first found, whitespace-normalized).",
    "h1_count": "Content | Number of <h1> elements.",
    "h2_count": "Content | Number of <h2> elements.",
    "h3_count": "Content | Number of <h3> elements.",
    "h4_count": "Content | Number of <h4> elements.",
    "word_count": "Content | Total words in visible text.",
    "character_count": "Content | Total characters in visible text.",
    "rendering_type": "Content | server_rendered, client_rendered (JS-rendered shell), or empty.",
    "internal_links": "Links | Count of links pointing to the same domain.",
    "external_links": "Links | Count of links pointing to another domain.",
    "total_links": "Links | Total <a href> links.",
    "external_domains": "Links | Count of UNIQUE external hostnames linked.",
    "images_count": "Elements | Number of <img> elements.",
    "images_missing_alt": "Elements | Number of <img> without an alt attribute.",
    "forms_count": "Elements | Number of <form> elements.",
    "iframes_count": "Elements | Number of <iframe> elements.",
    "scripts_count": "Elements | Number of <script> elements.",
    "styles_count": "Elements | Number of <link rel=stylesheet> elements.",
    "facebook_url": "Social | Facebook profile/page URL, if linked.",
    "linkedin_url": "Social | LinkedIn profile/company URL, if linked.",
    "instagram_url": "Social | Instagram profile URL, if linked.",
    "youtube_url": "Social | YouTube channel URL, if linked.",
    "github_url": "Social | GitHub profile/org URL, if linked.",
    "contact_emails": "Contact | All visible email addresses, deduped (' ; '-joined).",
    "contact_phones": "Contact | All visible phone numbers, deduped (' ; '-joined).",
    "org_name": "Organization | Organization name (JSON-LD, fallback site_name).",
    "org_description": "Organization | Organization description (JSON-LD).",
    "org_logo": "Organization | Organization logo URL (JSON-LD or og:image).",
    "org_url": "Organization | Organization official URL (JSON-LD or canonical).",
    "org_founding_date": "Organization | foundingDate from structured data, if present.",
    "org_location": "Organization | Location/address from structured data, if present.",
    "org_phone": "Organization | telephone from structured data, if present.",
    "jsonld_types": "Organization | All JSON-LD @type values found (' ; '-joined).",
    "https": "Security | 'true' if the page was served over HTTPS.",
    "technology": "Tech | Best-guess platform/CMS/framework (evidence-based).",
    "technology_evidence": "Tech | The clues that produced `technology`.",
    "copyright_text": "Tech | First visible © / 'All rights reserved' line.",
    "page_size_bytes": "Weight | Size of the downloaded HTML in bytes.",
    "response_time_ms": "Weight | Round-trip fetch time in milliseconds.",
    "headings_outline": "Snapshot | h1–h6 headings in reading order as 'tag: text'.",
    "visible_text_preview": "Snapshot | First ~500 characters of visible text.",
}

# ---------------------------------------------------------------------------
# HTML constants
# ---------------------------------------------------------------------------

STRIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "head",
    "iframe", "canvas", "object", "embed", "audio", "video",
    "meta", "link", "select", "textarea", "input", "option",
}

# Elements treated as block-level for the purpose of the tree walk.
BLOCK_ELEMENTS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "td", "th", "dt", "dd",
    "section", "article", "aside", "header", "footer", "main",
    "figure", "figcaption", "form", "nav",
    "table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl",
    "caption", "summary", "address", "fieldset", "legend",
    "body", "html",
}

# Elements that are ALWAYS emitted as an atomic content unit, even if they
# contain another block element inside (e.g. <h1>Code <div>…</div> Work</h1>).
# These carry their own semantic text; descending into them would DROP that
# text. This distinction fixes real-world data loss on Snowflake, IBM,
# NetSolTech, and others whose headings contain nested markup.
LEAF_CONTENT = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "pre", "blockquote", "td", "th", "dt", "dd",
    "figcaption", "caption", "address", "legend", "summary",
}

_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"[\u00a0\u2007\u202f]")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone patterns: require a genuine phone signature to avoid matching years,
# version numbers, Fibonacci digits, or IDs. We look for:
#   1. an explicit <a href="tel:..."> target, or
#   2. a leading international "+" with a plausible digit count, or
#   3. a parenthesized area code (XXX) followed by digits.
_TEL_HREF_RE = re.compile(r"^tel:", re.I)
_INTL_PHONE_RE = re.compile(r"\+[0-9][0-9\s\-\.\(\)]{6,18}[0-9]")
_US_STYLE_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}")
_COPYRIGHT_RE = re.compile(r"(\u00a9|&copy;|\(c\)|Copyright|All rights reserved)[^\n]{0,120}", re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def empty_row() -> dict:
    """A row dict with every canonical column pre-filled to '' (order = SCHEMA)."""
    return {c: "" for c in SCHEMA}


def extract_site(
    html: str,
    url: str = "",
    *,
    http_status: Optional[int] = None,
    fetch_status: str = "ok",
    fetch_error: str = "",
    final_url: str = "",
    response_time_ms: Optional[int] = None,
    headers: Optional[dict] = None,
    page_size_bytes: Optional[int] = None,
) -> dict:
    """Extract a fully-populated, schema-complete row for a single page."""
    d = empty_row()
    d["url"] = url
    d["final_url"] = final_url or url
    d["http_status"] = "" if http_status is None else str(http_status)
    d["fetch_status"] = fetch_status
    d["fetch_error"] = fetch_error
    if response_time_ms is not None:
        d["response_time_ms"] = str(response_time_ms)
    if page_size_bytes is not None:
        d["page_size_bytes"] = str(page_size_bytes)
    else:
        d["page_size_bytes"] = str(len(html.encode("utf-8", errors="replace")) if html else 0)

    parsed = urlparse(final_url or url)
    d["domain"] = (parsed.hostname or "").lower()
    d["https"] = "true" if (parsed.scheme or "").lower() == "https" else "false"

    if not html:
        return d

    soup = _parse(html)
    if soup is None:
        return d

    _extract_head(soup, d, html, headers or {})
    _extract_content(soup, d, html)
    _extract_links(soup, d, parsed)
    _extract_social(soup, d, html)
    _extract_structured_data(soup, d)
    _extract_technology(soup, d, html, headers or {})
    return d


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse(html) -> Optional[Tag]:
    if not html:
        return None
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    return soup


def _meta(soup, attr: str, value: str) -> str:
    m = soup.find("meta", attrs={attr: re.compile(rf"^{re.escape(value)}$", re.I)})
    if m is None:
        m = soup.find("meta", attrs={attr: value})
    if m is None or not isinstance(m, Tag):
        return ""
    return (m.get("content") or "").strip()


def _attr_meta(soup, attr: str, value: str) -> str:
    for m in soup.find_all("meta"):
        if m.get(attr) and m.get(attr).strip().lower() == value.lower():
            return (m.get("content") or "").strip()
    return ""


def _link(soup, rel_values) -> str:
    for tag in soup.find_all("link"):
        rel = (tag.get("rel") or [])
        if isinstance(rel, str):
            rel = [rel]
        rel = {r.lower() for r in rel}
        if rel & set(rel_values):
            return (tag.get("href") or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Head / meta extraction
# ---------------------------------------------------------------------------

def _extract_head(soup: Tag, d: dict, html: str, headers: dict) -> None:
    title = soup.find("title")
    d["page_title"] = _norm(title.get_text(" ", strip=True)) if title else ""

    d["meta_description"] = _attr_meta(soup, "name", "description")
    d["meta_keywords"] = _attr_meta(soup, "name", "keywords")
    d["og_title"] = _attr_meta(soup, "property", "og:title")
    d["og_description"] = _attr_meta(soup, "property", "og:description")
    d["og_type"] = _attr_meta(soup, "property", "og:type")
    d["og_image"] = _attr_meta(soup, "property", "og:image")
    d["og_url"] = _attr_meta(soup, "property", "og:url")
    d["og_site_name"] = _attr_meta(soup, "property", "og:site_name")
    d["og_locale"] = _attr_meta(soup, "property", "og:locale")

    d["twitter_card"] = _attr_meta(soup, "name", "twitter:card")
    d["twitter_title"] = _attr_meta(soup, "name", "twitter:title")
    d["twitter_description"] = _attr_meta(soup, "name", "twitter:description")
    d["twitter_image"] = _attr_meta(soup, "name", "twitter:image")
    handle = _attr_meta(soup, "name", "twitter:site") or _attr_meta(soup, "name", "twitter:creator")
    d["twitter_handle"] = handle if handle.startswith("@") else ("@" + handle if handle else "")

    d["canonical_url"] = _link(soup, ["canonical"])
    d["robots_meta"] = _attr_meta(soup, "name", "robots")
    d["viewport"] = _attr_meta(soup, "name", "viewport")
    d["generator"] = _attr_meta(soup, "name", "generator")

    html_tag = soup.find("html")
    if html_tag is not None:
        d["language"] = (html_tag.get("lang") or "").strip()

    # Charset: from header(s) is best-effort; also from <meta charset>.
    d["charset"] = _detect_charset(soup, headers)
    d["favicon"] = _favicon(soup)

    # site_name precedence: og:site_name -> title-derived -> domain brand.
    d["site_name"] = d["og_site_name"]
    if not d["site_name"]:
        d["site_name"] = d["page_title"]
    if not d["site_name"]:
        d["site_name"] = _brand_from_domain(d["domain"])


def _detect_charset(soup: Tag, headers: dict) -> str:
    m = soup.find("meta", attrs={"charset": True})
    if m is not None and isinstance(m, Tag):
        return (m.get("charset") or "").strip()
    ct = headers.get("Content-Type", "") or headers.get("content-type", "")
    mt = re.search(r"charset\s*=\s*([\w\-]+)", ct, re.I)
    if mt:
        return mt.group(1)
    return ""


def _favicon(soup: Tag) -> str:
    for rel in (["icon", "shortcut icon"], ["apple-touch-icon"]):
        href = _link(soup, rel)
        if href:
            return href
    return ""


def _brand_from_domain(domain: str) -> str:
    """Derive a clean brand label from a hostname (best-effort, deterministic).

    Strips leading 'www' / 'wwwN' / 'm' / 'docs' / 'blog' labels and the TLD,
    then capitalizes the remaining label(s). E.g. 'www.postgresql.org' ->
    'Postgresql', 'docs.python.org' -> 'Python'.
    """
    if not domain:
        return ""
    labels = [x for x in domain.lower().split(".") if x]
    _NON_BRAND = {"www", "www1", "www2", "m", "mobile", "docs", "blog", "en",
                  "de", "es", "fr", "ja", "zh", "ru", "pt", "tr", "uk", "xen"}
    while labels and labels[0] in _NON_BRAND:
        labels.pop(0)
    # Drop the TLD.
    if labels:
        labels.pop()
    if not labels:
        labels = [domain.split(".")[0]]
    return " ".join(w.capitalize() for w in labels)


# ---------------------------------------------------------------------------
# Content extraction (headings, word counts, preview)
# ---------------------------------------------------------------------------

def _visible_blocks(soup: Tag) -> List[Tuple[str, str]]:
    """Yield (tag_name, text) for every visible leaf block, in reading order."""
    out: List[Tuple[str, str]] = []
    root = soup.find("body") or soup

    def walk(node: Tag) -> None:
        for child in list(getattr(node, "children", [])):
            if not isinstance(child, Tag) or child.name is None:
                continue
            if child.name in STRIP_TAGS:
                continue
            if _is_hidden(child):
                continue
            name = child.name
            if name in LEAF_CONTENT:
                # Always emit an atomic content unit's full text — never
                # descend, so nested markup (e.g. a <span>/<div> inside an
                # <h1>) is captured as part of the heading, not dropped.
                text = _norm(_collect_text(child))
                if text:
                    out.append((name, text))
            elif name in BLOCK_ELEMENTS:
                if _has_block_descendant(child):
                    walk(child)
                else:
                    text = _norm(_collect_text(child))
                    if text:
                        out.append((name, text))
            else:
                # Unknown or custom element (e.g. a Web Component like
                # <c4d-video-cta-container>). If it has child ELEMENTS whose
                # text we have not yet walked, recurse into it; if it is a
                # leaf with text of its own, emit that. Never throw the
                # subtree away — that caused full-page data loss on IBM.
                if any(isinstance(c, Tag) for c in getattr(child, "children", [])):
                    walk(child)
                else:
                    text = _norm(_collect_text(child))
                    if text:
                        out.append((name, text))

    walk(root)
    return out


def _extract_content(soup: Tag, d: dict, html: str) -> None:
    blocks = _visible_blocks(soup)

    h_counts = Counter()
    headings: List[str] = []
    h1s: List[str] = []
    for tag, text in blocks:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            h_counts[tag] += 1
            headings.append(f"{tag}: {text}")
            if tag == "h1":
                h1s.append(text)

    d["h1_text"] = h1s[0] if h1s else ""
    d["h1_count"] = str(h_counts["h1"])
    d["h2_count"] = str(h_counts["h2"])
    d["h3_count"] = str(h_counts["h3"])
    d["h4_count"] = str(h_counts["h4"])
    d["headings_outline"] = " | ".join(headings)

    full_text = "\n".join(text for _, text in blocks)
    d["word_count"] = str(len(full_text.split()))
    d["character_count"] = str(len(full_text))
    d["rendering_type"] = _classify_rendering(blocks, html)
    d["visible_text_preview"] = full_text[:500]

    # Copyright line
    m = _COPYRIGHT_RE.search(full_text)
    if m:
        d["copyright_text"] = _norm(m.group(0))


def _collect_text(node: Tag) -> str:
    parts: List[str] = []
    for child in node.children:
        if getattr(child, "name", None) is None and not isinstance(child, Tag):
            # NavigableString
            parts.append(str(child))
        elif isinstance(child, Tag):
            n = child.name
            if n == "br":
                parts.append(" ")
            elif n == "img":
                alt = child.get("alt")
                if alt:
                    parts.append("\u200b" + str(alt).strip() + "\u200b")
            elif n in STRIP_TAGS or _is_hidden(child):
                continue
            else:
                parts.append(_collect_text(child))
    txt = _NBSP_RE.sub(" ", "".join(parts))
    txt = re.sub(r"\s+", " ", txt).replace("\u200b", "").strip()
    return txt


def _has_block_descendant(node: Tag) -> bool:
    for desc in node.find_all(True):
        if desc is node:
            continue
        if desc.name and desc.name in BLOCK_ELEMENTS:
            return True
    return False


def _classify_rendering(blocks, html: str) -> str:
    """Classify whether the page was server-rendered or is a JS-rendered shell.

    Deterministic heuristic:
      * No HTML at all -> "empty"
      * No visible text blocks ->
          - has JS-framework markers / scripts but no body text -> "client_rendered"
          - truly empty body -> "empty"
      * Very few words AND strong JS-shell evidence -> "client_rendered"
      * Otherwise -> "server_rendered"

    This makes a near-zero word count on a healthy fetch honest: it is a
    client-side-rendered SPA whose content only appears after JavaScript runs,
    not a scraper bug.
    """
    raw = html or ""
    if not raw:
        return "empty"

    word_count = sum(len(x[1].split()) for x in blocks)
    body_text = re.sub(r"<[^>]+>", " ", _extract_body_raw(raw))
    body_text = re.sub(r"\s+", " ", body_text).strip()

    js_marker = (
        "__NEXT_DATA__" in raw
        or "__NUXT__" in raw
        or "data-reactroot" in raw.lower()
        or re.search(r"\bid=[\"']root[\"']", raw) is not None
        or re.search(r"\bid=[\"']app[\"']", raw) is not None
        or re.search(r"\bid=[\"']__next[\"']", raw) is not None
        or re.search(r"\bid=[\"']__nuxt[\"']", raw) is not None
        or "appServerConfig" in raw                       # Spotify & similar
        or "__spotify" in raw
        or "data-react-id" in raw.lower()
        or "ng-version" in raw.lower()                    # Angular marker
        or "id=\"app-root\"" in raw
    )
    has_scripts = "<script" in raw.lower()

    if not blocks:
        # No visible text at all. Distinguish "JS shell" from "nothing here".
        if js_marker or (has_scripts and not body_text):
            return "client_rendered"
        return "empty"

    if word_count <= 2 and js_marker and not body_text:
        return "client_rendered"

    return "server_rendered"


def _extract_body_raw(html: str) -> str:
    m = re.search(r"<body[^>]*>(.*?)</body>", html, re.I | re.S)
    if m:
        return m.group(1)
    return html


def _is_hidden(node: Tag) -> bool:
    if node.get("hidden") is not None:
        return True
    aria = node.get("aria-hidden")
    if aria is not None and str(aria).strip().lower() == "true":
        return True
    style = node.get("style", "")
    if style:
        s = style.replace(" ", "").lower()
        if "display:none" in s or "visibility:hidden" in s:
            return True
    cls = node.get("class")
    if cls:
        if isinstance(cls, list):
            cls = " ".join(cls)
        cl = str(cls).lower()
        # Split on WHITESPACE only — class names are whitespace-delimited
        # tokens. This correctly treats "hidden", "visually-hidden", and
        # "sr-only" as visibility classes while leaving unrelated classes
        # alone, including:
        #   * "overflow-hidden"  (a CSS overflow utility — does NOT hide)
        #   * "[&_br]:hidden"    (a Tailwind arbitrary selector that hides a
        #                         descendant <br>, NOT the element itself)
        # Both of the above previously caused false "hidden" detection and
        # dropped real content (Apple <h1>, Databricks <h1>, IBM headings).
        tokens = {t for t in cl.split() if t}
        for marker in ("hidden", "visually-hidden", "sr-only", "invisible"):
            if marker in tokens:
                return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _extract_links(soup: Tag, d: dict, base) -> None:
    base_domain = (base.hostname or "").lower()
    internal = 0
    external = 0
    ext_domains = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        p = urlparse(href)
        host = (p.hostname or "").lower()
        if not host:
            internal += 1  # relative / same-site link
        elif host == base_domain or host.endswith("." + base_domain):
            internal += 1
        else:
            external += 1
            ext_domains.add(host)
    d["internal_links"] = str(internal)
    d["external_links"] = str(external)
    d["total_links"] = str(internal + external)
    d["external_domains"] = str(len(ext_domains))

    d["images_count"] = str(len(soup.find_all("img")))
    d["images_missing_alt"] = str(sum(
        1 for img in soup.find_all("img") if not (img.get("alt") or "").strip()
    ))
    d["forms_count"] = str(len(soup.find_all("form")))
    d["iframes_count"] = str(len(soup.find_all("iframe")))
    # scripts/styles counted on the raw HTML so stripped subtrees don't undercount
    d["scripts_count"] = str(len(soup.find_all("script")))
    d["styles_count"] = str(len(
        [l for l in soup.find_all("link") if "stylesheet" in (l.get("rel") or [])]
    ))

    emails = sorted(set(_EMAIL_RE.findall(_visible_text(soup))))
    d["contact_emails"] = " ; ".join(emails)

    # Phones: ONLY from visible text, and ONLY matches with a real signature.
    visible = _visible_text(soup)
    phones = set()
    # 1. explicit tel: links (very high confidence — these ARE phone numbers)
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if _TEL_HREF_RE.match(href.strip()):
            cleaned = re.sub(r"[^\d+]", "", href[4:])
            if len(re.sub(r"\D", "", cleaned)) >= 7:
                phones.add(cleaned)
    # 2. international format in visible text
    for m in _INTL_PHONE_RE.finditer(visible):
        candidate = _norm(m.group(0))
        if 7 <= len(re.sub(r"\D", "", candidate)) <= 15:
            phones.add(candidate)
    # 3. US-style (XXX) XXX-XXXX in visible text
    for m in _US_STYLE_PHONE_RE.finditer(visible):
        candidate = _norm(m.group(0))
        if 10 <= len(re.sub(r"\D", "", candidate)) <= 11:
            phones.add(candidate)
    d["contact_phones"] = " ; ".join(sorted(phones))


# ---------------------------------------------------------------------------
# Social URLs
# ---------------------------------------------------------------------------

_SOCIAL_PATTERNS = {
    "facebook_url": ("facebook.com", None),
    "linkedin_url": ("linkedin.com", None),
    "instagram_url": ("instagram.com", None),
    "youtube_url": ("youtube.com", "youtu.be"),
    "github_url": ("github.com", None),
}


def _extract_social(soup: Tag, d: dict, html: str) -> None:
    candidates: List[str] = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.startswith("http"):
            candidates.append(href)
    for meta in soup.find_all("meta"):
        content = (meta.get("content") or "").strip()
        if content.startswith("http") and any(
            k in content.lower() for k in ("facebook", "linkedin", "instagram", "youtube", "github")
        ):
            candidates.append(content)

    found = {k: "" for k in _SOCIAL_PATTERNS}
    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        for col, (m1, m2) in _SOCIAL_PATTERNS.items():
            if found[col]:
                continue
            if host == m1 or host.endswith("." + m1) or (m2 and (host == m2 or host.endswith("." + m2))):
                found[col] = url
    for col, val in found.items():
        d[col] = val


# ---------------------------------------------------------------------------
# JSON-LD structured data
# ---------------------------------------------------------------------------

def _extract_structured_data(soup: Tag, d: dict) -> None:
    types = set()
    org: dict = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.get_text("", strip=False)
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Some pages wrap JSON-LD in a comment; try stripping that.
            cleaned = re.sub(r"^\s*<!--|-->\s*$", "", raw.strip())
            try:
                data = json.loads(cleaned)
            except (ValueError, TypeError):
                continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if t:
                if isinstance(t, list):
                    types.update(str(x) for x in t)
                else:
                    types.add(str(t))
            if not org:
                org = _org_from_item(item)

    d["jsonld_types"] = " ; ".join(sorted(types))
    if org:
        d["org_name"] = org.get("name", d["org_name"])
        d["org_description"] = org.get("description", d["org_description"])
        d["org_logo"] = org.get("logo", d["org_logo"])
        d["org_url"] = org.get("url", d["org_url"])
        d["org_founding_date"] = org.get("foundingDate", d["org_founding_date"])
        d["org_location"] = org.get("location", d["org_location"])
        d["org_phone"] = org.get("phone", d["org_phone"])

    # Fallbacks so org fields are populated even without Organization JSON-LD.
    # org_name precedence (cleanest first): JSON-LD (above) -> og:site_name ->
    # domain-derived brand -> title. We deliberately SKIP the verbose page
    # title in favour of the clean brand.
    if not d["org_name"]:
        d["org_name"] = d["og_site_name"]
    if not d["org_name"]:
        d["org_name"] = _brand_from_domain(d["domain"])
    if not d["org_name"]:
        d["org_name"] = d["page_title"]
    if not d["org_description"]:
        d["org_description"] = d["meta_description"] or d["og_description"]
    if not d["org_logo"]:
        d["org_logo"] = d["og_image"]
    if not d["org_url"]:
        d["org_url"] = d["canonical_url"] or d["final_url"]


def _org_from_item(item: dict) -> dict:
    out: dict = {}
    for key in ("name", "description"):
        if item.get(key):
            out[key] = str(item[key]).strip()
    for key in ("url", "foundingDate"):
        if item.get(key):
            out[key] = str(item[key]).strip()
    # telephone
    if item.get("telephone"):
        out["phone"] = str(item["telephone"]).strip()
    # logo (may be str or {url:...})
    logo = item.get("logo")
    if isinstance(logo, str) and logo:
        out["logo"] = logo
    elif isinstance(logo, dict) and logo.get("url"):
        out["logo"] = str(logo["url"]).strip()
    # address / location
    address = item.get("address")
    if isinstance(address, dict):
        parts = [address.get(x) for x in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
        loc = ", ".join(str(p) for p in parts if p)
        if loc:
            out["location"] = loc
    elif isinstance(item.get("location"), dict):
        loc_item = item["location"]
        addr = loc_item.get("address")
        if isinstance(addr, dict):
            parts = [addr.get(x) for x in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
            loc = ", ".join(str(p) for p in parts if p)
            if loc:
                out["location"] = loc
        elif isinstance(loc_item, str):
            out["location"] = loc_item
    return out


# ---------------------------------------------------------------------------
# Technology detection (evidence-based, deterministic)
# ---------------------------------------------------------------------------

def _extract_technology(soup: Tag, d: dict, html: str, headers: dict) -> None:
    if d["robots_meta"]:
        pass  # keep linter quiet; robots has no bearing on tech
    low = html.lower() if html else ""
    clues: List[str] = []

    gen = d["generator"].lower() if d["generator"] else ""
    gen_map = {
        "wordpress": "WordPress", "wp": "WordPress",
        "shopify": "Shopify", "squarespace": "Squarespace",
        "wix": "Wix", "webflow": "Webflow", "joomla": "Joomla!",
        "drupal": "Drupal", "ghost": "Ghost", "medium": "Medium",
        "hubspot": "HubSpot", "elementor": "Elementor",
        "typo3": "TYPO3", "prestashop": "PrestaShop", "magento": "Magento",
    }
    if gen:
        for k, v in gen_map.items():
            if k in gen:
                clues.append(f"generator:{v}")
                break

    if "wp-content" in low or "wp-includes" in low or "wp-json" in low:
        clues.append("path:wp-content")
    if "cdn.shopify.com" in low or "cdn/shopify" in low:
        clues.append("cdn:shopify")
    if "squarespace" in low:
        clues.append("mark:squarespace")
    if "webflow" in low:
        clues.append("mark:webflow")
    if "gatsby" in low or "__GATSBY" in html:
        clues.append("mark:gatsby")
    if "__NEXT_DATA__" in html or "/_next/" in low:
        clues.append("mark:next.js")
    if "__nuxt" in html or "/_nuxt/" in low:
        clues.append("mark:nuxt.js")
    if "vue" in _script_srcs(soup).lower() or "__vue__" in html:
        clues.append("mark:vue")
    if "react" in _script_srcs(soup).lower() or "data-reactroot" in low or "_reactroot" in low:
        clues.append("mark:react")
    if re.search(r"\bng-(version|app|controller)\b", html, re.I):
        clues.append("attr:angular")
    if "jquery" in _script_srcs(soup).lower():
        clues.append("lib:jquery")

    xp = headers.get("X-Powered-By") or headers.get("x-powered-by")
    if xp:
        clues.append(f"header:x-powered-by:{xp.strip()}")
    server = headers.get("Server") or headers.get("server")
    if server:
        clues.append(f"header:server:{server.strip()}")

    # Map to a single best label
    tech = _tech_label(clues)

    d["technology"] = tech
    d["technology_evidence"] = "; ".join(clues)


def _script_srcs(soup: Tag) -> str:
    return " ".join((s.get("src") or "") for s in soup.find_all("script"))


def _tech_label(clues: List[str]) -> str:
    priority = [
        "generator:", "path:wp-content", "cdn:shopify", "mark:squarespace",
        "mark:webflow", "mark:next.js", "mark:nuxt.js", "mark:gatsby",
        "mark:vue", "attr:angular", "mark:react", "lib:jquery",
        "header:x-powered-by:", "header:server:",
    ]
    ordered = [c for p in priority for c in clues if c.startswith(p)]
    if not ordered:
        return ""
    first = ordered[0]
    if first.startswith("generator:"):
        return first.split(":", 1)[1]
    if "wp-content" in first:
        return "WordPress"
    if "shopify" in first:
        return "Shopify"
    if "squarespace" in first:
        return "Squarespace"
    if "webflow" in first:
        return "Webflow"
    if "next.js" in first:
        return "Next.js"
    if "nuxt.js" in first:
        return "Nuxt.js"
    if "gatsby" in first:
        return "Gatsby"
    if "vue" in first:
        return "Vue.js"
    if "angular" in first:
        return "Angular"
    if "react" in first:
        return "React"
    if "jquery" in first:
        return "jQuery"
    return ""


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def _visible_text(soup: Tag) -> str:
    """Visible text only (script/style/stripped subtrees removed).

    Uses the same visibility rules as `_collect_text`, so contact/phone scans
    never read JavaScript, CSS, or hidden content — eliminating false matches.
    """
    return "\n".join(text for _, text in _visible_blocks(soup))


__all__ = [
    "SCHEMA", "DICTIONARY", "empty_row", "extract_site",
]
