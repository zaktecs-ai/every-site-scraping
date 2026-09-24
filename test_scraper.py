"""Test suite for stdout scrawler.

Run with:  python -m unittest tests.test_scraper -v
   or:     python tests/test_scraper.py

Covers:
  1. Text extraction from a known HTML sample.
  2. Block ordering and block types.
  3. Hidden content exclusion (script, style, style="display:none").
  4. Duplicate removal.
  5. CSV writing round-trip.
  6. End-to-end single/multi URL runs against a local HTTP server.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import extract_text, write_csv  # noqa: E402


SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Acme Software</title>
  <style>body { color: red; }</style>
</head>
<body>
  <h1>Welcome to Acme Software</h1>
  <p>We build tools for developers.</p>
  <p>Learn more at our <a href="/about">about page</a>.</p>
  <ul>
    <li>Fast</li>
    <li>Secure</li>
    <li hidden>This should be hidden</li>
    <li style="display:none">This too</li>
  </ul>
  <script>console.log('nope');</script>
  <blockquote>"The best tool we ever used."</blockquote>
  <p>We build tools for developers.</p>
</body>
</html>
"""


class TestExtraction(unittest.TestCase):
    def test_extracts_heading_and_paragraphs(self):
        blocks = extract_text(SAMPLE_HTML, url="https://acme.test")
        texts = [b["text"] for b in blocks]
        self.assertIn("Welcome to Acme Software", texts)
        self.assertIn("We build tools for developers.", texts)
        self.assertIn("Learn more at our about page.", texts)

    def test_hidden_content_is_excluded(self):
        blocks = extract_text(SAMPLE_HTML)
        joined = " | ".join(b["text"] for b in blocks)
        self.assertNotIn("This should be hidden", joined)
        self.assertNotIn("This too", joined)
        self.assertNotIn("console.log", joined)
        self.assertNotIn("color: red", joined)

    def test_duplicates_are_removed(self):
        blocks = extract_text(SAMPLE_HTML)
        counts = sum(1 for b in blocks if b["text"] == "We build tools for developers.")
        self.assertEqual(counts, 1)

    def test_block_types(self):
        blocks = extract_text(SAMPLE_HTML)
        types = {b["text"]: b["block_type"] for b in blocks}
        self.assertEqual(types.get("Welcome to Acme Software"), "heading")
        self.assertEqual(types.get("Fast"), "list_item")

    def test_quoted_text_present(self):
        blocks = extract_text(SAMPLE_HTML)
        self.assertTrue(
            any('The best tool we ever used' in b["text"] for b in blocks)
        )


class TestCSV(unittest.TestCase):
    def test_write_csv_roundtrip(self):
        blocks = extract_text(SAMPLE_HTML, url="https://acme.test")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            write_csv(blocks, out)
            self.assertTrue(os.path.exists(out))
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("block_type", text)
            self.assertIn("element_path", text)


# ---------------------------------------------------------------------------
# End-to-end: spin up a local server so we don't need the internet.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = SAMPLE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


class TestEndToEnd(unittest.TestCase):
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

    def test_multiscrape_single_url(self):
        from multiscrape import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            url = f"http://127.0.0.1:{self.port}/"
            results = run([url], output=out)
            self.assertTrue(results[0]["ok"])
            self.assertGreater(results[0]["block_count"], 0)
            self.assertTrue(os.path.exists(out))

    def test_multiscrape_multiple_urls(self):
        from multiscrape import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            url = f"http://127.0.0.1:{self.port}/"
            results = run([url, url, url], output=out)
            self.assertEqual(len(results), 3)
            self.assertTrue(all(r["ok"] for r in results))
            self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
