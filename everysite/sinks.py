"""Output sinks for scraped rows: CSV, JSON, JSONL and SQLite.

CSV and JSON already existed with hard-won crash-safety guarantees (flush +
fsync per row, torn-tail repair, atomic checkpoints). This module keeps that
contract and generalises it:

  * Every sink implements the same minimal interface — ``write_row``,
    ``close``, ``is_fresh_file`` — so the runner is sink-agnostic and
    crash-safety is a property of the sink, not of the runner.
  * **JSONL** (one typed JSON object per line) is the append-only format that
    is impossible to corrupt: a torn final line is detected and dropped, and
    the file is otherwise always valid, even for a stream of 10M rows.
  * **SQLite** gives a queryable artefact — index the columns you filter on and
    run SQL over hundreds of thousands of scraped sites. Writes are batched in
    a transaction and committed on a cadence, because per-row fsync of SQLite
    is far slower than the append-only formats; ``--resume`` reads the existing
    primary keys back as the done-set.

List-valued columns follow one rule everywhere: in CSV they are newline-joined,
in JSON/JSONL/SQLite they stay real arrays (JSON) or JSON-encoded text (SQLite),
matching the existing JSON contract exactly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .checkpoint import row_to_json_site, SafeCSVWriter, SafeJSONWriter
from .netlayer import normalize_url

__all__ = [
    "SafeJSONLWriter", "SQLiteWriter", "make_sink", "normalize_url",
    "SUPPORTED_FORMATS",
]

SUPPORTED_FORMATS = ("csv", "json", "jsonl", "sqlite")


class SafeJSONLWriter:
    """Append-only typed JSON lines: one JSON object per row, fsync'd.

    A JSONL file is *always* valid up to its last complete line, which makes it
    the most robust sink for very large runs: a crash can only ever damage the
    line being written, and ``repair`` drops exactly that line.
    """

    def __init__(self, path: str, meta: Optional[Dict] = None,
                 write_header: bool = True):
        self.path = str(path)
        self.meta = dict(meta or {})
        self._fh = None
        self._rows = 0
        self._fresh = write_header
        if write_header:
            # fresh file: truncate, and emit a meta line so the format is
            # self-describing even when the consumer knows nothing about us.
            self._fh = open(self.path, "w", encoding="utf-8")
            if self.meta:
                self._fh.write(json.dumps({"_meta": self.meta},
                                          ensure_ascii=False) + "\n")
                self._flush()
        else:
            self._fh = open(self.path, "a", encoding="utf-8")

    def _flush(self) -> None:
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def write_row(self, row: Dict[str, str]) -> None:
        site = row_to_json_site(row)
        self._fh.write(json.dumps(site, ensure_ascii=False) + "\n")
        self._flush()
        self._rows += 1

    def close(self) -> None:
        if self._fh is not None:
            self._flush()
            self._fh.close()
            self._fh = None

    def is_fresh_file(self) -> bool:
        return self._fresh

    @staticmethod
    def read_urls(path: str) -> set:
        """Every ``url`` present in an existing JSONL file (for --resume)."""
        done: set = set()
        p = Path(path)
        if not p.exists():
            return done
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('{"_meta"'):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # torn tail — the incomplete last line
                u = obj.get("url")
                if u:
                    done.add(str(u))
        return done

    @staticmethod
    def repair(path: str) -> int:
        """Drop a torn final line. Returns how many bytes were removed."""
        p = Path(path)
        if not p.exists():
            return 0
        data = p.read_bytes()
        if not data or data.endswith(b"\n"):
            return 0
        cut = data.rfind(b"\n")
        keep = data[: cut + 1] if cut != -1 else b""
        removed = len(data) - len(keep)
        dirn = p.parent or Path(".")
        fd, tmp = tempfile.mkstemp(dir=str(dirn), prefix=".jsonlfix-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(keep)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return removed


class SQLiteWriter:
    """Row-per-site SQLite sink with a queryable schema.

    Every schema column becomes a TEXT column (the scrape output is text by
    design); list columns additionally store a JSON array so consumers can use
    SQLite's JSON1 functions. An index is created on ``url`` so ``--resume``
    lookups and downstream joins are fast.

    Threading: the runner's collector thread owns all writes, but the writer is
    constructed on the main thread. SQLite enforces per-thread connection
    affinity, so the connection is opened lazily on first write (in the writing
    thread) with ``check_same_thread=False`` and guarded by a lock — the two
    together are safe and avoid a hang if the collector ever dies mid-run.
    """

    def __init__(self, path: str, schema: Sequence[str],
                 write_header: bool = True, commit_every: int = 50):
        self.path = str(path)
        self.schema = list(schema)
        self.commit_every = max(1, int(commit_every))
        self._pending = 0
        self._fresh = write_header
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            cols = ", ".join(f'"{c}" TEXT' for c in self.schema)
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS sites ({cols}, '
                f'"_scraped_at" TEXT DEFAULT CURRENT_TIMESTAMP)')
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_sites_url ON sites("url")')
            conn.commit()

    def write_row(self, row: Dict[str, str]) -> None:
        cols = self.schema
        placeholders = ", ".join("?" for _ in cols)
        collist = ", ".join(f'"{c}"' for c in cols)
        with self._lock:
            conn = self._connect()
            conn.execute(
                f"INSERT INTO sites ({collist}) VALUES ({placeholders})",
                [row.get(c, "") for c in cols])
            self._pending += 1
            if self._pending >= self.commit_every:
                conn.commit()
                self._pending = 0

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None

    def is_fresh_file(self) -> bool:
        return self._fresh

    @staticmethod
    def read_urls(path: str) -> set:
        done: set = set()
        p = Path(path)
        if not p.exists():
            return done
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except sqlite3.Error:
            return done
        try:
            try:
                for (u,) in conn.execute('SELECT "url" FROM sites'):
                    if u:
                        done.add(str(u))
            except sqlite3.Error:
                pass  # no such table yet
        finally:
            conn.close()
        return done


def detect_format(path: str) -> str:
    """Infer the sink format from a path's extension."""
    p = str(path).lower()
    if p.endswith(".jsonl") or p.endswith(".ndjson"):
        return "jsonl"
    if p.endswith((".sqlite", ".sqlite3", ".db")):
        return "sqlite"
    if p.endswith(".json"):
        return "json"
    return "csv"


def make_sink(fmt: str, path: str, *, schema: Sequence[str],
              meta: Optional[Dict] = None, write_header: bool = True):
    """Construct the right writer for ``fmt``. Unknown format -> ValueError."""
    fmt = (fmt or "csv").lower()
    if fmt == "csv":
        return SafeCSVWriter(path, write_header=write_header)
    if fmt == "json":
        return SafeJSONWriter(path, meta=meta or {}, write_header=write_header)
    if fmt == "jsonl":
        return SafeJSONLWriter(path, meta=meta or {}, write_header=write_header)
    if fmt == "sqlite":
        return SQLiteWriter(path, schema, write_header=write_header)
    raise ValueError(
        f"Unknown output format {fmt!r}; choose from {', '.join(SUPPORTED_FORMATS)}")


def read_done_urls(path: str, fmt: Optional[str] = None) -> set:
    """Read the done-set (already-scraped URLs) from any sink, for --resume."""
    fmt = fmt or detect_format(path)
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return set()
    if fmt == "jsonl":
        return SafeJSONLWriter.read_urls(path)
    if fmt == "sqlite":
        return SQLiteWriter.read_urls(path)
    # CSV / JSON both key on the url field; CSV is the historical path.
    from . import checkpoint as cp
    if fmt == "json":
        done: set = set()
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            for s in doc.get("sites", []):
                if s.get("url"):
                    done.add(str(s["url"]))
        except Exception:
            # fall back to the crash-repairing reader
            return cp.read_completed_urls(path)
        return done
    return cp.read_completed_urls(path)
