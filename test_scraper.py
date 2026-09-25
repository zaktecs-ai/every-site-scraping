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
    def test_schema_has_90_columns(self):
        self.assertEqual(len(SCHEMA), 90)

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


class TestRegressionFixes(unittest.TestCase):
    """Lock in the visibility/tree-walk bugs found during the 58-site stress test."""

    def test_overflow_hidden_is_not_treated_as_hidden(self):
        # "overflow-hidden" is a CSS overflow utility, NOT a visibility class.
        # It must not hide content (this was dropping Apple/IBM headings).
        from scraper import _is_hidden
        from bs4 import BeautifulSoup
        tag = BeautifulSoup('<h1 class="overflow-hidden">Visible</h1>', "lxml").h1
        self.assertFalse(_is_hidden(tag))

    def test_tailwind_arbitrary_selector_is_not_hidden(self):
        # "[&_br]:hidden" hides a descendant <br>, NOT the element itself.
        from scraper import _is_hidden
        from bs4 import BeautifulSoup
        tag = BeautifulSoup(
            '<h1 class="mb-2 [&_br]:hidden text-6">Visible</h1>', "lxml"
        ).h1
        self.assertFalse(_is_hidden(tag))

    def test_real_visibility_classes_still_hidden(self):
        from scraper import _is_hidden
        from bs4 import BeautifulSoup
        for cls in ("hidden", "invisible"):
            tag = BeautifulSoup(f'<div class="{cls}">x</div>', "lxml").div
            self.assertTrue(_is_hidden(tag), cls)

    def test_accessibility_classes_are_kept(self):
        # sr-only / visually-hidden hide content VISUALLY but keep it for
        # crawlers; a completeness-focused extractor must keep them (this was
        # data loss on sitepoint.com's sr-only <h1>).
        from scraper import _is_hidden
        from bs4 import BeautifulSoup
        for cls in ("sr-only", "visually-hidden", "screen-reader-text"):
            tag = BeautifulSoup(f'<h1 class="{cls}">Real</h1>', "lxml").h1
            self.assertFalse(_is_hidden(tag), cls)

    def test_sr_only_heading_is_extracted(self):
        html = '<body><h1 class="sr-only">SitePoint Heading</h1><p>x</p></body>'
        d = extract_site(html, url="https://x.test")
        self.assertEqual(d["h1_count"], "1")
        self.assertEqual(d["h1_text"], "SitePoint Heading")

    def test_heading_inside_list_item_is_extracted(self):
        # python.org's <h1> lives inside <li class="slide">; the <li> must
        # descend so the heading is captured separately, not swallowed.
        html = ("<body><ul><li><h1>Slide Heading</h1>"
                "<p>body text</p></li></ul></body>")
        d = extract_site(html, url="https://x.test")
        self.assertEqual(d["h1_count"], "1")
        self.assertEqual(d["h1_text"], "Slide Heading")

    def test_heading_with_nested_block_is_not_dropped(self):
        # <h1>Code <div>…</div> Work</h1> must yield "Code Work", not vanish.
        html = "<body><h1>Code <div>x</div> Work</h1></body>"
        d = extract_site(html, url="https://x.test")
        self.assertEqual(d["h1_count"], "1")
        self.assertIn("Code", d["h1_text"])

    def test_custom_element_subtree_is_walked(self):
        # Unknown/custom elements (Web Components) must be descended into,
        # not dropped — this caused full-page data loss on IBM.
        html = ("<body><c4d-video-cta-container>"
                "<h1>Real Heading</h1><p>Body text here</p>"
                "</c4d-video-cta-container></body>")
        d = extract_site(html, url="https://x.test")
        self.assertEqual(d["h1_count"], "1")
        self.assertEqual(d["h1_text"], "Real Heading")
        self.assertIn("Body text here", d["visible_text_preview"])


LINKS_HTML = """
<html><head>
  <title>Linky</title>
  <link rel="stylesheet" href="/css/main.css">
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <link rel="alternate" hreflang="fr" href="https://linky.test/fr/">
  <meta property="og:image" content="/og.png">
</head><body>
  <nav><a href="/about">About</a><a href="/contact">Contact</a></nav>
  <a href="/about">About dup</a>
  <a href="https://github.com/acme">GitHub</a>
  <a href="https://facebook.com/acme">FB</a>
  <a href="https://external.com/page">Ext</a>
  <a href="/files/report.pdf">Report</a>
  <a href="mailto:hi@linky.test">Email</a>
  <a href="tel:+15550100">Call</a>
  <a href="javascript:void(0)">bad</a>
  <a href="#section">frag</a>
  <img src="/img/a.png" alt="A">
  <img data-src="/img/lazy.png">
  <img srcset="/img/s1.png 1x, /img/s2.png 2x">
  <script src="/js/app.js"></script>
  <script>inline()</script>
  <iframe src="https://www.youtube.com/embed/xyz"></iframe>
  <video src="/media/clip.mp4"></video>
</body></html>
"""


class TestLinkHarvest(unittest.TestCase):
    def _d(self):
        return extract_site(LINKS_HTML, url="https://linky.test/",
                            final_url="https://linky.test/")

    def test_relative_urls_absolutized(self):
        d = self._d()
        self.assertIn("https://linky.test/about", d["internal_links_list"].split("\n"))
        self.assertNotIn("/about", d["internal_links_list"].split("\n"))

    def test_internal_external_split(self):
        d = self._d()
        ext = d["external_links_list"].split("\n")
        self.assertIn("https://external.com/page", ext)
        self.assertIn("https://github.com/acme", ext)
        self.assertNotIn("https://linky.test/about", ext)

    def test_counts_equal_list_lengths(self):
        d = self._d()
        pairs = [
            ("internal_links", "internal_links_list"),
            ("external_links", "external_links_list"),
            ("external_domains", "external_domains_list"),
            ("all_urls_count", "all_urls"),
            ("image_urls_count", "image_urls"),
            ("document_links_count", "document_links"),
            ("media_urls_count", "media_urls"),
        ]
        for count_col, list_col in pairs:
            n = int(d[count_col])
            items = [x for x in d[list_col].split("\n") if x]
            self.assertEqual(n, len(items), f"{count_col} != len({list_col})")

    def test_dedupe_of_repeated_link(self):
        d = self._d()
        internal = d["internal_links_list"].split("\n")
        self.assertEqual(internal.count("https://linky.test/about"), 1)

    def test_junk_schemes_excluded(self):
        d = self._d()
        joined = d["all_urls"]
        self.assertNotIn("javascript:", joined)
        self.assertNotIn("#section", joined)

    def test_documents_classified(self):
        d = self._d()
        self.assertIn("https://linky.test/files/report.pdf", d["document_links"].split("\n"))

    def test_images_srcset_and_lazy(self):
        d = self._d()
        imgs = d["image_urls"].split("\n")
        for u in ("https://linky.test/img/a.png", "https://linky.test/img/lazy.png",
                  "https://linky.test/img/s1.png", "https://linky.test/img/s2.png"):
            self.assertIn(u, imgs)

    def test_media_from_iframe_and_video(self):
        d = self._d()
        media = d["media_urls"].split("\n")
        self.assertIn("https://www.youtube.com/embed/xyz", media)
        self.assertIn("https://linky.test/media/clip.mp4", media)

    def test_scripts_styles_feeds_hreflang(self):
        d = self._d()
        self.assertIn("https://linky.test/js/app.js", d["script_urls"])
        self.assertIn("https://linky.test/css/main.css", d["stylesheet_urls"])
        self.assertIn("https://linky.test/feed.xml", d["rss_feeds"])
        self.assertIn("https://linky.test/fr/", d["hreflang_urls"])

    def test_nav_links(self):
        d = self._d()
        nav = d["nav_links"].split("\n")
        self.assertIn("https://linky.test/about", nav)
        self.assertIn("https://linky.test/contact", nav)

    def test_mailto_tel_and_contacts(self):
        d = self._d()
        self.assertIn("hi@linky.test", d["mailto_links"])
        self.assertIn("+15550100", d["tel_links"])
        # folded into contacts (email behind a mailto with no visible text)
        self.assertIn("hi@linky.test", d["contact_emails"])
        self.assertIn("+15550100", d["contact_phones"])

    def test_social_superset_and_first(self):
        d = self._d()
        social = d["social_links"].split("\n")
        self.assertIn("https://github.com/acme", social)
        self.assertIn("https://facebook.com/acme", social)
        self.assertEqual(d["github_url"], "https://github.com/acme")
        self.assertEqual(d["facebook_url"], "https://facebook.com/acme")

    def test_list_cells_use_newline_delim(self):
        d = self._d()
        # multiple internal links must be newline-separated, never ';' or ','
        self.assertIn("\n", d["all_urls"])


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
            self.assertEqual(stats["columns"], 90)
            with open(out, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)               # header + 1 row
            self.assertEqual(rows[0], SCHEMA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
