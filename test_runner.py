"""Tests for the v2.0 parallel engine: runner, checkpoint, runlog.

Run with:  python -m unittest test_scraper -v   (runs the whole suite)

Covers:
  1. SafeCSVWriter — flush-per-row append, header only on fresh files.
  2. Checkpoint — atomic save/load, rate limiting, torn-file tolerance.
  3. Resume — done-set from CSV, torn-tail repair, no data loss.
  4. Input loading — TXT, single-column CSV, multi-column CSV with
     URL-column auto-detection (header name AND content heuristics).
  5. runlog — JSONL events parse, counters, summary structure (16 sections).
  6. runner.run — parallel end-to-end against a local HTTP server:
     all workers actually work, rows == urls, resume skips done URLs,
     every URL handled exactly once, crash-safety invariants.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import checkpoint as cp
from runlog import RunLogger, SummaryBuilder
from scraper import SCHEMA


def _row(url: str, status: str = "ok") -> dict:
    d = {c: "" for c in SCHEMA}
    d["url"] = url
    d["final_url"] = url
    d["fetch_status"] = status
    return d


class TestSafeCSVWriter(unittest.TestCase):
    def test_fresh_file_gets_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.csv")
            w = cp.SafeCSVWriter(p, write_header=True)
            w.write_row(_row("https://a.test"))
            w.close()
            text = Path(p).read_text(encoding="utf-8-sig")
            self.assertTrue(text.startswith(",".join(SCHEMA)))
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], SCHEMA)
            self.assertEqual(len(rows), 2)

    def test_append_mode_no_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.csv")
            w = cp.SafeCSVWriter(p, write_header=True)
            w.write_row(_row("https://a.test"))
            w.close()
            w2 = cp.SafeCSVWriter(p, write_header=False)
            w2.write_row(_row("https://b.test"))
            w2.close()
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)          # header + 2 rows, ONE header
            self.assertEqual(rows[1][SCHEMA.index("url")], "https://a.test")
            self.assertEqual(rows[2][SCHEMA.index("url")], "https://b.test")

    def test_extra_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = cp.SafeCSVWriter(os.path.join(tmp, "out.csv"), write_header=True)
            bad = _row("https://x.test")
            bad["not_in_schema"] = "boom"
            with self.assertRaises(ValueError):
                w.write_row(bad)
            w.close()


class TestCheckpoint(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cp.Checkpoint(os.path.join(tmp, "cp.json"))
            ok = c.save({"rows": 5, "total": 100}, force=True)
            self.assertTrue(ok)
            c2 = cp.Checkpoint(os.path.join(tmp, "cp.json"))
            self.assertEqual(c2.load().get("rows"), 5)

    def test_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cp.Checkpoint(os.path.join(tmp, "cp.json"))
            self.assertTrue(c.save({"n": 1}, force=True))
            self.assertFalse(c.save({"n": 2}))      # too soon -> skipped
            self.assertTrue(c.save({"n": 3}, force=True))

    def test_load_tolerates_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cp.json")
            Path(p).write_text("{not json", encoding="utf-8")
            c = cp.Checkpoint(p)
            self.assertEqual(c.load(), {})

    def test_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cp.json")
            c = cp.Checkpoint(p)
            c.save({"n": 1}, force=True)
            self.assertFalse(os.path.exists(p + ".tmp"))


class TestResume(unittest.TestCase):
    def test_done_urls_from_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.csv")
            w = cp.SafeCSVWriter(p, write_header=True)
            w.write_row(_row("https://a.test"))
            w.write_row(_row("https://b.test"))
            w.close()
            done = cp.read_completed_urls(p)
            self.assertEqual(done, {"https://a.test", "https://b.test"})

    def test_torn_tail_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.csv")
            w = cp.SafeCSVWriter(p, write_header=True)
            w.write_row(_row("https://a.test"))
            w.write_row(_row("https://b.test"))
            w.close()
            # simulate a crash mid-append: chop the last line in half
            data = Path(p).read_bytes()
            torn = data[: len(data) - 40]
            Path(p).write_bytes(torn)
            done = cp.read_completed_urls(p)
            self.assertIn("https://a.test", done)
            self.assertNotIn("https://b.test", done)  # torn row dropped
            # and the file is now clean / parseable
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = [r for r in csv.reader(f) if r]
            self.assertEqual(len(rows[-1]), len(SCHEMA))

    def test_torn_inside_quoted_multiline_cell_is_repaired(self):
        # Real-world crash shape: the cut lands INSIDE a quoted multi-line
        # list cell. A naive field-count check accepts the torn row (each
        # embedded \n keeps the fragment "parsable"); the \\r\\n rule
        # must still drop it. Found in live Round-5 testing.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.csv")
            w = cp.SafeCSVWriter(p, write_header=True)
            a = _row("https://a.test")
            b = _row("https://b.test")
            b["internal_links_list"] = "https://x.test/1\nhttps://x.test/2"
            w.write_row(a)
            w.write_row(b)
            w.close()
            data = Path(p).read_bytes()
            # cut in the middle of the quoted cell (after first embedded \n)
            marker = b"https://x.test/1\n".index(b"\n")
            cut = data.index(b"https://x.test/1\n") + marker - 5
            Path(p).write_bytes(data[:cut])
            done = cp.read_completed_urls(p)
            self.assertIn("https://a.test", done)
            self.assertNotIn("https://b.test", done)
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = [r for r in csv.reader(f) if r]
            self.assertEqual(len(rows[-1]), len(SCHEMA))

    def test_missing_file_is_empty_set(self):
        self.assertEqual(cp.read_completed_urls("/nonexistent/x.csv"), set())


class TestInputLoading(unittest.TestCase):
    def test_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "urls.txt")
            Path(p).write_text(
                "https://a.test\n# comment\nwww.b.test\n\nplainname.org\n",
                encoding="utf-8")
            urls = cp.load_urls_any(p)
            self.assertEqual(urls, ["https://a.test", "www.b.test", "plainname.org"])

    def test_single_column_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "urls.csv")
            Path(p).write_text("url\nhttps://a.test\nhttps://b.test\n",
                              encoding="utf-8")
            urls = cp.load_urls_any(p)
            self.assertEqual(urls, ["https://a.test", "https://b.test"])

    def test_multicolumn_csv_header_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "leads.csv")
            Path(p).write_text(
                "company,contact,website\n"
                "Acme,John,https://acme.test\n"
                "Beta,Jane,https://beta.test\n",
                encoding="utf-8")
            urls = cp.load_urls_any(p)
            self.assertEqual(urls, ["https://acme.test", "https://beta.test"])

    def test_multicolumn_csv_content_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "weird.csv")
            # no recognizable header, but column 2 looks like URLs
            Path(p).write_text(
                "1,https://a.test,x\n2,https://b.test,y\n3,https://c.test,z\n",
                encoding="utf-8")
            urls = cp.load_urls_any(p)
            self.assertEqual(len(urls), 3)

    def test_dedupe_preserves_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "urls.txt")
            Path(p).write_text("https://a.test\nhttps://a.test\nhttps://b.test\n",
                              encoding="utf-8")
            self.assertEqual(cp.load_urls_any(p),
                             ["https://a.test", "https://b.test"])


class TestRunLog(unittest.TestCase):
    def test_jsonl_events_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "run.log.jsonl")
            lg = RunLogger(p).open()
            lg.emit("run_start", workers=4, urls=10)
            lg.emit("url_done", url="https://a.test", ok=True, ms=120)
            lg.emit("url_error", url="https://b.test", err="timeout")
            lg.close()
            lines = [json.loads(l) for l in
                     Path(p).read_text(encoding="utf-8").splitlines() if l]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["kind"], "run_start")
            self.assertEqual(lines[1]["ok"], True)
            self.assertTrue(all("ts" in r and "t" in r for r in lines))
            self.assertEqual(lg.counters["events"], 3)

    def test_summary_has_16_sections_and_health(self):
        sb = SummaryBuilder(0.0, "test-run")
        agg = {
            "n_rows": 2, "n_ok": 1,
            "status_hist": {"ok": 1, "timeout": 1},
            "err_hist": {"Request timed out": 1},
            "resp_times": [100, 200],
            "total_bytes": 4000, "domain_hist": {"a.test": 2},
        }
        s = sb.build(elapsed_s=12.0, resource={
            "peak_cpu_percent": 55.0, "mean_cpu_percent": 40.0,
            "cpu_cores": 8, "peak_rss_mb": 300.0, "mean_rss_mb": 250.0,
        }, agg=agg)
        for section in ("meta", "command_line", "input", "results",
                        "throughput", "resources", "workers", "parallelism",
                        "workers_report", "timing", "stability",
                        "checkpoint", "output", "data_quality",
                        "domain_analysis", "response_time_analysis",
                        "platform", "health"):
            self.assertIn(section, s, f"missing section {section}")
        self.assertEqual(s["results"]["scraped_ok"], 1)
        self.assertEqual(s["results"]["failed"], 1)
        self.assertEqual(s["data_quality"]["fetch_status_histogram"],
                         {"ok": 1, "timeout": 1})
        self.assertEqual(s["data_quality"]["top_errors"],
                         [{"error": "Request timed out", "count": 1}])
        self.assertEqual(s["response_time_analysis"]["max_ms"], 200)
        self.assertEqual(s["health"]["verdict"], "fair")  # 50% -> fair boundary
        self.assertIsInstance(s["health"]["warnings"], list)

    def test_summary_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.json")
            sb = SummaryBuilder(0.0, "r1")
            sb.write(p, 5.0, {}, {"n_rows": 1, "n_ok": 1, "status_hist": {"ok": 1},
                                 "err_hist": {}, "resp_times": [], "total_bytes": 0,
                                 "domain_hist": {}})
            loaded = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(loaded["results"]["scraped_ok"], 1)
            self.assertFalse(os.path.exists(p + ".tmp"))


# ---------------------------------------------------------------------------
# Structured JSON output
# ---------------------------------------------------------------------------

class TestJSONOutput(unittest.TestCase):
    def _row(self, url: str, status: str = "ok") -> dict:
        d = {c: "" for c in SCHEMA}
        d["url"] = url
        d["fetch_status"] = status
        d["http_status"] = "200"
        d["internal_links_list"] = "https://a.test/1\nhttps://a.test/2"
        d["internal_links"] = "2"
        d["https"] = "true"
        d["word_count"] = "42"
        d["response_time_ms"] = "123"
        return d

    def test_json_file_is_valid_document_with_typed_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.json")
            w = cp.SafeJSONWriter(p, meta={"run_id": "t1"})
            w.write_row(self._row("https://a.test"))
            w.write_row(self._row("https://b.test"))
            w.close()
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(doc["meta"]["run_id"], "t1")
            self.assertEqual(len(doc["sites"]), 2)
            s = doc["sites"][0]
            # typed values, not strings
            self.assertEqual(s["internal_links_list"],
                             ["https://a.test/1", "https://a.test/2"])
            self.assertIsInstance(s["internal_links"], int)
            self.assertIsInstance(s["word_count"], int)
            self.assertIsInstance(s["response_time_ms"], int)
            self.assertIs(s["https"], True)
            # count == len(list) self-verification carries into JSON
            self.assertEqual(s["internal_links"], len(s["internal_links_list"]))

    def test_json_repair_drops_torn_last_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.json")
            w = cp.SafeJSONWriter(p, meta={"run_id": "t2"})
            w.write_row(self._row("https://a.test"))
            w.write_row(self._row("https://b.test"))
            w.close()
            # simulate a crash: append a half line, then repair
            with open(p, "a", encoding="utf-8") as f:
                f.write('{"url": "https://c.test", "wor')  # torn
            n = cp.repair_json_file(p)
            self.assertEqual(n, 2)                     # only intact sites
            # repair leaves the appendable form; finalize -> valid document
            cp.finalize_json_file(p)
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(len(doc["sites"]), 2)
            self.assertEqual({s["url"] for s in doc["sites"]},
                             {"https://a.test", "https://b.test"})

    def test_json_survives_unicode_line_separator_in_text(self):
        # REAL BUG (found live on contour-software.com): str.splitlines()
        # splits on \u2028 (Unicode line separator) which json.dumps keeps
        # INSIDE a string (ensure_ascii=False) — so a site whose text
        # contains \u2028 had its JSON line broken in two and the site
        # silently dropped. Line handling must split on real "\n" only.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.json")
            d = {c: "" for c in SCHEMA}
            d["url"] = "https://u.test"
            d["fetch_status"] = "ok"
            d["visible_text_preview"] = "weird\u2028separator\u2029inside"
            d["headings_outline"] = "h1: line\u2028sep"
            w = cp.SafeJSONWriter(p, meta={"run_id": "t4"})
            w.write_row(d)
            w.write_row({**d, "url": "https://v.test"})
            w.close()
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(len(doc["sites"]), 2)
            self.assertEqual(doc["sites"][0]["visible_text_preview"],
                             "weird\u2028separator\u2029inside")
            # repair + finalize stay lossless too
            n = cp.repair_json_file(p)
            self.assertEqual(n, 2)
            cp.finalize_json_file(p)
            doc2 = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(len(doc2["sites"]), 2)

    def test_finalize_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.json")
            w = cp.SafeJSONWriter(p, meta={"run_id": "t3"})
            w.write_row(self._row("https://a.test"))
            w.close()
            n1 = cp.finalize_json_file(p)
            n2 = cp.finalize_json_file(p)
            self.assertEqual(n1, n2)
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(len(doc["sites"]), 1)


class TestRunnerJSONEndToEnd(unittest.TestCase):
    """runner.run(json_output=...) writes both CSV and a valid JSON doc."""

    def test_json_written_and_matches_csv(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            jp = os.path.join(tmp, "out.json")
            urls = [f"https://x{i}.test" for i in range(6)]
            result = run(urls, output=out, workers=3, quiet=True,
                         json_output=jp,
                         log_path=os.path.join(tmp, "l.jsonl"),
                         summary_path=os.path.join(tmp, "s.json"),
                         checkpoint_path=os.path.join(tmp, "c.json"))
            self.assertEqual(result["rows"], 6)
            doc = json.loads(Path(jp).read_text(encoding="utf-8"))
            self.assertEqual(len(doc["sites"]), 6)
            json_urls = {s["url"] for s in doc["sites"]}
            self.assertEqual(json_urls, set(urls))
            # CSV row count == JSON site count
            with open(out, encoding="utf-8-sig", newline="") as f:
                csv_rows = [r for r in csv.reader(f) if r]
            self.assertEqual(len(csv_rows) - 1, len(doc["sites"]))
            # summary knows about the JSON output
            self.assertEqual(result["summary"]["output"]["json"], jp)


# ---------------------------------------------------------------------------
# End-to-end parallel run against a local HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path
        if path.startswith("/slow"):
            import time
            time.sleep(0.3)
        body = f"""<!DOCTYPE html><html lang="en"><head><title>Site {path}</title>
        <meta name="description" content="test page {path}">
        </head><body><h1>Heading {path}</h1><p>Visible words here.</p>
        <a href="/about">About</a></body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestRunnerEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _urls(self, n: int, slow: bool = False):
        base = f"http://127.0.0.1:{self.port}"
        kind = "slow" if slow else "page"
        return [f"{base}/{kind}/{i}" for i in range(n)]

    def test_parallel_run_all_workers_active(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            n, w = 40, 8
            result = run(self._urls(n, slow=True), output=out, workers=w,
                          log_path=os.path.join(tmp, "l.jsonl"),
                          summary_path=os.path.join(tmp, "s.json"),
                          checkpoint_path=os.path.join(tmp, "c.json"),
                          quiet=True)
            self.assertEqual(result["rows"], n)
            self.assertEqual(result["ok"], n)
            # every one of the 8 workers actually did work
            wr = result["summary"]["workers"]
            self.assertEqual(wr["total"], w)
            active = [x for x in wr["list"] if x["sites_done"] > 0]
            self.assertEqual(len(active), w, "not all workers participated")
            # counts add up exactly — no double counting, no loss
            self.assertEqual(sum(x["sites_done"] for x in wr["list"]), n)
            self.assertEqual(wr["sites_ok"], n)
            self.assertEqual(wr["sites_failed"], 0)
            # CSV integrity
            with open(out, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], SCHEMA)
            self.assertEqual(len(rows), n + 1)
            urls = [r[SCHEMA.index("url")] for r in rows[1:]]
            self.assertEqual(len(set(urls)), n)   # exactly once each
            # log + checkpoint + summary exist
            for extra in ("l.jsonl", "s.json", "c.json"):
                self.assertTrue(os.path.exists(os.path.join(tmp, extra)))

    def test_single_worker_still_correct(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            result = run(self._urls(6), output=out, workers=1, quiet=True,
                         log_path=os.path.join(tmp, "l.jsonl"),
                         summary_path=os.path.join(tmp, "s.json"),
                         checkpoint_path=os.path.join(tmp, "c.json"))
            self.assertEqual(result["rows"], 6)
            self.assertEqual(
                result["summary"]["workers"]["list"][0]["sites_done"], 6)

    def test_resume_skips_done_urls(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            urls = self._urls(10)
            r1 = run(urls[:5], output=out, workers=3, quiet=True,
                     log_path=os.path.join(tmp, "l.jsonl"),
                     summary_path=os.path.join(tmp, "s.json"),
                     checkpoint_path=os.path.join(tmp, "c.json"))
            self.assertEqual(r1["rows"], 5)
            r2 = run(urls, output=out, workers=3, resume=True, quiet=True,
                     log_path=os.path.join(tmp, "l.jsonl"),
                     summary_path=os.path.join(tmp, "s.json"),
                     checkpoint_path=os.path.join(tmp, "c.json"))
            self.assertEqual(r2["rows"], 5)             # only the remaining 5
            with open(out, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 11)            # header + 10 unique
            got = {r[SCHEMA.index("url")] for r in rows[1:]}
            self.assertEqual(got, set(urls))

    def test_parallel_faster_than_serial(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            # 12 slow pages (300ms each) — serial >= 3.6s, parallel(6) ~0.6s
            out = os.path.join(tmp, "out.csv")
            result = run(self._urls(12, slow=True), output=out, workers=6,
                         quiet=True, log_path=os.path.join(tmp, "l.jsonl"),
                         summary_path=os.path.join(tmp, "s.json"),
                         checkpoint_path=os.path.join(tmp, "c.json"))
            self.assertEqual(result["rows"], 12)
            self.assertLess(result["elapsed_s"], 3.0,
                            "parallel engine should beat serial time here")

    def test_worker_stats_no_double_count(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            result = run(self._urls(20), output=out, workers=4, quiet=True,
                         log_path=os.path.join(tmp, "l.jsonl"),
                         summary_path=os.path.join(tmp, "s.json"),
                         checkpoint_path=os.path.join(tmp, "c.json"))
            s = result["summary"]
            self.assertEqual(s["workers"]["sites_total"], 20)
            self.assertEqual(
                s["workers"]["sites_ok"] + s["workers"]["sites_failed"], 20)
            self.assertEqual(s["results"]["unique_rows_written"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
