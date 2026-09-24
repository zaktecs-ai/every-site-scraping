"""stdout scrawler — the do-everything text extractor.

This module provides `extract_text` which turns an HTML document into a *text
model* of the page: every visible block of text, with enough structural
information to rebuild a readable rendering of the page without ever having
seen the site before.

The text model is a flat list of "blocks". Each block has:
    url          the page it came from (filled in by the caller)
    block_type   paragraph | heading | list_item | table_cell | code |
                 quote | term | definition
    element_path a stable CSS-like path to the element (used to spot duplicates)
    sequence     the order the block appeared in the document
    text         the cleaned, visible text

Design goals:
  * No per-site rules, no selectors, no config. Works on unknown HTML.
  * Only "visible" text: drops <script>, <style>, <noscript>, <template>,
    <svg>, <head>, HTML comments, and elements hidden by inline styles or
    common accessibility classes. Captures image `alt` text as a bonus.
  * Faithful whitespace: text is taken from the source document, not rebuilt
    with synthetic separators, so punctuation and inline runs read correctly.
  * Deduplicates text that is rendered once but present twice in the DOM
    (a <title> and an <h1>, a mobile/desktop pair, a carousel slide duplicated
    by a JS framework).
  * Single light dependency (BeautifulSoup) on lxml for robust parsing.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Whole subtrees that must never contribute visible text.
STRIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "head",
    "iframe", "canvas", "object", "embed", "audio", "video",
    "meta", "link", "select", "textarea", "input", "option",
}

# Elements treated as block-level for the purpose of the tree walk. A block
# element that *contains* another block element is a container (we descend into
# it); a block element with no block descendants is a leaf (we emit its text).
BLOCK_ELEMENTS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "td", "th", "dt", "dd",
    "section", "article", "aside", "header", "footer", "main",
    "figure", "figcaption", "form", "nav",
    "table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl",
    "caption", "summary", "address", "fieldset", "legend",
    "body", "html",
}

# Human-friendly category for each element tag.
BLOCK_TYPE_BY_TAG = {
    "h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading",
    "h5": "heading", "h6": "heading",
    "li": "list_item",
    "pre": "code", "code": "code",
    "blockquote": "quote", "q": "quote",
    "td": "table_cell", "th": "table_cell",
    "dt": "term", "dd": "definition",
    "figcaption": "caption", "caption": "caption",
}

_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"[\u00a0\u2007\u202f]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_text(html: str, url: str = "", dedupe: bool = True) -> List[dict]:
    """Convert an HTML document into a list of visible text blocks.

    Args:
        html: The raw HTML document (bytes or str).
        url: Optional source URL, copied onto every emitted block.
        dedupe: When True (default), remove exact-duplicate text blocks.

    Returns:
        A list of dicts, each with keys:
        url, block_type, element_path, sequence, text.
    """
    root = _parse(html)
    if root is None:
        return []

    state = {"sequence": 0, "blocks": []}
    _walk(root, url, state)
    if dedupe:
        return _dedupe(state["blocks"])
    return state["blocks"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse(html) -> Optional[Tag]:
    if not html:
        return None

    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")

    # Drop script/style subtrees and comments before building the DOM. This
    # is both faster and safer than skip-lists later.
    html = re.sub(r"<script\b[^>]*>.*?</script\s*>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<style\b[^>]*>.*?</style\s*>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # html.parser attaches an <html><body> wrapper; lxml gives us real tags.
    found = soup.find("body")
    if found is not None:
        return found
    if isinstance(soup, Tag):
        return soup
    return None


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

def _walk(node: Tag, url: str, state: dict) -> None:
    for child in list(node.children):
        if not isinstance(child, Tag) or child.name is None:
            continue
        if child.name in STRIP_TAGS:
            continue
        if _is_hidden(child):
            continue

        name = child.name
        if name in BLOCK_ELEMENTS:
            if _has_block_descendant(child):
                _walk(child, url, state)
            else:
                text = _collect_visible_text(child)
                if text:
                    state["blocks"].append(
                        {
                            "url": url,
                            "block_type": BLOCK_TYPE_BY_TAG.get(name, "paragraph"),
                            "element_path": _element_path(child),
                            "sequence": state["sequence"],
                            "text": text,
                        }
                    )
                    state["sequence"] += 1
        else:
            # Inline element (a, span, strong, em, …) with no block parent
            # above it — its text is minor standalone content (e.g. a lone
            # link in a stripped-down DOM). Capture it so nothing is lost.
            text = _collect_visible_text(child)
            if text:
                state["blocks"].append(
                    {
                        "url": url,
                        "block_type": "paragraph",
                        "element_path": _element_path(child),
                        "sequence": state["sequence"],
                        "text": text,
                    }
                )
                state["sequence"] += 1


def _has_block_descendant(node: Tag) -> bool:
    """True if `node` contains any other BLOCK_ELEMENTS (deeper than itself)."""
    for descendant in node.find_all(True):
        if descendant is node:
            continue
        name = descendant.name
        if name and name in BLOCK_ELEMENTS:
            return True
    return False


# ---------------------------------------------------------------------------
# Text collection — faithful to source whitespace
# ---------------------------------------------------------------------------

def _collect_visible_text(node: Tag) -> str:
    parts: List[str] = []
    _append_text(node, parts)
    return _clean_text("".join(parts))


def _append_text(node: Tag, parts: List[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name
            if name is None:
                continue
            if name == "br":
                parts.append(" ")
            elif name == "img":
                alt = child.get("alt")
                if alt:
                    parts.append("\u200b" + str(alt).strip() + "\u200b")
            elif name in STRIP_TAGS:
                continue
            elif _is_hidden(child):
                continue
            else:
                _append_text(child, parts)


# ---------------------------------------------------------------------------
# Visibility heuristics
# ---------------------------------------------------------------------------

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
        cls_l = str(cls).lower()
        for marker in ("hidden", "visually-hidden", "sr-only"):
            if marker in cls_l:
                return True
    return False


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    txt = _NBSP_RE.sub(" ", raw)
    txt = unicodedata.normalize("NFKC", txt)
    txt = _WS_RE.sub(" ", txt)
    txt = txt.replace("\u200b", "")  # remove our img-alt sentinels
    return txt.strip()


# ---------------------------------------------------------------------------
# Element path — a stable CSS-like address
# ---------------------------------------------------------------------------

def _element_path(node: Tag) -> str:
    parts: List[str] = []
    cur = node
    while cur is not None and isinstance(cur, Tag) and cur.name is not None:
        tag = cur.name
        ident = cur.get("id")
        if ident:
            tag += "#" + str(ident).strip()
        cls = cur.get("class")
        if cls:
            if isinstance(cls, list):
                cls = ".".join(cls)
            tag += "." + str(cls).replace(" ", ".")
        parts.append(tag)
        cur = cur.parent
    parts.reverse()
    return " > ".join(parts)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedupe(blocks: List[dict]) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for b in blocks:
        key = b["text"].strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["url", "block_type", "element_path", "text"]


def write_csv(blocks: List[dict], path) -> str:
    """Write the block model to CSV. `path` may be str, Path, or a file object."""
    close = False
    if isinstance(path, (str, Path)):
        f = open(str(path), "w", newline="", encoding="utf-8")
        close = True
    else:
        f = path

    try:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for b in blocks:
            writer.writerow(
                [b.get("url", ""), b.get("block_type", ""),
                 b.get("element_path", ""), b.get("text", "")]
            )
    finally:
        if close:
            f.close()
    return str(path)
