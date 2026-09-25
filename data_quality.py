#!/usr/bin/env python3
"""CSV integrity auditor for the Every-Site Structured Scraper.

Verifies that a produced CSV:
  * has a header that EXACTLY matches the canonical schema (order + length),
  * has no duplicate or empty header cells,
  * has every data row column-aligned to the schema,
  * contains no None values,
  * keeps list-like fields deduplicated and sorted (deterministic output).

Exit code 0 = clean, 1 = problems found.

Usage:
    python data_quality.py output.csv
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraper import SCHEMA, LIST_COLUMNS, LIST_DELIM, COUNT_LIST_PAIRS  # noqa: E402

# Columns that hold sorted, deduped newline-joined lists (single source of
# truth lives in scraper.py). The auditor checks they are sorted + deduped.
_LIST_COLUMNS = set(LIST_COLUMNS)


def audit(path: str) -> int:
    problems = []

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            print("FAIL: file is empty / has no header row.")
            return 1

        # 1. Header integrity
        if header != SCHEMA:
            if len(header) != len(SCHEMA):
                problems.append(
                    f"header has {len(header)} columns; schema has {len(SCHEMA)}"
                )
            else:
                diffs = [i for i, (a, b) in enumerate(zip(header, SCHEMA)) if a != b]
                problems.append(f"header mismatches schema at indices {diffs[:10]}")
        if len(set(header)) != len(header):
            problems.append("header contains duplicate column names")

        idx = {name: i for i, name in enumerate(header)} if len(header) == len(SCHEMA) else {}

        # 2. Row integrity
        for lineno, row in enumerate(reader, start=2):
            if len(row) != len(SCHEMA):
                problems.append(
                    f"row {lineno}: {len(row)} columns (expected {len(SCHEMA)})"
                )
                continue
            for c, val in enumerate(row):
                if val is None:
                    problems.append(f"row {lineno}, column {c}: None value")
            # list determinism checks (newline-delimited, sorted + deduped)
            for col in _LIST_COLUMNS:
                if col not in idx:
                    continue
                cell = row[idx[col]]
                if cell:
                    parts = [p for p in cell.split(LIST_DELIM) if p]
                    if parts != sorted(parts):
                        problems.append(
                            f"row {lineno}: {col} not sorted"
                        )
                    if len(parts) != len(set(parts)):
                        problems.append(
                            f"row {lineno}: {col} has duplicates"
                        )
            # count == list-length consistency (self-verifying accuracy)
            for count_col, list_col in COUNT_LIST_PAIRS.items():
                if count_col not in idx or list_col not in idx:
                    continue
                try:
                    declared = int(row[idx[count_col]] or "0")
                except ValueError:
                    problems.append(f"row {lineno}: {count_col} not an integer")
                    continue
                cell = row[idx[list_col]]
                actual = len([p for p in cell.split(LIST_DELIM) if p]) if cell else 0
                if declared != actual:
                    problems.append(
                        f"row {lineno}: {count_col}={declared} but "
                        f"{list_col} has {actual} items"
                    )

    if problems:
        print(f"FAIL: {len(problems)} problem(s) in {path}")
        for p in problems[:50]:
            print(f"  - {p}")
        return 1

    print(f"OK: {path} is clean ({len(SCHEMA)} columns, schema-aligned).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    target = sys.argv[1]
    if not Path(target).exists():
        print(f"FAIL: file not found: {target}")
        raise SystemExit(2)
    raise SystemExit(audit(target))
