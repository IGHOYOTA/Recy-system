import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock

from recy.backend import get_database
from recy.scheduled import compact_page, fingerprint, run_scheduled
from recy.source import RegistryHTML, Scraper, SourceError, SOURCE_URL
from recy.wsgi import application
from tests.test_registry import FIXTURES, RESULTS


class WSGITests(unittest.TestCase):
    def request(self, path, token='', method='GET'):
        response = []
        body = b''.join(application({'PATH_INFO': path, 'REQUEST_METHOD': method,
                                     'HTTP_AUTHORIZATION': token, 'wsgi.input': io.BytesIO()},
                                    lambda status, headers: response.append(status)))
        return int(response[0].split()[0]), json.loads(body)

    @patch.dict(os.environ, {'RECY_API_KEY': 'test-key'})
    def test_authentication_and_health(self):
        self.assertEqual(self.request('/health')[0], 200)
        self.assertEqual(self.request('/api/v1/upcs/1234')[0], 401)
        self.assertEqual(self.request('/api/v1/upcs/1234', 'Bearer wrong')[0], 401)
        self.assertEqual(self.request('/health', method='POST')[0], 405)
        with patch('recy.wsgi.get_database') as backend:
            backend.return_value.verify.return_value = {'registered': True}
            self.assertTrue(self.request('/api/v1/upcs/1234', 'Bearer test-key')[1]['registered'])
            backend.return_value.verify.return_value = None
            self.assertEqual(self.request('/api/v1/upcs/1234', 'Bearer test-key')[0], 503)
            backend.return_value.verify.side_effect = ValueError('bad upc')
            self.assertEqual(self.request('/api/v1/upcs/abc', 'Bearer test-key')[0], 400)

    @patch.dict(os.environ, {'DATABASE_URL': '', 'RECY_REQUIRE_POSTGRES': '1'})
    def test_hosted_backend_never_falls_back_to_sqlite(self):
        with self.assertRaisesRegex(ValueError, 'DATABASE_URL'):
            get_database()


class MemoryCheckpoint:
    def __init__(self):
        self.generation = None
        self.pages = {}
        self.published = None

    def status(self):
        return None

    def checkpoint_start(self, generation):
        if self.generation != generation:
            self.pages.clear()
            self.generation = generation
        return '2026-10-25T00:00:00+00:00'

    def checkpoint_pages(self, generation):
        return dict(self.pages)

    def checkpoint_put(self, generation, url, html):
        self.pages[url] = html

    def checkpoint_clear(self, generation):
        self.pages.clear()

    def sync(self, snapshot, started_at, **kwargs):
        self.published = snapshot
        return {'total': len(snapshot.rows)}


class CheckpointTests(unittest.TestCase):
    def test_compact_real_page_preserves_values_and_pagination(self):
        html = (FIXTURES / 'live_last.html').read_text()
        compact = compact_page(html)
        original, stored = RegistryHTML(html), RegistryHTML(compact)
        self.assertEqual(original.products(), stored.products())
        self.assertEqual(original.result_counts, stored.result_counts)
        self.assertEqual(fingerprint(html), fingerprint(compact))
        self.assertTrue(any('disabled' in rel for _, rel, label in stored.links if label == 'Next'))

    def test_resume_after_budget_never_publishes_partial_snapshot(self):
        base = RESULTS.replace('<p>Total products: 2</p>',
                               '<p title="6 products">Database Last Updated October 23, 2026</p>')
        first = base + '<a href="?Rn=2">2</a><a href="?Rn=3">3</a>'
        second = base.replace('Example', 'Second') + '<a href="?Rn=3">3</a>'
        third = base.replace('Example', 'Third')
        clock = [0]
        requested = []
        def fetch(self, url):
            requested.append(url)
            if 'Rn=2' in url:
                clock[0] = 2
                return second
            return third if 'Rn=3' in url else first
        database = MemoryCheckpoint()
        with patch.object(Scraper, '_fetch', fetch), patch('recy.scheduled.time.monotonic', side_effect=lambda: clock[0]):
            report = run_scheduled(database, max_seconds=1)
            self.assertEqual(report['status'], 'checkpointed')
            self.assertIsNone(database.published)
            self.assertEqual(len(database.pages), 1)
            clock[0] = 0
            requested.clear()
            report = run_scheduled(database, max_seconds=10)
            self.assertEqual(report['status'], 'published')
            self.assertEqual(report['total'], 6)
            self.assertFalse(any('Rn=2' in url for url in requested))
            self.assertEqual(database.pages, {})

    def test_fresh_registry_skips_network(self):
        from recy.database import now
        database = MagicMock()
        database.status.return_value = {'completed_at': now()}
        with patch.object(Scraper, '_fetch') as fetch:
            self.assertEqual(run_scheduled(database)['status'], 'not_due')
            fetch.assert_not_called()

    def test_source_generation_change_discards_cached_pages(self):
        html = (FIXTURES / 'live_first.html').read_text()
        checkpoint = MemoryCheckpoint()
        checkpoint.checkpoint_start(fingerprint(html))
        checkpoint.pages['old'] = 'old'
        checkpoint.checkpoint_start(fingerprint(html.replace('October 23', 'October 24')))
        self.assertEqual(checkpoint.pages, {})
