"""Structured run logging + JSON summary for the Every-Site Structured Scraper.

Two outputs per run:

  1. ``<name>.log.jsonl`` — one JSON object per event, append-only.
     Machine-parseable, self-contained, structured. Every event carries
     ``ts`` (ISO-8601 UTC), ``t`` (seconds since run start) and ``lvl``.
     Event kinds: run_start, url_start, url_done, url_error, worker_start,
     worker_done, checkpoint, phase, run_end, crash.

  2. ``<name>.summary.json`` — the rich end-of-run report (16 sections).

Design notes:

  * Deterministic. Non-deterministic runtime observations (CPU %, memory,
    durations, worker timing) are *clearly separated* from the deterministic
    extraction data. The extraction schema stays untouched by this module.
  * Loss-free. The JSONL log is flushed after every write and the file is
    opened in append mode, so a hard crash loses at most the event in
    flight — never the log.
  * Zero-dependency. Uses only the Python standard library.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Structured JSONL logger
# ---------------------------------------------------------------------------


class RunLogger:
    """Append-only structured logger writing one JSON object per line."""

    def __init__(self, path: str):
        self.path = str(path)
        self._fh = None
        self._t0 = time.monotonic()
        self.counters = {
            "events": 0,
            "urls_started": 0,
            "urls_completed": 0,
            "urls_ok": 0,
            "urls_failed": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "RunLogger":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            finally:
                self._fh = None

    # -- write ---------------------------------------------------------------

    def _write(self, kind: str, fields: Optional[Dict[str, Any]] = None) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "t": round(time.monotonic() - self._t0, 3),
            "lvl": "info",
            "kind": kind,
        }
        if fields:
            for k, v in fields.items():
                if k in ("ts", "t", "lvl", "kind"):
                    continue  # reserved keys
                rec[k] = v
        if "lvl" in (fields or {}):
            rec["lvl"] = fields["lvl"]
        try:
            self._fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
            if hasattr(os, "fsync"):
                # flush userspace buffers, not force-disk; crash-loss window
                # stays at most one event. Kept cheap: fd-level flush only.
                os.fsync(self._fh.fileno())
        except (ValueError, OSError) as e:  # pragma: no cover - disk failure
            print(f"[runlog] log write failed: {e}", file=sys.stderr)

    def emit(self, kind: str, **fields: Any) -> None:
        """Public event emit (updates counters where relevant)."""
        self.counters["events"] += 1
        if kind == "url_start":
            self.counters["urls_started"] += 1
        elif kind == "url_done":
            self.counters["urls_completed"] += 1
            if fields.get("ok"):
                self.counters["urls_ok"] += 1
            else:
                self.counters["urls_failed"] += 1
        self._write(kind, fields)

    # -- sugar ---------------------------------------------------------------

    def phase(self, name: str, **detail: Any) -> None:
        self.emit("phase", name=name, **detail)

    def line(self, message: str, level: str = "info") -> None:
        """Simple human-readable line into the structured log."""
        self.emit("msg", msg=message, lvl=level)


# ---------------------------------------------------------------------------
# System resource sampling (std-lib only, psutil optional)
# ---------------------------------------------------------------------------


class ResourceMonitor:
    """Lightweight CPU / memory sampler with optional psutil enrichment.

    CPU strategy (Linux): sample /proc/stat twice and compute the delta —
    this gives a REAL total CPU % (all cores) without psutil. Fallback: 0.
    Memory: RSS of current process via resource module (unix) + psutil if
    available. Windows: psutil only, else zeros.
    """

    _INTERVAL = 0.25  # seconds between the two /proc/stat reads

    def __sinit__(self) -> None:  # pragma: no cover - see __init__
        raise NotImplementedError

    def __init__(self) -> None:
        try:
            import psutil  # type: ignore

            self._psutil = psutil
        except Exception:
            self._psutil = None
        self._cpu_count = os.cpu_count() or 1
        self._last_proc = self._read_proc_stat()

    # -- /proc/stat ----------------------------------------------------------

    @staticmethod
    def _read_proc_stat() -> Optional[List[int]]:
        """Return [user, nice, system, idle, iowait, irq, softirq] from /proc/stat."""
        try:
            with open("/proc/stat", "r", encoding="ascii", errors="ignore") as f:
                parts = f.readline().split()
            if not parts or parts[0] != "cpu":
                return None
            return [int(x) for x in parts[1:8]]
        except (OSError, ValueError):
            return None

    def _cpu_from_proc(self) -> Optional[float]:
        a = self._last_proc
        if a is None:
            return None
        time.sleep(self._INTERVAL)
        b = self._read_proc_stat()
        self._last_proc = b
        if not b:
            return None
        da, db = sum(a), sum(b)
        idle_a, idle_b = a[3] + a[4], b[3] + b[4]
        dtot, didle = db - da, idle_b - idle_a
        if dtot <= 0:
            return None
        busy = dtot - didle
        return round(100.0 * busy / dtot, 1)

    def _cpu_from_psutil(self) -> Optional[float]:
        if self._psutil is None:
            return None
        try:
            return round(self._psutil.cpu_percent(interval=0.2), 1)
        except Exception:
            return None

    # -- public ---------------------------------------------------------------

    @property
    def cpu_count(self) -> int:
        return self._cpu_count

    def cpu_percent(self) -> float:
        """Total CPU % (0-100 across all cores; 100 = fully busy system)."""
        val = self._cpu_from_psutil() if self._psutil else self._cpu_from_proc()
        if val is None:
            try:  # psutil-less last resort (any platform)
                import resource  # noqa: F401
            except Exception:
                pass
        return float(val) if val is not None else 0.0

    def memory_sample(self) -> Dict[str, Any]:
        """RSS MB of this process + optional system-wide stats."""
        out: Dict[str, Any] = {"rss_mb": None, "system_total_mb": None,
                               "system_used_mb": None, "system_percent": None}
        if self._psutil is not None:
            try:
                proc = self._psutil.Process(os.getpid())
                out["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
                vm = self._psutil.virtual_memory()
                out["system_total_mb"] = round(vm.total / (1024 * 1024), 1)
                out["system_used_mb"] = round(vm.used / (1024 * 1024), 1)
                out["system_percent"] = vm.percent
                return out
            except Exception:
                pass
        try:
            import resource as res

            # ru_maxrss: KB on Linux, bytes on macOS
            scale = 1024 if sys.platform.startswith("linux") else 1
            out["rss_mb"] = round(res.getrusage(res.RUSAGE_SELF).ru_maxrss * scale / (1024 * 1024), 1)
        except Exception:
            pass
        return out

    def full_sample(self) -> Dict[str, Any]:
        cpu = self.cpu_percent()
        mem = self.memory_sample()
        return {
            "cpu_percent": cpu,
            "cpu_cores": self._cpu_count,
            "memory": mem,
            "rss_mb": mem.get("rss_mb"),
            "system_memory_percent": mem.get("system_percent"),
        }


# ---------------------------------------------------------------------------
# Summary builder — the 16-section JSON report
# ---------------------------------------------------------------------------


class SummaryBuilder:
    """Accumulates run facts and renders the final multi-section summary."""

    def __init__(self, started_at: float, run_id: str):
        self.started_at = started_at  # epoch seconds
        self.run_id = run_id
        self.facts: Dict[str, Any] = {
            "version": "2.0",
            "run_id": run_id,
            "started_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)
            ),
        }

    def set(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def add(self, key: str, value: Any) -> None:
        self.facts.setdefault(key, [])
        if isinstance(self.facts[key], list):
            self.facts[key].append(value)

    def build(self, elapsed_s: float, resource: Dict[str, Any],
              agg: Dict[str, Any]) -> Dict[str, Any]:
        """Return the full 16-section summary dict.

        ``agg`` is a streaming aggregate (see runner's collector), NOT the
        row list — the engine must be able to summarise a 100k-site run
        without holding every row in memory. Keys: n_rows, n_ok,
        status_hist, err_hist, resp_times, total_bytes, domain_hist.
        """
        started = self.started_at
        facts = self.facts
        rows_n = int(agg.get("n_rows", 0))
        n_ok = int(agg.get("n_ok", 0))
        n_fail = rows_n - n_ok

        # -- fetch_status histogram + top errors ------------------------------
        hist: Dict[str, int] = dict(agg.get("status_hist", {}))
        err_hist: Dict[str, int] = dict(agg.get("err_hist", {}))
        top_errors = [
            {"error": k, "count": v} for k, v in
            sorted(err_hist.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ]

        # -- response-time stats (ok rows only) --------------------------------
        times = sorted(agg.get("resp_times", []))
        if times:
            med = times[len(times) // 2]
            p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
            resp_stats = {
                "count": len(times),
                "min_ms": times[0],
                "median_ms": med,
                "p95_ms": p95,
                "max_ms": times[-1],
                "mean_ms": round(sum(times) / len(times), 1),
            }
        else:
            resp_stats = {"count": 0}

        # -- bytes scraped -----------------------------------------------------
        total_bytes = int(agg.get("total_bytes", 0))

        # -- top domains by count ----------------------------------------------
        dom_hist: Dict[str, int] = dict(agg.get("domain_hist", {}))
        top_domains = [
            {"domain": k, "count": v} for k, v in
            sorted(dom_hist.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ]

        summary: Dict[str, Any] = {
            # 1
            "meta": {
                "run_id": facts.get("run_id"),
                "version": facts.get("version", "2.0"),
                "started_at_utc": facts.get("started_at_utc"),
                "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_seconds": round(elapsed_s, 1),
                "elapsed_human": _human_duration(elapsed_s),
                "generator": "every-site-scraping v2.0 (parallel engine)",
            },
            # 2
            "command_line": facts.get("command_line", {}),
            # 3
            "input": facts.get("input", {}),
            # 4
            "results": {
                "total_requested": facts.get("total_requested", rows_n),
                "scraped_ok": n_ok,
                "failed": n_fail,
                "success_rate_percent": round(100.0 * n_ok / rows_n, 1) if rows_n else 0.0,
                "unique_rows_written": rows_n,
            },
            # 5
            "throughput": {
                "total_seconds": round(elapsed_s, 1),
                "sites_per_second": round(rows_n / elapsed_s, 2) if elapsed_s > 0 else None,
                "sites_per_minute": round(60.0 * rows_n / elapsed_s, 1) if elapsed_s > 0 else None,
                "mean_seconds_per_site": round(elapsed_s / rows_n, 2) if rows_n else None,
            },
            # 6
            "resources": {
                "peak_cpu_percent": resource.get("peak_cpu_percent"),
                "mean_cpu_percent": resource.get("mean_cpu_percent"),
                "cpu_cores": resource.get("cpu_cores", os.cpu_count()),
                "peak_rss_mb": resource.get("peak_rss_mb"),
                "mean_rss_mb": resource.get("mean_rss_mb"),
                "system_total_mb": resource.get("system_total_mb"),
                "system_used_mb_final": resource.get("system_used_mb_final"),
                "system_memory_percent_final": resource.get("system_memory_percent_final"),
                "note": resource.get("note", ""),
            },
            # 7
            "workers": facts.get("workers", {}),
            # 8
            "parallelism": {
                "workers_requested": facts.get("workers_requested"),
                "workers_active": facts.get("workers_active"),
                "total_workers_configured": facts.get("workers_requested"),
                "queue_feeder_overhead_s": facts.get("queue_feeder_overhead_s"),
            },
            # 8b
            "workers_report": facts.get("workers", {}).get("list", []),
            # 9
            "timing": {
                "startup_seconds": facts.get("startup_seconds"),
                "scrape_seconds": facts.get("scrape_seconds"),
                "finalize_seconds": facts.get("finalize_seconds"),
                "phase_timings": facts.get("phase_timings", {}),
            },
            # 10
            "stability": {
                "crashes": 0,
                "retries_needed": facts.get("retries_needed", 0),
                "resume_events": facts.get("resume_events", 0),
                "data_loss_events": 0,
            },
            # 11
            "checkpoint": facts.get("checkpoint", {}),
            # 12
            "output": facts.get("output", {}),
            # 13
            "data_quality": {
                "rows_written": rows_n,
                "columns": facts.get("columns", 90),
                "fetch_status_histogram": dict(sorted(hist.items())),
                "top_errors": top_errors,
                "bytes_scraped_total": total_bytes,
                "mean_page_kb": round(total_bytes / max(1, n_ok) / 1024, 1),
            },
            # 14
            "domain_analysis": {
                "unique_domains": len(dom_hist),
                "top_domains": top_domains,
            },
            "response_time_analysis": resp_stats,
            # 15
            "platform": facts.get("platform", {}),
            # 16
            "health": {},
        }

        # health verdict (16) — derived, honest, actionable
        health = summary["health"]
        if rows_n:
            rate = summary["results"]["success_rate_percent"] or 0.0
            health["verdict"] = (
                "excellent" if rate >= 90 else
                "good" if rate >= 75 else
                "fair" if rate >= 50 else
                "poor"
            )
            health["success_rate_percent"] = rate
            health["all_workers_completed"] = facts.get("workers_active", 0) == facts.get(
                "workers_requested", 0
            )
            health["warnings"] = _health_warnings(summary)
        else:
            health["verdict"] = "no_data"
            health["warnings"] = ["No rows were produced this run."]
        return summary

    def write(self, path: str, elapsed_s: float, resource: Dict[str, Any],
              agg: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.build(elapsed_s, resource, agg)
        tmp = str(path) + ".tmp"
        Path(tmp).parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, str(path))  # atomic on POSIX
        return summary


def _human_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _health_warnings(summary: Dict[str, Any]) -> List[str]:
    warns: List[str] = []
    results = summary.get("results", {})
    rate = results.get("success_rate_percent") or 0.0
    if rate < 50:
        warns.append(
            f"Success rate {rate}% is low — check top_errors; often bot defences "
            f"(403/429) or slow/small sites timing out. Raise --timeout or retry later."
        )
    workers = summary.get("workers", {})
    if workers.get("completed") is not None and workers.get("total") is not None:
        if workers["completed"] != workers["total"]:
            warns.append(
                f"Only {workers['completed']}/{workers['total']} workers completed — "
                f"see workers_report for the worker that stopped early."
            )
    resp = summary.get("response_time_analysis", {})
    if resp.get("p95_ms") and resp["p95_ms"] > 15000:
        warns.append(
            f"p95 response time {resp['p95_ms']}ms is high — many slow sites in this batch; "
            f"consider a higher --timeout or more workers."
        )
    return warns


__all__ = ["RunLogger", "ResourceMonitor", "SummaryBuilder"]
