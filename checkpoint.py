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
    "Checkpoint",
    "read_completed_urls",
    "load_urls_txt",
    "load_urls_csv",
    "load_urls_any",
    "detect_url_column",
]
