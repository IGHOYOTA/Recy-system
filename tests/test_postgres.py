"""Run only against the disposable database named recy_test."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from tests import test_registry as contract


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to a disposable recy_test database')
class PostgresTests(contract.DatabaseTests):
    def setUp(self):
        from recy.postgres import PostgresDatabase
        url = os.environ['TEST_DATABASE_URL']
        if urlparse(url).path != '/recy_test':
            raise RuntimeError('Refusing to reset any database except recy_test')
        self.db = PostgresDatabase(url)
        with self.db.connect() as conn:
            conn.execute('DROP SCHEMA IF EXISTS recy CASCADE')
        self.db.initialize()
        self.snapshot = contract.Scraper(fetch=lambda url: contract.RESULTS).scrape()

    def test_schema_is_exact_source_headers_and_values(self):
        self.sync()
        with self.db.connect() as conn:
            fields = conn.execute("SELECT column_name FROM information_schema.columns "
                                  "WHERE table_schema='recy' AND table_name='registered_products' "
                                  "ORDER BY ordinal_position").fetchall()
            self.assertEqual([r['column_name'] for r in fields], self.snapshot.headers)
        self.assertTrue(self.db.verify('00-1234 567890')['registered'])
        self.assertFalse(self.db.verify('1234567890')['registered'])

    def test_checkpoint_persists_and_resets_by_generation(self):
        started = self.db.checkpoint_start('one')
        self.db.checkpoint_put('one', 'page2', '<table>cached</table>')
        self.assertEqual(self.db.checkpoint_start('one'), started)
        self.assertEqual(self.db.checkpoint_pages('one'), {'page2': '<table>cached</table>'})
        self.db.checkpoint_start('two')
        self.assertEqual(self.db.checkpoint_pages('two'), {})
        with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            self.db.checkpoint_put('one', 'page3', 'stale')

    def test_migration_is_idempotent(self):
        self.db.initialize()
        self.sync()
        self.db.initialize()
        self.assertTrue(self.db.verify('001234567890')['registered'])

    def test_scheduled_scrape_publishes_and_cleans_checkpoint(self):
        self.db.initialize_source_monitor()
        from recy.scheduled import run_scheduled
        from recy.source import SOURCE_URL
        first = contract.RESULTS.replace('<p>Total products: 2</p>',
            '<p title="4 products">Database Last Updated October 23, 2026</p>')
        second = first.replace('Example', 'Second')
        first += '<a href="?Rn=2">2</a>'
        with patch.object(contract.Scraper, '_fetch', lambda self, url: first if url == SOURCE_URL else second):
            result = run_scheduled(self.db)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(self.db.status()['total'], 4)
        self.assertTrue(self.db.verify('001234567890')['registered'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM recy.scrape_pages').fetchone()['n'], 0)

    def test_display_renumbering_does_not_trigger_removal_guard(self):
        self.snapshot.headers.insert(0, '#')
        self.snapshot.rows = [(str(i + 1), *row) for i, row in enumerate(self.snapshot.rows)]
        self.sync()
        self.snapshot.rows = [(str(int(row[0]) + 1), *row[1:]) for row in self.snapshot.rows]
        self.snapshot.rows.append(('1', '009999999999', 'New', 'Water', '500 mL'))
        self.sync('2026-01-02T00:00:00+00:00')
        self.assertTrue(self.db.verify('001234567890')['registered'])

    def test_dry_run_first_sync_does_not_create_active_table(self):
        self.sync(dry_run=True)
        with self.db.connect() as conn:
            row = conn.execute("SELECT to_regclass('recy.registered_products') AS name").fetchone()
            self.assertIsNone(row['name'])
