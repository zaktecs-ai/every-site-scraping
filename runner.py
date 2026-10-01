"""Parallel engine for the Every-Site Structured Scraper (v2.0).

Architecture (thread fan-out, single collector):

    input (CSV/TXT) ──► URL list ──► in-queue ──┬─► worker 1 ─┐
                                                ├─► worker 2  │ completions
                                                │   ...       │ flow to ONE
                                                └─► worker N ─┘ collector
                                                                 │
                                              SafeCSVWriter: flush + fsync
                                              after EVERY row + atomic JSON
                                              checkpoint + JSONL event log

Design decisions:

  * **Threads, not processes** — the workload is network-bound (requests
    waits + lxml parsing release the GIL), so threads give real N-way
    parallelism with zero IPC overhead and one shared address space.

  * **All workers actually work** — every worker pulls from the same queue
    until exhaustion; with N workers configured, N workers each scrape
    sites. Per-worker counts in the summary make this verifiable.

  * **Collector pattern** — workers never touch the CSV. One collector
    thread owns the writer: writes stay strictly serial (crash-safety
    depends on it) while scraping stays fully parallel. All per-worker
    stats are updated by the collector alone — no shared-counter races.

  * **No data loss** — every completed row is flushed + fsynced before
    the run moves on. kill -9 mid-run loses at most the sites in flight;
    a rerun with --resume skips everything already in the CSV.

  * **Poison-pill termination** — the feeder puts one _DONE sentinel per
    worker; a fatally broken worker drops extra sentinels so the run
    always ends cleanly (no zombie hangs).

Public API:  run(urls, output=..., workers=..., resume=..., ...)
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from multiscrape import _normalize_url, fetch_page, row_from_fetch
from runlog import ResourceMonitor, RunLogger, SummaryBuilder
from scraper import SCHEMA

import checkpoint as cp

_DONE = object()          # poison pill: one per worker ends the pool
_CP_EVERY = 25            # checkpoint cadence (rows)


class _WorkerStats:
    """Per-worker counters. Written ONLY by the collector thread (race-free)."""

    __slots__ = ("id", "done", "ok", "failed", "bytes", "ms_active",
                 "first_url", "last_url", "errors", "error_detail")

    def __init__(self, wid: int):
        self.id = wid
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.bytes = 0
        self.ms_active = 0
        self.first_url: Optional[str] = None
        self.last_url: Optional[str] = None
        self.errors: List[str] = []
        self.error_detail: Dict[str, int] = {}

    def as_dict(self) -> Dict:
        return {
            "worker": self.id,
            "sites_done": self.done,
            "sites_ok": self.ok,
            "sites_failed": self.failed,
            "ms_active": self.ms_active,
            "mb_scraped": round(self.bytes / 1048576, 2),
            "first_url": self.first_url,
            "last_url": self.last_url,
            "internal_errors": len(self.errors),
            "error_detail": self.error_detail or None,
        }


def run(
    urls: List[str],
    output: str = "output.csv",
    workers: int = 10,
    timeout: int = 20,
    resume: bool = False,
    user_agent: Optional[str] = None,
    log_path: Optional[str] = None,
    summary_path: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
    json_output: Optional[str] = None,
    quiet: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict:
    """Scrape ``urls`` with ``workers`` parallel threads.

    Returns a result dict: total, ok, rows, elapsed_s, workers, summary.
    ``progress(n_done, n_total, status)`` is called from the collector
    after every row lands in the CSV.
    When ``json_output`` is set, a typed, structured JSON file is written
    alongside the CSV (same rows, real arrays/ints/bools).
    """
    t_start = time.time()
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(t_start))

    # ---- input normalisation (dedupe, keep order) --------------------------
    all_urls: List[str] = []
    seen: set = set()
    for raw in urls:
        u = _normalize_url(str(raw or ""))
        if u and u not in seen:
            seen.add(u)
            all_urls.append(u)

    # ---- resume: done-set from the ACTUAL CSV ------------------------------
    done_before: set = set()
    if resume and Path(output).exists():
        done_before = cp.read_completed_urls(output)
    skipped = [u for u in all_urls if u in done_before]
    pending = [u for u in all_urls if u not in done_before]

    logger = RunLogger(log_path or (str(output) + ".log.jsonl")).open()
    monitor = ResourceMonitor()
    sb = SummaryBuilder(t_start, run_id)

    sb.set("command_line", {"workers": workers, "timeout": timeout,
                            "resume": resume, "output": str(output),
                            "user_agent": user_agent or "default"})
    sb.set("input", {
        "urls_requested": len(urls),
        "unique_urls": len(all_urls),
        "already_done_from_resume": len(skipped),
        "pending_after_resume": len(pending),
        "resume_mode": resume,
    })
    sb.set("total_requested", len(all_urls))

    logger.emit("run_start", run_id=run_id, workers=workers, urls=len(all_urls),
                resume=resume, pending=len(pending), output=str(output))

    # ---- fresh vs append (header needed only on a new/empty file) ----------
    out_p = Path(output)
    fresh = (not resume) or (not out_p.exists()) or (out_p.stat().st_size == 0)

    writer = cp.SafeCSVWriter(str(output), write_header=fresh)
    checkpointer = cp.Checkpoint(checkpoint_path or (str(output) + ".checkpoint.json"))
    if resume:
        checkpointer.load()

    # ---- optional structured JSON output (typed, crash-safe, resumed) ------
    json_path: Optional[str] = None
    json_writer = None
    json_healed = 0
    json_rebuilt = False
    if json_output:
        json_path = str(json_output)
        json_meta = {
            "run_id": run_id,
            "workers": workers,
            "timeout": timeout,
            "output_csv": str(output),
            "schema_columns": len(SCHEMA),
            "list_columns_join": "newline (\\n) in CSV; real arrays here",
        }
        if resume and Path(json_path).exists() and Path(json_path).stat().st_size > 0:
            # Heal a crashed JSON, then verify it matches the CSV (the
            # source of truth). Counts equal -> append; mismatch -> rebuild.
            json_healed = cp.repair_json_file(json_path)
            n_json = cp.count_json_sites(json_path)
            if n_json == len(done_before):
                json_writer = cp.SafeJSONWriter(json_path, meta=json_meta,
                                                write_header=False)
            else:
                cp.rebuild_json_from_csv(str(output), json_path, json_meta)
                json_rebuilt = True
                json_writer = cp.SafeJSONWriter(json_path, meta=json_meta,
                                                write_header=False)
        else:
            # fresh JSON (ignore any stale file from a non-resume rerun)
            json_writer = cp.SafeJSONWriter(json_path, meta=json_meta,
                                            write_header=True)

    # ---- shared plumbing -----------------------------------------------------
    in_q: "queue.Queue" = queue.Queue()
    out_q: "queue.Queue" = queue.Queue()
    workers_list = [_WorkerStats(i) for i in range(1, workers + 1)]
    fatal: List[str] = []
    _fatal_lock = threading.Lock()

    n_total = len(pending)
    sb.set("workers_requested", workers)

    def save_checkpoint(n_done: int, force: bool = False) -> None:
        checkpointer.save({
            "run_id": run_id,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows_written_this_run": n_done,
            "rows_total_this_run": n_total,
            "progress_percent": round(100.0 * n_done / max(1, n_total), 1),
            "workers": workers,
            "output": str(output),
        }, force=force)

    # ---- collector thread ------------------------------------------------------
    # Streaming aggregation: per-row stats are folded into small accumulators
    # immediately; the row itself is NOT retained. This keeps memory flat
    # even for 100k-site runs (summary needs counts/histograms, not rows).
    agg: Dict[str, Any] = {
        "n_rows": 0, "n_ok": 0,
        "status_hist": {}, "err_hist": {},
        "resp_times": [], "total_bytes": 0, "domain_hist": {},
    }

    def collect() -> None:
        n_done = 0
        while True:
            item = out_q.get()
            if item is _DONE:
                out_q.task_done()   # keep join() balanced for every pill
                return
            kind, wid, payload = item
            w = workers_list[wid - 1]
            if kind == "row":
                row, ms_active, n_bytes = payload
                writer.write_row(row)            # flush + fsync EVERY row
                if json_writer is not None:
                    json_writer.write_row(row)   # typed, flush + fsync too
                n_done += 1
                # -- fold into the streaming aggregate (no row retained) --
                agg["n_rows"] += 1
                status = row.get("fetch_status", "?")
                agg["status_hist"][status] = agg["status_hist"].get(status, 0) + 1
                if status == "ok":
                    agg["n_ok"] += 1
                    tms = str(row.get("response_time_ms", ""))
                    if tms.isdigit():
                        agg["resp_times"].append(int(tms))
                else:
                    if row.get("fetch_error"):
                        e = str(row["fetch_error"]).split(";")[0][:80]
                        agg["err_hist"][e] = agg["err_hist"].get(e, 0) + 1
                b = str(row.get("page_size_bytes", ""))
                if b.isdigit():
                    agg["total_bytes"] += int(b)
                if row.get("domain"):
                    agg["domain_hist"][row["domain"]] = agg["domain_hist"].get(row["domain"], 0) + 1
                # -- per-worker stats --
                w.done += 1
                w.ms_active += ms_active
                w.bytes += n_bytes
                w.last_url = row.get("url", "")
                if w.first_url is None:
                    w.first_url = row.get("url", "")
                if status == "ok":
                    w.ok += 1
                else:
                    w.failed += 1
                if progress and not quiet:
                    try:
                        progress(n_done, n_total,
                                 f"{row.get('url','')} -> {status}")
                    except Exception:
                        pass
                if n_done % _CP_EVERY == 0:
                    save_checkpoint(n_done)
            elif kind == "err":
                w.errors.append(str(payload))
                key = str(payload).split(":", 1)[0][:60]
                w.error_detail[key] = w.error_detail.get(key, 0) + 1
            out_q.task_done()

    collector = threading.Thread(target=collect, name="collector", daemon=True)
    collector.start()

    # ---- worker threads --------------------------------------------------------
    def worker_loop(w: _WorkerStats) -> None:
        try:
            while True:
                url = in_q.get()          # blocks until work or _DONE pill
                if url is _DONE:
                    return
                t0 = time.monotonic()
                try:
                    fetch = fetch_page(url, timeout=timeout, user_agent=user_agent)
                    row = row_from_fetch(fetch)
                    ms = int((time.monotonic() - t0) * 1000)
                    out_q.put(("row", w.id, (row, ms, len(fetch.get("html") or ""))))
                except Exception as e:   # one URL must never kill a worker
                    out_q.put(("err", w.id, f"{type(e).__name__}: {e}"))
                finally:
                    in_q.task_done()
        except Exception as e:           # fatal: poison the pool, end clean
            with _fatal_lock:
                fatal.append(f"worker {w.id} fatal: {e}")
            for _ in range(workers):
                in_q.put(_DONE)

    threads: List[threading.Thread] = []
    t_ws = time.time()
    for w in workers_list:
        th = threading.Thread(target=worker_loop, args=(w,),
                              name=f"worker-{w.id}", daemon=True)
        th.start()
        threads.append(th)
        logger.emit("worker_start", worker=w.id)
    logger.phase("workers_started", count=len(threads),
                 seconds=round(time.time() - t_ws, 2))
    sb.set("workers_active", len(threads))

    # ---- feeder: fill the queue, then send one pill per worker ----------------
    for u in pending:
        in_q.put(u)
    for _ in range(workers):
        in_q.put(_DONE)
    logger.phase("queue_filled", pending=len(pending))

    # ---- background resource monitor --------------------------------------------
    cpu_samples: List[float] = []
    rss_samples: List[float] = []
    sysp_samples: List[float] = []
    stop_mon = threading.Event()

    def monitor_loop() -> None:
        while not stop_mon.wait(5.0):
            s = monitor.full_sample()
            if s.get("cpu_percent") is not None:
                cpu_samples.append(s["cpu_percent"])
            if s.get("rss_mb") is not None:
                rss_samples.append(s["rss_mb"])
            if s.get("system_memory_percent") is not None:
                sysp_samples.append(s["system_memory_percent"])

    mon_thread = threading.Thread(target=monitor_loop, name="monitor", daemon=True)
    mon_thread.start()

    # ---- wait for the pool to drain ------------------------------------------------
    for th in threads:
        th.join()
    stop_mon.set()
    mon_thread.join(timeout=2)

    # Drain stragglers DETERMINISTICALLY, then stop the collector.
    # out_q.join() waits on task_done() — it cannot return while the
    # collector is still mid-write (a plain empty() check CAN: the item is
    # already get()'d, so the queue looks empty while the write is still
    # in flight — closing the writer then races the collector and drops
    # the last row(s); seen live with a 1.4 MB site).
    out_q.join()
    out_q.put(_DONE)
    collector.join()
    # Belt-and-braces: after the collector thread has exited, no writer
    # call can race it anymore.
    writer.close()
    if json_writer is not None:
        json_writer.close()   # finalizes into one valid JSON document

    # ---- final checkpoint + summary -------------------------------------------------
    save_checkpoint(agg["n_rows"], force=True)
    elapsed = time.time() - t_start
    n_ok = agg["n_ok"]

    resource_summary = {
        "peak_cpu_percent": max(cpu_samples) if cpu_samples else None,
        "mean_cpu_percent": (round(sum(cpu_samples) / len(cpu_samples), 1)
                             if cpu_samples else None),
        "cpu_cores": monitor.cpu_count,
        "peak_rss_mb": max(rss_samples) if rss_samples else None,
        "mean_rss_mb": (round(sum(rss_samples) / len(rss_samples), 1)
                        if rss_samples else None),
        "system_total_mb": None,
        "system_used_mb_final": None,
        "system_memory_percent_final": sysp_samples[-1] if sysp_samples else None,
        "note": ("cpu_percent = total across all cores (100 = every core busy); "
                 "rss_mb = this Python process only"),
    }
    sb.set("workers", {
        "total": workers,
        "started": len(threads),
        "sites_total": sum(w.done for w in workers_list),
        "sites_ok": sum(w.ok for w in workers_list),
        "sites_failed": sum(w.failed for w in workers_list),
        "internal_errors": sum(len(w.errors) for w in workers_list),
        "fatal_errors": fatal or None,
        "list": [w.as_dict() for w in workers_list],
    })
    sb.set("retries_needed", 0)
    sb.set("resume_events", len(skipped))
    sb.set("checkpoint", {
        "path": checkpointer.path,
        "rows_written_this_run": agg["n_rows"],
        "atomic_writes": True,
        "interval_rows": _CP_EVERY,
    })
    sb.set("output", {
        "csv": str(output),
        "json": json_path,
        "json_healed_lines": json_healed,
        "json_rebuilt_from_csv": json_rebuilt,
        "columns": len(SCHEMA),
        "rows_this_run": agg["n_rows"],
        "append_mode": not fresh,
        "utf8_bom": True,
    })
    sb.set("platform", {
        "python": sys.version.split()[0],
        "os": sys.platform,
        "cpu_cores": monitor.cpu_count,
    })
    sb.set("scrape_seconds", round(elapsed, 1))
    sb.set("phase_timings", {})

    summary = sb.write(summary_path or (str(output) + ".summary.json"),
                       elapsed, resource_summary, agg)
    logger.emit("run_end", run_id=run_id, rows=agg["n_rows"],
                ok=n_ok, elapsed_s=round(elapsed, 1),
                summary_path=str(summary_path or (str(output) + ".summary.json")))
    logger.close()

    if not quiet:
        print(f"\nDone: {n_ok}/{agg['n_rows']} ok | {agg['n_rows']}/{n_total} rows | "
              f"{workers} workers | {round(elapsed, 1)}s | "
              f"summary -> {summary_path or (str(output) + '.summary.json')}", flush=True)

    return {
        "total": n_total,
        "ok": n_ok,
        "rows": agg["n_rows"],
        "elapsed_s": round(elapsed, 1),
        "workers": workers,
        "summary": summary,
    }


__all__ = ["run"]
