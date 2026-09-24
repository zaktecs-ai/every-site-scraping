"""Test suite for the Every-Site Structured Scraper.

Run with:  python -m unittest test_scraper -v

Covers:
  1. Schema integrity (68 columns, no duplicates).
  2. Structured extraction of a known HTML sample (meta, open graph, links,
     headings, contacts, JSON-LD, technology).
  3. Row structural guarantees: every field present, no unknown fields.
  4. Strict CSV writing: header equals schema, every row is column-aligned.
  5. End-to-end single/multi URL runs against a local HTTP server.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraper import SCHEMA, empty_row, extract_site  # noqa: E402
from multiscrape import write_csv  # noqa: E402


SAMPLE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Acme Software — Cloud Tools for Teams</title>
  <meta name="description" content="Acme builds developer and team cloud tools.">
  <meta property="og:site_name" content="Acme Software">
  <meta property="og:title" content="Acme Software">
  <meta property="og:type" content="website">
  <meta property="og:image" content="https://acme.test/logo.png">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:site" content="@acme">
  <link rel="canonical" href="https://acme.test/">
  <link rel="icon" href="/favicon.ico">
  <meta name="generator" content="WordPress 6.5">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Organization","name":"Acme Software",
   "description":"Cloud tools for teams","url":"https://acme.test",
   "logo":"https://acme.test/logo.png","foundingDate":"2012",
   "telephone":"+1-555-0100","address":{"streetAddress":"1 Main St",
   "addressLocality":"Austin","addressRegion":"TX","postalCode":"78701",
   "addressCountry":"US"}}
  </script>
</head>
<body>
  <h1>Welcome to Acme Software</h1>
  <h2>Products</h2>
  <p>We build tools for developers. Contact us at sales@acme.test or +1 (555) 0100.</p>
  <a href="/about">About</a>
  <a href="https://docs.acme.test">Docs</a>
  <a href="https://github.com/acme">GitHub</a>
  <a href="https://facebook.com/acmesoftware">Facebook</a>
  <img src="/a.png" alt="Screenshot">
  <img src="/b.png">
  <form action="/subscribe"></form>
  <footer>© 2024 Acme Software. All rights reserved.</footer>
</body>
</html>
"""


class TestSchema(unittest.TestCase):
    def test_schema_has_69_columns(self):
        self.assertEqual(len(SCHEMA), 69)

    def test_schema_no_duplicates(self):
        self.assertEqual(len(SCHEMA), len(set(SCHEMA)))

    def test_empty_row_has_all_and_only_schema_keys(self):
        row = empty_row()
        self.assertEqual(list(row.keys()), SCHEMA)


class TestExtraction(unittest.TestCase):
    def _row(self):
        return extract_site(SAMPLE_HTML, url="https://acme.test/",
                            http_status=200, final_url="https://acme.test/")

    def test_source_fields(self):
        d = self._row()
        self.assertEqual(d["domain"], "acme.test")
        self.assertEqual(d["https"], "true")
        self.assertEqual(d["http_status"], "200")
        self.assertEqual(d["page_title"], "Acme Software — Cloud Tools for Teams")

    def test_meta_and_og(self):
        d = self._row()
        self.assertEqual(d["og_site_name"], "Acme Software")
        self.assertEqual(d["og_type"], "website")
        self.assertEqual(d["og_image"], "https://acme.test/logo.png")
        self.assertEqual(d["meta_description"], "Acme builds developer and team cloud tools.")
        self.assertEqual(d["language"], "en")
        self.assertEqual(d["charset"], "utf-8")

    def test_twitter(self):
        d = self._row()
        self.assertEqual(d["twitter_card"], "summary_large_image")
        self.assertEqual(d["twitter_handle"], "@acme")

    def test_headings_and_counts(self):
        d = self._row()
        self.assertEqual(d["h1_text"], "Welcome to Acme Software")
        self.assertEqual(d["h1_count"], "1")
        self.assertEqual(d["h2_count"], "1")

    def test_contacts(self):
        d = self._row()
        self.assertIn("sales@acme.test", d["contact_emails"])
        self.assertTrue(d["contact_phones"])

    def test_social(self):
        d = self._row()
        self.assertIn("github.com", d["github_url"])
        self.assertIn("facebook.com", d["facebook_url"])

    def test_structured_data(self):
        d = self._row()
        self.assertEqual(d["org_name"], "Acme Software")
        self.assertEqual(d["org_founding_date"], "2012")
        self.assertEqual(d["org_phone"], "+1-555-0100")
        self.assertIn("Organization", d["jsonld_types"])
        self.assertIn("Austin", d["org_location"])

    def test_technology_detection(self):
        d = self._row()
        self.assertEqual(d["technology"], "WordPress")
        self.assertIn("generator:WordPress", d["technology_evidence"])

    def test_links_and_elements(self):
        d = self._row()
        self.assertEqual(d["images_count"], "2")
        self.assertEqual(d["images_missing_alt"], "1")
        self.assertEqual(d["forms_count"], "1")
        self.assertEqual(d["copyright_text"], "© 2024 Acme Software. All rights reserved.")

    def test_no_unknown_or_missing_fields(self):
        d = self._row()
        self.assertEqual(set(d.keys()), set(SCHEMA))

    def test_all_values_are_strings(self):
        d = self._row()
        for v in d.values():
            self.assertIsInstance(v, str)


class TestCSVIntegrity(unittest.TestCase):
    def test_header_equals_schema_and_rows_align(self):
        rows = [
            extract_site(SAMPLE_HTML, url="https://a.test/"),
            extract_site(SAMPLE_HTML, url="https://b.test/"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            write_csv(rows, out)
            with open(out, encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
            self.assertEqual(reader[0], SCHEMA)          # header == schema
            self.assertEqual(len(reader), 3)             # header + 2 rows
            for row in reader[1:]:
                self.assertEqual(len(row), len(SCHEMA))  # every row column-aligned


# ---------------------------------------------------------------------------
# End-to-end via a local HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = SAMPLE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
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

    def test_run_single_and_multiple(self):
        from multiscrape import run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            url = f"http://127.0.0.1:{self.port}/"
            stats = run([url], output=out)
            self.assertEqual(stats["ok"], 1)
            self.assertEqual(stats["columns"], 69)
            with open(out, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)               # header + 1 row
            self.assertEqual(rows[0], SCHEMA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
