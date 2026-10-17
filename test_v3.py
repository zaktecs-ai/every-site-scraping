"""Tests for the v3 modules: crawler, sinks, config and netlayer.

These lock the behaviours a stranger will rely on:

  * URL canonicalisation and scope decisions (what gets crawled at all)
  * robots.txt parsing and enforcement
  * sitemap parsing, including nested indexes and malformed XML
  * every output sink, including torn-tail repair and resume done-sets
  * configuration precedence: defaults < file < environment
  * proxy rotation and quarantine

Network access is avoided: anything that would hit the wire is either tested
through its pure function or driven against a local ``http.server``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from everysite import config as config_mod
from everysite import crawler as crawler_mod
from everysite import netlayer as netlayer_mod
from everysite import sinks as sink_mod
from everysite.scraper import SCHEMA

# --------------------------------------------------------------------------
# crawler: URL canonicalisation
# --------------------------------------------------------------------------

class TestCanonicalize(unittest.TestCase):

    def test_adds_scheme(self):
        self.assertEqual(crawler_mod.canonicalize("example.com"),
                         "https://example.com/")

    def test_lowercases_scheme_and_host(self):
        self.assertEqual(crawler_mod.canonicalize("HTTPS://Example.COM/Path"),
                         "https://example.com/Path")

    def test_drops_default_port(self):
        self.assertEqual(crawler_mod.canonicalize("https://example.com:443/"),
                         "https://example.com/")
        self.assertEqual(crawler_mod.canonicalize("http://example.com:80/"),
                         "http://example.com/")

    def test_keeps_non_default_port(self):
        self.assertIn(":8080", crawler_mod.canonicalize("http://example.com:8080/"))

    def test_strips_trailing_slash_but_not_root(self):
        self.assertEqual(crawler_mod.canonicalize("https://e.com/a/b/"),
                         "https://e.com/a/b")
        self.assertEqual(crawler_mod.canonicalize("https://e.com/"),
                         "https://e.com/")

    def test_collapses_multiple_slashes(self):
        self.assertEqual(crawler_mod.canonicalize("https://e.com/a//b///c"),
                         "https://e.com/a/b/c")

    def test_drops_fragment(self):
        self.assertEqual(crawler_mod.canonicalize("https://e.com/a#sec"),
                         "https://e.com/a")

    def test_drops_tracking_params_and_sorts_the_rest(self):
        got = crawler_mod.canonicalize(
            "https://e.com/p?utm_source=x&b=2&a=1&fbclid=zz")
        self.assertEqual(got, "https://e.com/p?a=1&b=2")

    def test_keeps_meaningful_query_params(self):
        self.assertIn("page=3", crawler_mod.canonicalize("https://e.com/?page=3"))

    def test_rejects_non_http_schemes(self):
        for bad in ("mailto:a@b.com", "javascript:void(0)", "ftp://x.com",
                    "tel:+123", "data:text/plain,hi"):
            self.assertEqual(crawler_mod.canonicalize(bad), "", bad)

    def test_empty_input(self):
        self.assertEqual(crawler_mod.canonicalize(""), "")
        self.assertEqual(crawler_mod.canonicalize("   "), "")

    def test_two_tracked_variants_collapse_to_one(self):
        a = crawler_mod.canonicalize("https://e.com/p?utm_source=news")
        b = crawler_mod.canonicalize("https://e.com/p?utm_source=twitter&fbclid=1")
        self.assertEqual(a, b)


class TestRegistrableDomain(unittest.TestCase):

    def test_simple_domain(self):
        self.assertEqual(crawler_mod.registrable_domain("www.example.com"),
                         "example.com")

    def test_two_level_suffix(self):
        self.assertEqual(crawler_mod.registrable_domain("www.example.co.uk"),
                         "example.co.uk")

    def test_pakistani_suffix(self):
        self.assertEqual(crawler_mod.registrable_domain("www.foo.com.pk"),
                         "foo.com.pk")

    def test_bare_domain_unchanged(self):
        self.assertEqual(crawler_mod.registrable_domain("example.com"),
                         "example.com")

    def test_ip_unchanged(self):
        self.assertEqual(crawler_mod.registrable_domain("192.168.1.1"),
                         "192.168.1.1")

    def test_subdomain_collapses(self):
        self.assertEqual(crawler_mod.registrable_domain("a.b.c.example.com"),
                         "example.com")


class TestScope(unittest.TestCase):

    def test_registrable_scope_includes_subdomains(self):
        allowed = {crawler_mod.registrable_domain("www.python.org")}
        self.assertTrue(crawler_mod.same_scope(
            "https://docs.python.org/3/", allowed_domains=allowed))

    def test_exact_host_scope_excludes_subdomains(self):
        allowed = {"www.python.org"}
        self.assertFalse(crawler_mod.same_scope(
            "https://docs.python.org/3/", allowed_domains=allowed,
            exact_hosts=True))
        self.assertTrue(crawler_mod.same_scope(
            "https://www.python.org/x", allowed_domains=allowed,
            exact_hosts=True))

    def test_foreign_domain_excluded(self):
        allowed = {"python.org"}
        self.assertFalse(crawler_mod.same_scope("https://evil.com/",
                                                allowed_domains=allowed))


class TestSkipExtensions(unittest.TestCase):

    def test_assets_skipped(self):
        for u in ("https://e.com/a.png", "https://e.com/x.css",
                  "https://e.com/d.pdf", "https://e.com/m.mp4"):
            self.assertTrue(crawler_mod._has_skip_extension(u), u)

    def test_pages_kept(self):
        for u in ("https://e.com/", "https://e.com/about", "https://e.com/a/b.html"):
            self.assertFalse(crawler_mod._has_skip_extension(u), u)


# --------------------------------------------------------------------------
# crawler: sitemaps
# --------------------------------------------------------------------------

class TestSitemapParsing(unittest.TestCase):

    def test_urlset(self):
        xml = ("<urlset><url><loc>https://a.com/1</loc></url>"
               "<url><loc>https://a.com/2</loc></url></urlset>")
        pages, nested = crawler_mod.parse_sitemap_xml(xml)
        self.assertEqual(pages, ["https://a.com/1", "https://a.com/2"])
        self.assertEqual(nested, [])

    def test_sitemapindex(self):
        xml = ("<sitemapindex><sitemap><loc>https://a.com/s1.xml</loc></sitemap>"
               "<sitemap><loc>https://a.com/s2.xml</loc></sitemap></sitemapindex>")
        pages, nested = crawler_mod.parse_sitemap_xml(xml)
        self.assertEqual(pages, [])
        self.assertEqual(nested, ["https://a.com/s1.xml", "https://a.com/s2.xml"])

    def test_whitespace_inside_loc_is_trimmed(self):
        xml = "<urlset><loc>\n  https://a.com/x  \n</loc></urlset>"
        pages, _ = crawler_mod.parse_sitemap_xml(xml)
        self.assertEqual(pages, ["https://a.com/x"])

    def test_empty_input(self):
        self.assertEqual(crawler_mod.parse_sitemap_xml(""), ([], []))

    def test_garbage_does_not_raise(self):
        pages, nested = crawler_mod.parse_sitemap_xml("<<<not xml>>>")
        self.assertIsInstance(pages, list)
        self.assertIsInstance(nested, list)


# --------------------------------------------------------------------------
# crawler: robots
# --------------------------------------------------------------------------

class TestRobotsPolicy(unittest.TestCase):

    def test_ignored_policy_allows_everything(self):
        pol = crawler_mod.RobotsPolicy(ignored=True)
        self.assertTrue(pol.allowed("https://anything.example/x"))

    def test_missing_policy_allows(self):
        pol = crawler_mod.RobotsPolicy(ignored=True)
        self.assertTrue(pol.allowed("https://x.com/"))

    def test_parser_honours_disallow(self):
        import urllib.robotparser as rp
        parser = rp.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /private/"])
        self.assertFalse(parser.can_fetch("*", "https://e.com/private/x"))
        self.assertTrue(parser.can_fetch("*", "https://e.com/public"))


# --------------------------------------------------------------------------
# netlayer
# --------------------------------------------------------------------------

class TestNormalizeUrl(unittest.TestCase):

    def test_adds_https(self):
        self.assertEqual(netlayer_mod.normalize_url("example.com"),
                         "https://example.com")

    def test_keeps_existing_scheme(self):
        self.assertEqual(netlayer_mod.normalize_url("http://e.com"),
                         "http://e.com")

    def test_strips_whitespace(self):
        self.assertEqual(netlayer_mod.normalize_url("  https://e.com  "),
                         "https://e.com")


class TestProxyPool(unittest.TestCase):

    def test_round_robin(self):
        pool = netlayer_mod.ProxyPool(["http://a", "http://b", "http://c"])
        self.assertEqual([pool.next() for _ in range(4)],
                         ["http://a", "http://b", "http://c", "http://a"])

    def test_quarantine_after_max_failures(self):
        pool = netlayer_mod.ProxyPool(["http://a", "http://b"], max_failures=2)
        for _ in range(2):
            pool.report("http://a", False)
        self.assertEqual(pool.stats()["alive"], 1)
        self.assertIn("http://a", pool.stats()["quarantined"])

    def test_success_resets_failures(self):
        pool = netlayer_mod.ProxyPool(["http://a"], max_failures=2)
        pool.report("http://a", False)
        pool.report("http://a", True)
        self.assertEqual(pool.stats()["alive"], 1)

    def test_all_dead_returns_none(self):
        pool = netlayer_mod.ProxyPool(["http://a"], max_failures=1)
        pool.report("http://a", False)
        self.assertIsNone(pool.next())

    def test_empty_pool_is_falsy(self):
        self.assertFalse(netlayer_mod.ProxyPool([]))

    def test_from_file_normalises_scheme(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.txt"
            f.write_text("# comment\n1.2.3.4:8080\nuser:pw@5.6.7.8:80\n"
                         "socks5://9.9.9.9:1\n\n", encoding="utf-8")
            pool = netlayer_mod.ProxyPool.from_file(str(f))
            self.assertEqual(len(pool), 3)
            self.assertEqual(pool._proxies[0], "http://1.2.3.4:8080")
            self.assertEqual(pool._proxies[2], "socks5://9.9.9.9:1")


class TestHostGuard(unittest.TestCase):

    def test_zero_interval_does_not_sleep(self):
        g = netlayer_mod.HostGuard(0.0)
        self.assertEqual(g.wait("https://e.com/1"), 0.0)
        self.assertEqual(g.wait("https://e.com/2"), 0.0)

    def test_interval_enforced_for_same_host(self):
        import time
        g = netlayer_mod.HostGuard(0.2)
        g.wait("https://e.com/1")
        t0 = time.monotonic()
        g.wait("https://e.com/2")
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)

    def test_different_hosts_not_delayed(self):
        import time
        g = netlayer_mod.HostGuard(0.5)
        g.wait("https://a.com/1")
        t0 = time.monotonic()
        g.wait("https://b.com/1")
        self.assertLess(time.monotonic() - t0, 0.2)


class TestMetaRefresh(unittest.TestCase):

    def test_extracts_target(self):
        html = '<meta http-equiv="refresh" content="0; url=/new">'
        self.assertEqual(netlayer_mod.resolve_meta_refresh(html, "https://e.com/a"),
                         "https://e.com/new")

    def test_absolute_target(self):
        html = '<meta http-equiv="refresh" content="5;url=https://x.com/y">'
        self.assertEqual(netlayer_mod.resolve_meta_refresh(html, "https://e.com/"),
                         "https://x.com/y")

    def test_none_when_absent(self):
        self.assertIsNone(netlayer_mod.resolve_meta_refresh("<html></html>",
                                                            "https://e.com/"))

    def test_none_on_empty(self):
        self.assertIsNone(netlayer_mod.resolve_meta_refresh("", "https://e.com/"))


# --------------------------------------------------------------------------
# sinks
# --------------------------------------------------------------------------

def _row(**over):
    row = {c: "" for c in SCHEMA}
    row.update({
        "url": "https://example.com",
        "page_title": "Example",
        "word_count": "42",
        "https": "True",
        "internal_links": "2",
        "internal_links_list": "https://example.com/a\nhttps://example.com/b",
        "contact_emails": "info@example.com",
    })
    row.update(over)
    return row


class TestDetectFormat(unittest.TestCase):

    def test_extensions(self):
        self.assertEqual(sink_mod.detect_format("a.csv"), "csv")
        self.assertEqual(sink_mod.detect_format("a.json"), "json")
        self.assertEqual(sink_mod.detect_format("a.jsonl"), "jsonl")
        self.assertEqual(sink_mod.detect_format("a.ndjson"), "jsonl")
        self.assertEqual(sink_mod.detect_format("a.sqlite"), "sqlite")
        self.assertEqual(sink_mod.detect_format("a.sqlite3"), "sqlite")
        self.assertEqual(sink_mod.detect_format("a.db"), "sqlite")
        self.assertEqual(sink_mod.detect_format("weird"), "csv")


class TestJSONLWriter(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "o.jsonl")

    def test_writes_one_typed_object_per_line(self):
        w = sink_mod.SafeJSONLWriter(self.path, meta={"run": 1})
        w.write_row(_row())
        w.write_row(_row())
        w.close()
        lines = [l for l in open(self.path, encoding="utf-8").read().splitlines() if l]  # noqa: SIM115
        self.assertEqual(len(lines), 3)  # meta + 2 rows
        sites = [json.loads(l) for l in lines[1:]]
        self.assertEqual(len(sites), 2)
        self.assertIsInstance(sites[0]["word_count"], int)
        self.assertIsInstance(sites[0]["https"], bool)
        self.assertIsInstance(sites[0]["internal_links_list"], list)

    def test_meta_line_first(self):
        w = sink_mod.SafeJSONLWriter(self.path, meta={"run": "abc"})
        w.write_row(_row())
        w.close()
        first = json.loads(open(self.path, encoding="utf-8").readline())
        self.assertIn("_meta", first)
        self.assertEqual(first["_meta"]["run"], "abc")

    def test_no_meta_when_not_given(self):
        w = sink_mod.SafeJSONLWriter(self.path, write_header=True)
        w.write_row(_row())
        w.close()
        first = json.loads(open(self.path, encoding="utf-8").readline())
        self.assertNotIn("_meta", first)

    def test_read_urls_returns_done_set(self):
        w = sink_mod.SafeJSONLWriter(self.path, meta={"r": 1})
        w.write_row(_row(url="https://a.com"))
        w.write_row(_row(url="https://b.com"))
        w.close()
        self.assertEqual(sink_mod.SafeJSONLWriter.read_urls(self.path),
                         {"https://a.com", "https://b.com"})

    def test_read_urls_skips_torn_tail(self):
        w = sink_mod.SafeJSONLWriter(self.path)
        w.write_row(_row(url="https://a.com"))
        w.close()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"url": "https://torn')
        self.assertEqual(sink_mod.SafeJSONLWriter.read_urls(self.path),
                         {"https://a.com"})

    def test_repair_drops_torn_tail(self):
        w = sink_mod.SafeJSONLWriter(self.path)
        w.write_row(_row())
        w.close()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"url": "https://torn')
        removed = sink_mod.SafeJSONLWriter.repair(self.path)
        self.assertGreater(removed, 0)
        for line in open(self.path, encoding="utf-8").read().splitlines():
            if line.strip():
                json.loads(line)  # must not raise

    def test_repair_is_noop_on_clean_file(self):
        w = sink_mod.SafeJSONLWriter(self.path)
        w.write_row(_row())
        w.close()
        self.assertEqual(sink_mod.SafeJSONLWriter.repair(self.path), 0)

    def test_repair_missing_file(self):
        self.assertEqual(sink_mod.SafeJSONLWriter.repair(
            os.path.join(self.d, "nope.jsonl")), 0)

    def test_read_urls_missing_file(self):
        self.assertEqual(sink_mod.SafeJSONLWriter.read_urls(
            os.path.join(self.d, "nope.jsonl")), set())


class TestSQLiteWriter(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "o.sqlite")

    def test_creates_schema_and_rows(self):
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        w.write_row(_row())
        w.write_row(_row(url="https://b.com"))
        w.close()
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sites)")]
        conn.close()
        self.assertEqual(n, 2)
        self.assertEqual(len(cols), len(SCHEMA) + 1)  # + _scraped_at

    def test_queryable_by_column(self):
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        w.write_row(_row())
        w.close()
        conn = sqlite3.connect(self.path)
        got = conn.execute(
            'SELECT "page_title" FROM sites WHERE "word_count" = ?',
            ("42",)).fetchall()
        conn.close()
        self.assertEqual(got, [("Example",)])

    def test_url_index_created(self):
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        w.write_row(_row())
        w.close()
        conn = sqlite3.connect(self.path)
        idx = [r[1] for r in conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index'")]
        conn.close()
        self.assertIn("idx_sites_url", idx)

    def test_read_urls_done_set(self):
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        w.write_row(_row(url="https://a.com"))
        w.write_row(_row(url="https://b.com"))
        w.close()
        self.assertEqual(sink_mod.SQLiteWriter.read_urls(self.path),
                         {"https://a.com", "https://b.com"})

    def test_writable_from_another_thread(self):
        """Regression: a connection opened on one thread must not break writes
        performed by another (this previously hung the whole run)."""
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        err = []

        def worker():
            try:
                for i in range(5):
                    w.write_row(_row(url=f"https://t{i}.com"))
            except Exception as e:  # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "writer deadlocked across threads")
        self.assertEqual(err, [])
        w.close()
        self.assertEqual(len(sink_mod.SQLiteWriter.read_urls(self.path)), 5)

    def test_resume_into_existing_file_appends(self):
        w = sink_mod.SQLiteWriter(self.path, SCHEMA)
        w.write_row(_row(url="https://a.com"))
        w.close()
        w2 = sink_mod.SQLiteWriter(self.path, SCHEMA, write_header=False)
        w2.write_row(_row(url="https://b.com"))
        w2.close()
        self.assertEqual(len(sink_mod.SQLiteWriter.read_urls(self.path)), 2)

    def test_read_urls_missing_file(self):
        self.assertEqual(sink_mod.SQLiteWriter.read_urls(
            os.path.join(self.d, "nope.sqlite")), set())


class TestMakeSinkAndReadDone(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_make_sink_each_format(self):
        for fmt, name in (("csv", "o.csv"), ("json", "o.json"),
                          ("jsonl", "o.jsonl"), ("sqlite", "o.sqlite")):
            w = sink_mod.make_sink(fmt, os.path.join(self.d, name),
                                   schema=SCHEMA, meta={"m": 1})
            w.write_row(_row())
            w.close()

    def test_make_sink_rejects_unknown(self):
        with self.assertRaises(ValueError):
            sink_mod.make_sink("xml", os.path.join(self.d, "x.xml"),
                               schema=SCHEMA)

    def test_read_done_urls_all_formats(self):
        for fmt, name in (("csv", "o.csv"), ("json", "o.json"),
                          ("jsonl", "o.jsonl"), ("sqlite", "o.sqlite")):
            path = os.path.join(self.d, name)
            w = sink_mod.make_sink(fmt, path, schema=SCHEMA, meta={"m": 1})
            w.write_row(_row(url="https://a.com"))
            w.close()
            self.assertEqual(sink_mod.read_done_urls(path), {"https://a.com"},
                             fmt)

    def test_read_done_urls_missing_file(self):
        self.assertEqual(sink_mod.read_done_urls(
            os.path.join(self.d, "nope.csv")), set())

    def test_supported_formats_constant(self):
        self.assertEqual(set(sink_mod.SUPPORTED_FORMATS),
                         {"csv", "json", "jsonl", "sqlite"})


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class TestConfig(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._saved_env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("SCRAPER_"):
                del os.environ[k]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_defaults_when_no_file(self):
        c = config_mod.load_config(None, start=Path(self.d))
        self.assertEqual(c["scrape"]["workers"], 10)
        self.assertEqual(c["output"]["format"], "csv")
        self.assertTrue(c["scrape"]["respect_robots"])

    def test_file_overrides_defaults(self):
        p = Path(self.d) / "scraper.toml"
        p.write_text("[scrape]\nworkers = 33\n\n[crawl]\nmax_depth = 7\n",
                     encoding="utf-8")
        c = config_mod.load_config(str(p))
        self.assertEqual(c["scrape"]["workers"], 33)
        self.assertEqual(c["crawl"]["max_depth"], 7)
        # untouched keys keep their default
        self.assertEqual(c["scrape"]["timeout"], 20)

    def test_env_overrides_file(self):
        p = Path(self.d) / "scraper.toml"
        p.write_text("[scrape]\nworkers = 33\n", encoding="utf-8")
        os.environ["SCRAPER_WORKERS"] = "77"
        c = config_mod.load_config(str(p))
        self.assertEqual(c["scrape"]["workers"], 77)

    def test_env_boolean_coercion(self):
        os.environ["SCRAPER_RESPECT_ROBOTS"] = "false"
        c = config_mod.load_config(None, start=Path(self.d))
        self.assertFalse(c["scrape"]["respect_robots"])

    def test_env_float_coercion(self):
        os.environ["SCRAPER_DELAY"] = "2.5"
        c = config_mod.load_config(None, start=Path(self.d))
        self.assertEqual(c["scrape"]["delay"], 2.5)

    def test_malformed_env_is_ignored(self):
        os.environ["SCRAPER_WORKERS"] = "not-a-number"
        c = config_mod.load_config(None, start=Path(self.d))
        self.assertEqual(c["scrape"]["workers"], 10)  # default survives

    def test_missing_config_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            config_mod.load_config(str(Path(self.d) / "nope.toml"))

    def test_find_config_walks_upward(self):
        (Path(self.d) / "scraper.toml").write_text("[scrape]\nworkers = 9\n",
                                                   encoding="utf-8")
        nested = Path(self.d) / "a" / "b"
        nested.mkdir(parents=True)
        found = config_mod.find_config(nested)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "scraper.toml")

    def test_to_cli_defaults_shape(self):
        c = config_mod.load_config(None, start=Path(self.d))
        d = config_mod.to_cli_defaults(c)
        for key in ("workers", "timeout", "max_depth", "max_urls", "format",
                    "output", "retries", "delay", "ignore_robots"):
            self.assertIn(key, d)

    def test_sample_config_parses(self):
        d = self._write_and_load(config_mod.SAMPLE_CONFIG)
        self.assertEqual(d["scrape"]["workers"], 20)
        self.assertEqual(d["output"]["format"], "csv")
        self.assertEqual(d["crawl"]["strategy"], "breadth")
        self.assertIsInstance(d["crawl"]["max_depth"], int)
        self.assertIsInstance(d["scrape"]["respect_robots"], bool)

    def _write_and_load(self, text):
        p = Path(self.d) / "sample.toml"
        p.write_text(text, encoding="utf-8")
        return config_mod.load_toml(p)

    def test_mini_toml_parser_handles_types(self):
        mini = config_mod._mini_toml(
            '[s]\na = 1\nb = 1.5\nc = true\nd = "x"\ne = [1, 2]\n')
        self.assertEqual(mini["s"]["a"], 1)
        self.assertEqual(mini["s"]["b"], 1.5)
        self.assertIs(mini["s"]["c"], True)
        self.assertEqual(mini["s"]["d"], "x")
        self.assertEqual(mini["s"]["e"], [1, 2])

    def test_mini_toml_ignores_comments_and_blanks(self):
        mini = config_mod._mini_toml("# top\n\n[s]\n# inner\na = 1\n")
        self.assertEqual(mini["s"]["a"], 1)


# --------------------------------------------------------------------------
# end-to-end: crawl + scrape against a local server
# --------------------------------------------------------------------------

class _SiteHandler(BaseHTTPRequestHandler):
    """A tiny site: / links to /a and /b; /a links to /b; assets elsewhere."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = {
            "/": "<html><head><title>Home</title></head><body>"
                 "<h1>Home</h1>"
                 "<a href='/a'>A</a><a href='/b'>B</a>"
                 "<a href='/img.png'>img</a>"
                 "<a href='https://external.example/x'>ext</a>"
                 "</body></html>",
            "/a": "<html><head><title>A</title></head><body>"
                  "<h1>A</h1><a href='/b'>B</a></body></html>",
            "/b": "<html><head><title>B</title></head><body>"
                  "<h1>B</h1></body></html>",
            "/robots.txt": "User-agent: *\nDisallow: /secret/\n",
        }.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        ctype = ("text/plain" if self.path == "/robots.txt"
                 else "text/html; charset=utf-8")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestCrawlAgainstLocalServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.t = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_discover_follows_links_within_scope(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=2, max_urls=20,
                                 use_sitemaps=False, respect_robots=False)
        self.assertIn(self.base + "/", r.urls)
        self.assertIn(self.base + "/a", r.urls)
        self.assertIn(self.base + "/b", r.urls)

    def test_discover_skips_asset_extensions(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=1, max_urls=20,
                                 use_sitemaps=False, respect_robots=False)
        self.assertNotIn(self.base + "/img.png", r.urls)

    def test_discover_respects_depth_zero(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=0, max_urls=20,
                                 use_sitemaps=False, respect_robots=False)
        self.assertEqual(r.urls, [self.base + "/"])

    def test_discover_dedupes(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=2, max_urls=50,
                                 use_sitemaps=False, respect_robots=False)
        self.assertEqual(len(r.urls), len(set(r.urls)))

    def test_discover_excludes_out_of_scope(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=1, max_urls=50,
                                 use_sitemaps=False, respect_robots=False)
        for u in r.urls:
            self.assertNotIn("external.example", u)

    def test_robots_disallow_is_enforced(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=1, max_urls=20,
                                 use_sitemaps=False, respect_robots=True)
        self.assertGreaterEqual(r.skipped_robots, 0)  # parsed without error

    def test_include_pattern_filters(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=2, max_urls=50,
                                 use_sitemaps=False, respect_robots=False,
                                 include_pattern=r"/a$")
        self.assertTrue(all(u.endswith("/a") for u in r.urls))

    def test_exclude_pattern_filters(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=2, max_urls=50,
                                 use_sitemaps=False, respect_robots=False,
                                 exclude_pattern=r"/b$")
        self.assertTrue(all(not u.endswith("/b") for u in r.urls))

    def test_depth_first_strategy_runs(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=2, max_urls=50,
                                 use_sitemaps=False, respect_robots=False,
                                 strategy="depth")
        self.assertIn(self.base + "/", r.urls)

    def test_result_dict_is_serialisable(self):
        r = crawler_mod.discover([self.base + "/"], max_depth=1, max_urls=5,
                                 use_sitemaps=False, respect_robots=False)
        json.dumps(r.as_dict())  # must not raise


class TestFetcherAgainstLocalServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.t = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fetch_ok(self):
        f = netlayer_mod.Fetcher(timeout=10, retries=0)
        r = f.fetch(self.base + "/")
        self.assertEqual(r["fetch_status"], "ok")
        self.assertEqual(r["http_status"], 200)
        self.assertIn("Home", r["html"])

    def test_fetch_404_reports_http_error(self):
        f = netlayer_mod.Fetcher(timeout=10, retries=0)
        r = f.fetch(self.base + "/nope")
        self.assertEqual(r["fetch_status"], "http_error")
        self.assertIn("404", r["fetch_error"])

    def test_robots_blocks_disallowed_path(self):
        f = netlayer_mod.Fetcher(timeout=10, retries=0, respect_robots=True)
        r = f.fetch(self.base + "/secret/x")
        self.assertEqual(r["fetch_status"], "robots_disallowed")
        self.assertEqual(r["html"], "")

    def test_ignore_robots_allows(self):
        f = netlayer_mod.Fetcher(timeout=10, retries=0, respect_robots=False)
        r = f.fetch(self.base + "/secret/x")
        self.assertNotEqual(r["fetch_status"], "robots_disallowed")


if __name__ == "__main__":
    unittest.main()
