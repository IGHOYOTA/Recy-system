import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from recy.api import create_server
from recy.database import Database, normalize_upc
from recy.source import RegistryHTML, Scraper, SourceError, SOURCE_URL, USER_AGENT

FIXTURES = Path(__file__).parent / "fixtures"
RESULTS = (FIXTURES / "results.html").read_text()


class ScraperTests(unittest.TestCase):
    def scraper(self, results=RESULTS):
        return Scraper(fetch=lambda url: results, delay=0)

    def test_scrapes_unfiltered_results_preserves_columns_and_missing_upcs(self):
        snapshot = self.scraper().scrape()
        self.assertEqual(snapshot.headers, ["UPC Code", "Brand Name", "Flavour", "Size"])
        self.assertEqual(len(snapshot.rows), 2)
        self.assertIn(("001234567890", "Example & Co", "Water", "500 mL"), snapshot.rows)

    def test_rejects_truncated_or_uncounted_results(self):
        for text in [RESULTS.replace("products: 2", "products: 200"),
                     RESULTS.replace("Total products: 2", "Search results")]:
            with self.assertRaises(SourceError):
                self.scraper(text).scrape()

    def test_paginates_and_checks_total(self):
        first = RESULTS.replace("products: 2", "products: 4") + '<a rel="next" href="?page=2">Next</a>'
        second = RESULTS.replace("products: 2", "products: 4").replace("Example", "Other")
        def fetch(url):
            return second if "page=2" in url else first
        snapshot = Scraper(fetch=fetch).scrape()
        self.assertEqual(snapshot.pages, 2)
        self.assertEqual(len(snapshot.rows), 4)

    def test_rejects_redirecting_pagination(self):
        with self.assertRaises(SourceError):
            self.scraper(RESULTS + '<a rel="next" href="https://example.com/">Next</a>').scrape()

    def test_starts_at_exact_empty_search_url(self):
        requested = []
        def fetch(url):
            requested.append(url)
            return RESULTS
        snapshot = Scraper(fetch=fetch).scrape()
        exact = 'https://www.return-it.ca/registeredbrands/?upc=&St=&flvr=&type=&size='
        self.assertEqual(requested, [exact])
        self.assertEqual(snapshot.source_url, exact)

    def test_numbered_pages_without_total_count(self):
        first = RESULTS.replace('<p>Total products: 2</p>', '')
        second = first.replace('Example', 'Second')
        third = first.replace('Example', 'Third')
        # Only numbered links, including the last page, with the site's arbitrary key.
        links = '<nav><a href="?Pg=2">2</a><a href="?Pg=3">3</a></nav>'
        requested = []
        def fetch(url):
            requested.append(url)
            if 'Pg=2' in url:
                return second + '<a href="?Pg=3">3</a>'
            if 'Pg=3' in url:
                return third
            return first + links
        snapshot = Scraper(fetch=fetch).scrape()
        self.assertEqual(snapshot.pages, 3)
        self.assertEqual(len(snapshot.rows), 6)
        self.assertEqual(len(requested), 3)
        self.assertTrue(all(field + '=' in url for url in requested
                            for field in ('upc', 'St', 'flvr', 'type', 'size')))

    def test_missing_numbered_page_rejected(self):
        first = RESULTS.replace('<p>Total products: 2</p>', '')
        with self.assertRaisesRegex(SourceError, 'Missing numbered'):
            Scraper(fetch=lambda url: first + '<a href="?Pg=3">3</a>'
                    if url == SOURCE_URL else first.replace('Example', 'Third')).scrape()

    def test_failed_numbered_page_rejected(self):
        def fetch(url):
            if url != SOURCE_URL:
                raise SourceError('HTTP 403')
            return RESULTS + '<a href="?Pg=2">2</a>'
        with self.assertRaisesRegex(SourceError, 'HTTP 403'):
            Scraper(fetch=fetch).scrape()

    def test_repeated_page_data_rejected(self):
        with self.assertRaisesRegex(SourceError, 'Overlapping'):
            self.scraper(RESULTS + '<a href="?Pg=2">2</a>').scrape()

    def test_filtered_page_links_rejected(self):
        with self.assertRaisesRegex(SourceError, 'changed unfiltered'):
            self.scraper(RESULTS + '<a href="?type=Water&amp;Pg=2">2</a>').scrape()

    def test_rejects_schema_change_during_pagination(self):
        def fetch(url):
            if url == SOURCE_URL:
                return RESULTS + '<a href="?Pg=2">2</a>'
            return RESULTS.replace('Brand Name', 'Changed').replace('Example', 'Second')
        with self.assertRaisesRegex(SourceError, 'schema changed'):
            Scraper(fetch=fetch).scrape()

    def test_rejects_malformed_table(self):
        with self.assertRaises(SourceError):
            RegistryHTML(RESULTS.replace('<td>1 L</td>', '')).products()

    def test_recovers_live_malformed_flavour_without_shifting_columns(self):
        document = RegistryHTML((FIXTURES / 'malformed_flavour.html').read_text())
        headers, rows = document.products()
        self.assertEqual(headers, ['#', 'Brand', 'Flavour', 'Product Type',
                                   'Container Type', 'UPC', 'Photo'])
        self.assertEqual(len(rows), 30)
        by_number = {row[0]: dict(zip(headers, row)) for row in rows}
        self.assertEqual(by_number['75501']['Flavour'],
                         'Afternoon Tea FRUITS & ICE TEA <Orange & Grapefrui')
        self.assertEqual(by_number['75502']['Flavour'],
                         'Afternoon Tea FRUITS & ICE TEA <White Grape & Lemo')
        self.assertEqual(by_number['75501']['UPC'], '4909411114718')
        self.assertEqual(by_number['75502']['UPC'], '4909411114732')
        self.assertEqual(by_number['75501']['Product Type'], 'Tea')

    def test_preserves_valid_markup_and_escaped_angle_brackets(self):
        html = RESULTS.replace('<td>Water</td>',
            '<td><span title="comparison < value">Water</span> &lt;still literal</td>')
        _, rows = RegistryHTML(html).products()
        self.assertTrue(all(row[2] == 'Water <still literal' for row in rows))

    def test_malformed_flavour_survives_checkpoint_round_trip(self):
        from recy.scheduled import compact_page
        html = ('<p title="156377 products">Database Last Updated October 23, 2026</p>' +
                (FIXTURES / 'malformed_flavour.html').read_text())
        self.assertEqual(RegistryHTML(html).products(),
                         RegistryHTML(compact_page(html)).products())

    def test_disabled_next_link_on_last_page_is_ignored(self):
        first = RESULTS.replace('products: 2', 'products: 4') + '<a href="?Rn=2">2</a>'
        second = RESULTS.replace('products: 2', 'products: 4').replace('Example', 'Second')
        second += '<a class="next disabled" href="?Rn=2">Next</a>'
        snapshot = Scraper(fetch=lambda url: first if url == SOURCE_URL else second).scrape()
        self.assertEqual(snapshot.pages, 2)
        self.assertEqual(len(snapshot.rows), 4)

    def test_reads_live_product_total_from_title(self):
        first = RegistryHTML((FIXTURES / 'live_first.html').read_text())
        self.assertEqual(first.result_counts, {156377})
        self.assertEqual(first.products()[0],
                         ['#', 'Brand', 'Flavour', 'Product Type', 'Container Type', 'UPC', 'Photo'])
        self.assertEqual(len(first.products()[1]), 30)

    def test_probe_captured_live_pages(self):
        requested = []
        def fetch(url):
            requested.append(url)
            name = 'last' if 'Rn=5213' in url else 'second' if 'Rn=2' in url else 'first'
            return (FIXTURES / ('live_' + name + '.html')).read_text()
        report = Scraper(fetch=fetch).probe()
        self.assertEqual(report['total_products'], 156377)
        self.assertEqual(report['last_page'], 5213)
        self.assertEqual(report['sampled_pages']['5213']['rows'], 17)
        self.assertFalse(report['full_import'])
        self.assertEqual(len(requested), 3)

    def test_http_client_uses_compatible_project_identifier(self):
        response = MagicMock()
        response.url = SOURCE_URL
        response.headers.get.return_value = 'text/html'
        response.headers.get_content_charset.return_value = 'utf-8'
        response.read.return_value = RESULTS.encode()
        response.__enter__.return_value = response
        with patch('recy.source.urlopen', return_value=response) as opener:
            self.assertEqual(Scraper(delay=0)._fetch(SOURCE_URL), RESULTS)
            request = opener.call_args[0][0]
            self.assertEqual(request.get_header('User-agent'), USER_AGENT)
            self.assertIn('RecyRegistry', USER_AGENT)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "registry.sqlite3")
        self.snapshot = Scraper(fetch=lambda url: RESULTS).scrape()

    def sync(self, stamp="2026-01-01T00:00:00+00:00", **kwargs):
        return self.db.sync(self.snapshot, stamp, **kwargs)

    def test_schema_is_exact_source_headers_and_values(self):
        self.sync()
        with self.db.connect() as conn:
            fields = conn.execute("PRAGMA table_info(registered_products)").fetchall()
            self.assertEqual([r["name"] for r in fields], self.snapshot.headers)
        result = self.db.verify("00-1234 567890")
        self.assertTrue(result["registered"])
        self.assertEqual(result["matches"][0]["UPC Code"], "001234567890")
        self.assertFalse(self.db.verify("999999999999")["registered"])
        self.assertFalse(self.db.verify("1234567890")["registered"])

    def test_reconciles_added_removed_and_is_idempotent(self):
        self.sync()
        report = self.sync("2026-01-02T00:00:00+00:00")
        self.assertEqual((report["added"], report["removed"]), (0, 0))
        self.snapshot.rows = [("009999999999", "New", "Water", "500 mL")]
        report = self.sync("2026-01-03T00:00:00+00:00", max_removal_fraction=1)
        self.assertEqual((report["added"], report["removed"], report["total"]), (1, 2, 1))
        self.assertFalse(self.db.verify("001234567890")["registered"])
        self.assertTrue(self.db.verify("009999999999")["registered"])

    def test_failed_sync_keeps_existing_data(self):
        self.sync()
        self.snapshot.rows = [("009999999999", "New", "Water", "500 mL")]
        with self.assertRaises(SourceError):
            self.sync("2026-01-02T00:00:00+00:00")
        self.assertTrue(self.db.verify("001234567890")["registered"])
        self.assertEqual(self.db.status()["total"], 2)

    def test_dry_run_and_stale_sync(self):
        self.sync(dry_run=True)
        self.assertIsNone(self.db.status())
        self.sync()
        with self.assertRaises(SourceError):
            self.sync("2025-01-01T00:00:00+00:00")

    def test_rejects_schema_drift(self):
        self.sync()
        self.snapshot.headers[1] = "Changed"
        with self.assertRaises(SourceError):
            self.sync("2026-01-02T00:00:00+00:00")
        self.assertTrue(self.db.verify("001234567890")["registered"])

    def test_normalization_rejects_malformed_codes(self):
        for value in ["", "123", "1234abc", "１２３４", "1" * 19, 1234]:
            with self.assertRaises(ValueError):
                normalize_upc(value)

    def test_api_ready_validation_and_lookup(self):
        server = create_server(self.db, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%s" % server.server_port
        def request(path):
            try:
                response = urlopen(base + path)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            self.assertEqual(request("/health")[0], 200)
            self.assertEqual(request("/ready")[0], 503)
            self.assertEqual(request("/api/v1/upcs/001234567890")[0], 503)
            self.sync()
            self.assertEqual(request("/ready")[0], 200)
            self.assertTrue(request("/api/v1/upcs/001234567890")[1]["registered"])
            self.assertFalse(request("/api/v1/upcs/999999999999")[1]["registered"])
            self.assertEqual(request("/api/v1/upcs/nope")[0], 400)
            self.assertEqual(request("/api/v1/schema")[1]["columns"], self.snapshot.headers)
            self.assertEqual(request("/missing")[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
