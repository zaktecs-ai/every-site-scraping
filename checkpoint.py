"""Crash-safe incremental output + checkpointing for the parallel scraper.

Guarantees:

  * **Row-per-completion flushing** — every finished site is appended and
    flushed to the CSV immediately. A crash (kill -9, power loss, OOM)
    loses at most the site in flight, never the batch.
  * **Trailing-row repair** — a crash mid-append can leave a truncated
    final line; on resume, the loader detects and removes it, so the CSV
    is always schema-clean.
  * **Atomic JSON checkpoint** — ``checkpoint.json`` is written via
    ``tmp + os.replace`` (atomic on POSIX), so it is never half-written.
  * **Resume** — a rerun skips every URL already present in the output
    CSV (matched on the ``url`` column), and appends only the remainder.

Conventions:

  * Output CSV is ``utf-8-sig`` (Excel-friendly) with ``\\r\\n`` row ends.
  * The checkpoint tracks state for observability, but resume itself
    derives its done-set from the ACTUAL CSV rows — the CSV is the source
    of truth, so a partially flushed checkpoint can never cause data loss
    or duplicate work.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, TextIO

from scraper import SCHEMA

# ---------------------------------------------------------------------------
# Crash-safe incremental CSV writer
# ---------------------------------------------------------------------------


class SafeCSVWriter:
    """Appends schema-complete rows to CSV, flushing + fsync after each.

    Thread-safety: rows are appended from the collector thread only (the
    parallel engine funnels completions through a single writer thread),
    so no lock is needed here.
    """

    def __init__(self, path: str, write_header: bool):
        self.path = str(path)
        self.rows_written = 0
        self.bytes_written = 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # newline="" is required so csv's \r\n isn't doubled on Windows.
        self._fh: TextIO = open(self.path, "a", newline="", encoding="utf-8-sig")
        if write_header:
            # Only when starting fresh (file was absent or empty).
            self._fh.write(",".join(SCHEMA) + "\r\n")
            self._fh.flush()
        self._writer = csv.DictWriter(
            self._fh, fieldnames=SCHEMA, extrasaction="raise", restval=""
        )

    def write_row(self, row: Dict[str, str]) -> None:
        """Append one row, flush to OS and to disk immediately."""
        self._writer.writerow(row)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.rows_written += 1
        self.bytes_written += self._fh.tell()

    def close(self) -> None:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except (OSError, ValueError):
            pass
        finally:
            self._fh.close()

    @property
    def is_fresh_file(self) -> bool:
        return self.rows_written == 0 and self._fh.tell() == 0 and not Path(self.path).stat().st_size


# ---------------------------------------------------------------------------
# Checkpoint (atomic JSON)
# ---------------------------------------------------------------------------


class Checkpoint:
    """Lightweight atomic JSON checkpoint written after every batch.

    It exists for observability and quick state inspection — resume does
    NOT depend on it (the CSV is the source of truth), so a torn or
    missing checkpoint file can never corrupt a run.
    """

    def __init__(self, path: str):
        self.path = str(path)
        self._last_write = 0.0
        self.state: Dict = {}

    def load(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state = data
        except (OSError, ValueError):
            self.state = {}
        return self.state

    def save(self, payload: Dict, *, force: bool = False, min_interval_s: float = 2.0) -> bool:
        """Atomically write the checkpoint (rate-limited unless force=True)."""
        now = time.monotonic()
        if not force and (now - self._last_write) < min_interval_s:
            return False
        self.state = payload
        tmp = self.path + ".tmp"
        try:
            Path(tmp).parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # atomic on POSIX
            self._last_write = now
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Resume: derive the done-set from the actual CSV
# ---------------------------------------------------------------------------


def read_completed_urls(csv_path: str) -> Set[str]:
    """Every ``url`` already in the output CSV, after repairing a torn tail.

    Handles three realities of a crashed append:

      1. A truncated final line (crash mid-write) — dropped.
      2. A complete-but-unquoted row with wrong column count — dropped.
      3. Anything after a torn line is impossible by construction
         (appends are strictly serial), so we stop at the first bad line.
    """
    p = Path(csv_path)
    done: Set[str] = set()
    if not p.exists() or p.stat().st_size == 0:
        return done

    # Repair pass: drop a torn trailing row, if any.
    try:
        _repair_torn_tail(csv_path)
    except OSError:
        pass

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return done
        if header != SCHEMA:
            # Not our file / wrong schema → treat as fresh (caller decides).
            return done
        try:
            url_idx = header.index("url")
        except ValueError:
            return done
        for row in reader:
            if len(row) != len(SCHEMA):
                continue  # tolerate rather than crash
            done.add(row[url_idx].strip())
    return done


def _repair_torn_tail(csv_path: str) -> Optional[int]:
    """Remove a truncated trailing row. Returns removed byte count or None.

    A crash during ``write_row`` can only ever damage the LAST row because
    appends are serial. Every complete write (header and row alike) ends
    with ``\\r\\n`` — and ``\\r\\n`` never occurs INSIDE a row (list cells
    embed plain ``\\n``, which the csv writer always quotes). Therefore:

      * if the file does not end with ``\\r\\n``, the trailing fragment
        after the last ``\\r\\n`` is a torn write → drop it;
      * if there is no ``\\r\\n`` at all, the entire file is one torn
        write → reset it to empty (resume then treats it as fresh).

    No CSV parsing is needed — this stays correct even when the cut lands
    inside a quoted multi-line cell (where a naive field-count check would
    wrongly accept the torn row).
    """
    p = Path(csv_path)
    size = p.stat().st_size
    if size == 0:
        return None
    with open(csv_path, "rb") as f:
        # Read the last 64KB — plenty for the longest legal row remainder.
        f.seek(max(0, size - 65536))
        tail = f.read()
    pos = tail.rfind(b"\r\n")
    if pos == -1:
        # No complete line at all → the whole file is a single torn write.
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            pass  # empty; read_completed_urls / runner treat it as fresh
        return size
    fragment = tail[pos + 2:]
    if not fragment:
        return None  # file already ends with a complete row
    with open(csv_path, "r+b") as f:
        f.truncate(size - len(fragment))
    return len(fragment)


# ---------------------------------------------------------------------------
# Crash-safe incremental JSON writer (typed, structured, one site per object)
# ---------------------------------------------------------------------------

# Columns written as real JSON arrays (newline-separated in CSV; see
# scraper.LIST_COLUMNS / LIST_DELIM — the single source of truth).
_JSON_LIST_COLUMNS = None  # lazily imported from scraper to avoid a cycle


def _json_list_columns() -> set:
    global _JSON_LIST_COLUMNS
    if _JSON_LIST_COLUMNS is None:
        from scraper import LIST_COLUMNS  # local import: avoid circular import
        _JSON_LIST_COLUMNS = set(LIST_COLUMNS)
    return _JSON_LIST_COLUMNS


def row_to_json_site(row: Dict[str, str]) -> Dict:
    """Convert one CSV row (all-strings) into a typed, structured site object.

    Types are deterministic and derived from the canonical schema:
      * list columns  -> real arrays  (split on the newline delimiter)
      * count columns -> int (with paired lists, so count == len(list))
      * "https"       -> bool
      * ms/bytes cols -> int
      * everything else -> str
    """
    from scraper import COUNT_LIST_PAIRS, LIST_DELIM
    lists = _json_list_columns()
    site: Dict = {}
    for col, val in row.items():
        if col in lists:
            site[col] = [p for p in (val or "").split(LIST_DELIM) if p] if val else []
        elif col in ("https",):
            site[col] = (val == "true")
        elif col in ("http_status", "h1_count", "h2_count", "h3_count",
                     "h4_count", "internal_links", "external_links",
                     "total_links", "external_domains", "all_urls_count",
                     "image_urls_count", "images_count", "images_missing_alt",
                     "document_links_count", "media_urls_count", "iframes_count",
                     "scripts_count", "styles_count", "forms_count",
                     "page_size_bytes", "response_time_ms",
                     "word_count", "character_count"):
            site[col] = int(val) if str(val).lstrip("-").isdigit() else _num_or_raw(val)
        else:
            site[col] = val if val is not None else ""
    return site


def _num_or_raw(val) -> object:
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return float(val)
        except (TypeError, ValueError):
            return val


class SafeJSONWriter:
    """Incremental JSON-lines-of-objects writer, crash-safe like the CSV.

    The file is a single valid JSON document::

        {"meta": {...}, "sites": [ {...}, {...}, ... ]}

    BUT it is built incrementally and kept valid after every site by
    rewriting the small tail (``]}`` closer + start of the row was complex
    to keep atomic) — instead, on disk it is stored as concatenated
    one-object-per-line JSONL *with a wrapper header*, and finalized on
    close() into a single JSON document. To stay crash-safe, every append
    is flushed + fsynced immediately; if a crash leaves a torn last line
    or a missing closer, `repair_json_file()` heals it on resume.
    """

    _OPENER = '{"meta": '
    _SITES_KEY = ', "sites": ['

    def __init__(self, path: str, meta: Dict, write_header: bool = True):
        self.path = str(path)
        self.meta = meta
        self.rows_written = 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # "a" keeps prior lines on resume; header only when starting fresh.
        self._fh: TextIO = open(self.path, "a", encoding="utf-8")
        if write_header:
            self._fh.write(self._OPENER + json.dumps(meta, ensure_ascii=False) +
                           self._SITES_KEY + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def write_row(self, row: Dict[str, str]) -> None:
        line = json.dumps(row_to_json_site(row), ensure_ascii=False,
                          default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.rows_written += 1

    def close(self) -> None:
        """Finalize into a single valid JSON document (atomic replace)."""
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except (OSError, ValueError):
            pass
        self._fh.close()
        finalize_json_file(self.path)

    @property
    def is_fresh_file(self) -> bool:
        return self.rows_written == 0 and not Path(self.path).exists()


def finalize_json_file(path: str) -> int:
    """Convert the JSONL-on-disk form into one valid JSON document.

    Idempotent: a file that is already a finalized document is returned
    as-is (site count). Torn trailing lines are dropped.
    """
    p = Path(path)
    if not p.exists():
        return 0
    raw = p.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return 0
    # already finalized?
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict) and isinstance(doc.get("sites"), list):
            return sum(1 for s in doc["sites"] if isinstance(s, dict))
    except ValueError:
        pass
    # NOTE: split on real "\n" only — str.splitlines() ALSO splits on
    # \u2028/\u2029/\x85/\x0b/\x0c, which can legally appear INSIDE a JSON
    # string (json.dumps never escapes them with ensure_ascii=False), so
    # splitlines() can break a single site line in two and lose the site.
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    meta: Dict = {}
    sites: List[Dict] = []
    started = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i == 0 and s.startswith('{"meta":'):
            # wrapper line: {"meta": {...}, "sites": [
            m = re.match(r'^\{"meta": (.*), "sites": \[$', s)
            if m:
                try:
                    meta = json.loads(m.group(1))
                except ValueError:
                    meta = {}
                started = True
                continue
        try:
            sites.append(json.loads(s))
        except ValueError:
            continue  # torn / junk line dropped
    doc = {"meta": meta, "sites": sites}
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(path))
    return len(sites)


def _write_jsonl_form(path: str, meta: Dict, sites: List[Dict]) -> None:
    """Write the appendable JSONL-on-disk form atomically."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(SafeJSONWriter._OPENER + json.dumps(meta, ensure_ascii=False) +
                SafeJSONWriter._SITES_KEY + "\n")
        for s in sites:
            f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(path))


def repair_json_file(path: str) -> int:
    """Heal a JSON output into the appendable JSONL-on-disk form.

    Handles every on-disk state:
      1. a FINALIZED single JSON document (a previous complete run) —
         converted back to the appendable form so a resumed run can append;
      2. a FINALIZED document damaged by a torn append (crash after close
         — the tail is garbage) — intact site objects are salvaged by a
         string-aware brace scan of the "sites" array region;
      3. the JSONL form with a torn last line (a crash mid-append) — the
         torn line is dropped, intact lines kept;
      4. missing/empty — treated as fresh (0 sites).

    Returns the count of intact site objects.
    """
    p = Path(path)
    if not p.exists():
        return 0
    raw = p.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return 0

    # 1) intact finalized document form
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict) and isinstance(doc.get("sites"), list):
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
            sites = [s for s in doc["sites"] if isinstance(s, dict)]
            _write_jsonl_form(str(path), meta, sites)
            return len(sites)
    except ValueError:
        pass

    # 2) JSONL-on-disk form (possibly torn). Real-"\n" split — see NOTE in
    #    finalize_json_file: splitlines() would also split on \u2028 etc.
    lines = [ln for ln in raw.split("\n") if ln.strip()]

    def _is_site_line(s: str) -> bool:
        s = s.strip()
        if not s.startswith("{") or not s.endswith("}"):
            return False
        try:
            return isinstance(json.loads(s), dict)
        except ValueError:
            return False

    # 2) JSONL-on-disk form? (single-line header: {"meta": ..., "sites": [)
    if lines and re.match(r'^\{"meta": .*"sites": \[$', lines[0].strip()):
        body = lines[1:]
        if body and not _is_site_line(body[-1]):
            body = body[:-1]              # drop torn last line
        meta: Dict = {}
        m = re.match(r'^\{"meta": (.*), "sites": \[$', lines[0].strip())
        if m:
            try:
                meta = json.loads(m.group(1))
            except ValueError:
                meta = {}
        sites = []
        for ln in body:
            s = ln.strip()
            if _is_site_line(s):
                try:
                    sites.append(json.loads(s))
                except ValueError:
                    continue
        _write_jsonl_form(str(path), meta, sites)
        return len(sites)

    # 3) damaged finalized document (pretty-printed, torn tail): salvage
    #    every parseable top-level object inside the "sites" array with a
    #    STRING-AWARE brace scan (braces inside string values are ignored).
    sites_salvaged = _salvage_sites_from_text(raw)
    meta_salvaged: Dict = {}
    midx = raw.find('"meta"')
    sidx = raw.find('"sites"')
    if midx != -1 and sidx != -1 and sidx > midx:
        seg = raw[midx + 7: sidx].strip().rstrip(",").strip()
        if seg.endswith("}"):
            seg = seg[: seg.rfind("}") + 1]
        elif seg.endswith("]") and not seg.endswith("}"):
            # meta object ended earlier; cut at its closing brace
            cut = seg.rfind("}")
            seg = seg[: cut + 1] if cut != -1 else seg
        try:
            meta_salvaged = json.loads(seg)
        except ValueError:
            meta_salvaged = {}
    _write_jsonl_form(str(path), meta_salvaged, sites_salvaged)
    return len(sites_salvaged)


def _salvage_sites_from_text(text: str) -> List[Dict]:
    """Brace-scan the ``"sites": [ ... ]`` region, string-aware.

    Braces inside string values never affect depth; every top-level
    ``{...}`` object found is parsed and kept only if it is a dict.
    """
    idx = text.find('"sites"')
    if idx == -1:
        return []
    after = text[idx + len('"sites"'):]
    opener = after.find("[")
    if opener == -1:
        return []
    body = after[opener + 1:]
    out: List[Dict] = []
    i, n = 0, len(body)
    depth = 0
    in_str = False
    esc = False
    start = -1
    while i < n:
        c = body[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    obj = json.loads(body[start: i + 1])
                    if isinstance(obj, dict):
                        out.append(obj)
                except ValueError:
                    pass
                start = -1
            elif depth < 0:
                break                 # past the array closer — done
        i += 1
    return out


def count_json_sites(path: str) -> int:
    """Count intact site objects in a JSON output (either on-disk form).

    Handles both the finalized single-document form (counts ``sites``)
    and the JSONL-on-disk form (counts object lines, torn line ignored).
    """
    p = Path(path)
    if not p.exists():
        return 0
    raw = p.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return 0
    # finalized document form?
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict) and isinstance(doc.get("sites"), list):
            return sum(1 for s in doc["sites"] if isinstance(s, dict))
    except ValueError:
        pass
    # JSONL-on-disk form (real-"\n" split — see NOTE in finalize_json_file)
    n = 0
    for ln in raw.split("\n"):
        s = ln.strip()
        if not s or s.startswith('{"meta":'):
            continue
        if s.startswith("{") and s.endswith("}"):
            try:
                json.loads(s)
                n += 1
            except ValueError:
                pass
    return n


def rebuild_json_from_csv(csv_path: str, json_path: str, meta: Dict) -> int:
    """Rebuild the JSON output from the (source-of-truth) CSV.

    Returns the number of sites rebuilt.
    """
    sites: List[Dict] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sites.append(row_to_json_site(row))
    except OSError:
        sites = []
    doc = {"meta": meta, "sites": sites}
    tmp = str(json_path) + ".tmp"
    Path(tmp).parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(json_path))
    return len(sites)


def load_urls_csv(csv_path: str) -> List[str]:
    """Read a single-column (or url-titled) CSV of URLs."""
    out: List[str] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            val = (row[0] or "").strip()
            if val and not val.startswith("#") and _looks_like_url(val):
                out.append(val)
    return out


def load_urls_txt(txt_path: str) -> List[str]:
    """Read a plain-text URL list (one per line, # comments ignored)."""
    out: List[str] = []
    for ln in Path(txt_path).read_text(encoding="utf-8", errors="replace").splitlines():
        v = ln.strip()
        if v and not v.startswith("#") and _looks_like_url(v):
            out.append(v)
    return out


_URL_RE_MIN = re.compile(r"^(https?://|www\.|[a-z0-9\-]+\.[a-z]{2,})", re.I)


def _looks_like_url(v: str) -> bool:
    return bool(_URL_RE_MIN.match(v))


_HEADER_NAMES = ("url", "website", "website url", "site", "site url",
                "link", "web", "domain", "homepage", "web address",
                "address", "company website")


def _named_url_column(header_cells: List[str]) -> Optional[int]:
    low = [(h or "").strip().lower() for h in header_cells]
    for cand in _HEADER_NAMES:
        if cand in low:
            return low.index(cand)
    return None


def _content_url_column(rows: List[List[str]]) -> Optional[int]:
    """The column that most looks like URLs, judged over the given rows."""
    if not rows:
        return None
    n_cols = max(len(r) for r in rows)
    best, best_hits = None, 0
    for c in range(n_cols):
        hits = sum(1 for r in rows if c < len(r) and _looks_like_url((r[c] or "").strip()))
        if hits > best_hits:
            best, best_hits = c, hits
    if best is not None and best_hits >= max(1, len(rows) // 2):
        return best
    return None


def detect_url_column(csv_path: str) -> Optional[int]:
    """Find the URL column in an arbitrary CSV: header name first, then
    content heuristics over all rows. Returns column index or None."""
    p = Path(csv_path)
    delim = "\t" if p.suffix.lower() == ".tsv" else ","
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f, delimiter=delim)
                if r and any((c or "").strip() for c in r)]
    if not rows:
        return None
    first = [(c or "").strip() for c in rows[0]]
    named = _named_url_column(first)
    if named is not None:
        return named
    # No named header: if the first row itself holds a URL it is DATA,
    # not a header — judge the column over every row.
    if any(_looks_like_url(c) for c in first if c):
        return _content_url_column(rows)
    return _content_url_column(rows[1:])


def load_urls_any(path: str) -> List[str]:
    """Load URLs from .txt/.csv/.tsv; CSV URL column auto-detected.

    Rules, in order:
      1. .txt/.text/.list — one URL per line, ``#`` comments ignored.
      2. CSV/TSV with a recognised header (url/website/site/link/...) —
         that column is used, header row skipped.
      3. CSV/TSV whose first row already contains URL-looking cells —
         treated as header-less data (first row is NOT swallowed).
      4. Anything else — the most URL-looking column wins; failing that,
         the first URL-looking cell per row is taken.
    Result is always de-duplicated, first-seen order preserved.
    """
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in (".txt", ".text", ".list", ""):
        raw = load_urls_txt(str(p))
    else:
        delim = "\t" if suffix == ".tsv" else ","
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.reader(f, delimiter=delim)
                    if r and any((c or "").strip() for c in r)]
        raw = []
        if rows:
            first = [(c or "").strip() for c in rows[0]]
            named = _named_url_column(first)
            if named is not None:
                data_rows, col = rows[1:], named
            elif any(_looks_like_url(c) for c in first if c):
                data_rows, col = rows, _content_url_column(rows)  # no header
            else:
                data_rows, col = rows[1:], _content_url_column(rows[1:])
            for r in data_rows:
                if not r:
                    continue
                if col is not None and col < len(r):
                    v = (r[col] or "").strip()
                    if v and not v.startswith("#") and _looks_like_url(v):
                        raw.append(v)
                else:
                    for cell in r:
                        v = (cell or "").strip()
                        if v and _looks_like_url(v):
                            raw.append(v)
                            break

    # de-dup preserving first-seen order
    seen: Set[str] = set()
    result: List[str] = []
    for u in raw:
        if u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


__all__ = [
    "SafeCSVWriter",
    "SafeJSONWriter",
    "Checkpoint",
    "read_completed_urls",
    "load_urls_txt",
    "load_urls_csv",
    "load_urls_any",
    "detect_url_column",
    "row_to_json_site",
    "finalize_json_file",
    "repair_json_file",
    "count_json_sites",
    "rebuild_json_from_csv",
]
