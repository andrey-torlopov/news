from __future__ import annotations

import importlib.util
from datetime import date
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "newsfetch.py"
SPEC = importlib.util.spec_from_file_location("newsfetch", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
newsfetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = newsfetch
SPEC.loader.exec_module(newsfetch)


class DateParsingTests(unittest.TestCase):
    def test_date_without_time_stays_date(self) -> None:
        parsed, has_time = newsfetch.parse_datetime("2026-08-07")

        self.assertEqual(parsed, date(2026, 8, 7))
        self.assertFalse(has_time)
        self.assertEqual(newsfetch.iso_value(parsed, has_time), "2026-08-07")

    def test_rfc822_date_is_converted_to_moscow(self) -> None:
        parsed, has_time = newsfetch.parse_datetime(
            "Thu, 06 Aug 2026 20:15:00 +0000"
        )

        self.assertTrue(has_time)
        self.assertEqual(
            newsfetch.iso_value(parsed, has_time), "2026-08-06T23:15:00+03:00"
        )


class FeedParsingTests(unittest.TestCase):
    def test_rss2_core_and_field_items(self) -> None:
        body = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item><title>Core &amp; item</title><link>https://example.org/core</link>
            <pubDate>Thu, 06 Aug 2026 20:15:00 +0000</pubDate>
            <description><![CDATA[<b>Core description</b>]]></description></item>
          <item><title>Field item</title><link>https://example.org/field</link>
            <pubDate>Fri, 31 Jul 2026 09:00:00 +0000</pubDate></item>
        </channel></rss>"""

        fmt, total, items, oldest, incomplete = newsfetch.parse_feed(
            body, date(2026, 8, 8), 7, 300
        )

        self.assertEqual(fmt, "rss2")
        self.assertEqual(total, 2)
        self.assertEqual([item["zone"] for item in items], ["ядро", "поле"])
        self.assertEqual(items[0]["title"], "Core & item")
        self.assertEqual(items[0]["description"], "Core description")
        self.assertEqual(oldest, "2026-07-31T12:00:00+03:00")
        self.assertFalse(incomplete)

    def test_atom_and_rdf_are_namespace_independent(self) -> None:
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Atom item</title><link href="https://example.org/atom" />
          <updated>2026-08-07T10:00:00Z</updated><summary>Summary</summary></entry>
        </feed>"""
        rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <item><title>RDF item</title><link>https://example.org/rdf</link>
          <dc:date>2026-08-01</dc:date><description>RDF summary</description></item>
        </rdf:RDF>"""

        atom_result = newsfetch.parse_feed(atom, date(2026, 8, 8), 7, 300)
        rdf_result = newsfetch.parse_feed(rdf, date(2026, 8, 8), 7, 300)

        self.assertEqual(atom_result[0], "atom")
        self.assertEqual(atom_result[2][0]["link"], "https://example.org/atom")
        self.assertEqual(rdf_result[0], "rdf")
        self.assertEqual(rdf_result[2][0]["date"], "2026-08-01")
        self.assertFalse(rdf_result[2][0]["has_time"])


class PageParsingTests(unittest.TestCase):
    def test_page_metadata_primary_links_and_visible_text(self) -> None:
        body = b"""<!doctype html><html><head>
          <title>Test article</title>
          <meta property="article:published_time" content="2026-08-07T12:30:00Z">
          <link rel="canonical" href="/canonical">
          <script type="application/ld+json">{"datePublished":"2026-08-06"}</script>
        </head><body><p>Visible text</p>
          <a href="https://doi.org/10.1000/test">Paper</a>
          <script>hidden text</script></body></html>"""

        result = newsfetch.parse_page(body, "https://example.org/article", 1500)

        self.assertEqual(result["title"], "Test article")
        self.assertEqual(result["canonical"], "https://example.org/canonical")
        self.assertEqual(result["page_date"], "2026-08-07T15:30:00+03:00")
        self.assertEqual(result["page_date_source"], "article:published_time")
        self.assertEqual(result["source_links"], ["https://doi.org/10.1000/test"])
        self.assertIn("Visible text", result["text"])
        self.assertNotIn("hidden text", result["text"])


class FetchRetryTests(unittest.TestCase):
    @staticmethod
    def result(
        *, http: int, returncode: int, error: str = "", body: bytes = b""
    ) -> object:
        return newsfetch.FetchResult(
            http=http,
            content_type="application/xml",
            effective_url="https://example.org/feed",
            body=body,
            error=error,
            returncode=returncode,
        )

    def test_network_retry_is_spent_once_per_process(self) -> None:
        calls: list[bool] = []
        results = iter(
            [
                self.result(http=503, returncode=0),
                self.result(http=200, returncode=0, body=b"ok"),
                self.result(http=503, returncode=0),
            ]
        )

        def fake_fetcher(*args: object, **kwargs: object) -> object:
            calls.append(bool(kwargs["compressed"]))
            return next(results)

        client = newsfetch.FetchClient(
            curl_path="curl", timeout=35, max_body_bytes=1000, fetcher=fake_fetcher
        )

        first = client.fetch("https://example.org/feed")
        second = client.fetch("https://example.org/another")

        self.assertEqual(first.request_count, 2)
        self.assertEqual(first.retry_mechanism, "сеть")
        self.assertTrue(first.retry_succeeded)
        self.assertEqual(second.request_count, 1)
        self.assertEqual(calls, [True, True, True])

    def test_compression_retry_disables_compressed_mode(self) -> None:
        calls: list[bool] = []
        results = iter(
            [
                self.result(http=200, returncode=61, error="bad content encoding"),
                self.result(http=200, returncode=0, body=b"ok"),
            ]
        )

        def fake_fetcher(*args: object, **kwargs: object) -> object:
            calls.append(bool(kwargs["compressed"]))
            return next(results)

        client = newsfetch.FetchClient(
            curl_path="curl", timeout=35, max_body_bytes=1000, fetcher=fake_fetcher
        )

        result = client.fetch("https://example.org/feed")

        self.assertEqual(result.request_count, 2)
        self.assertEqual(result.retry_mechanism, "сжатие")
        self.assertTrue(result.retry_succeeded)
        self.assertEqual(calls, [True, False])

    def test_size_limit_failure_is_not_retried(self) -> None:
        calls: list[bool] = []

        def fake_fetcher(*args: object, **kwargs: object) -> object:
            calls.append(bool(kwargs["compressed"]))
            return self.result(
                http=200, returncode=63, error="response exceeded 1000 bytes"
            )

        client = newsfetch.FetchClient(
            curl_path="curl", timeout=35, max_body_bytes=1000, fetcher=fake_fetcher
        )

        result = client.fetch("https://example.org/feed")

        self.assertEqual(result.request_count, 1)
        self.assertIsNone(result.retry_mechanism)
        self.assertEqual(calls, [True])


class InputValidationTests(unittest.TestCase):
    def test_bad_feed_row_is_reported_without_fetch(self) -> None:
        args = argparse_namespace(date="2026-08-08", period=7, desc=300)
        client = unittest.mock.Mock()
        stdout = io.StringIO()

        with patch.object(newsfetch.sys, "stdin", io.StringIO("broken-line\n")):
            with patch.object(newsfetch.sys, "stdout", stdout):
                exit_code = newsfetch.feeds_mode(args, client)

        self.assertEqual(exit_code, 2)
        self.assertIn('"kind":"ERROR"', stdout.getvalue())
        client.fetch.assert_not_called()


def argparse_namespace(**values: object) -> object:
    class Namespace:
        pass

    namespace = Namespace()
    for key, value in values.items():
        setattr(namespace, key, value)
    return namespace


if __name__ == "__main__":
    unittest.main()
