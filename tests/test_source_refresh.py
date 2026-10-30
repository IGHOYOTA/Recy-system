import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from recy.refresh import check_source_update, imported_date, next_midnight, run_due_sync, source_date
from recy.source import Scraper, SourceError
from tests import test_postgres as postgres
from tests.test_registry import RESULTS

UTC=timezone.utc
NOW=datetime.now(UTC)


def page(day=27):
    return RESULTS.replace('<p>Total products: 2</p>',
        '<p title="2 products">Database Last Updated October %s, 2026</p>' % day)


class SourceDateTests(unittest.TestCase):
    def test_midnight_is_next_local_day_and_observes_dst(self):
        self.assertEqual(next_midnight(datetime(2026,1,15,20,tzinfo=UTC)),datetime(2026,1,16,8,tzinfo=UTC))
        self.assertEqual(next_midnight(datetime(2026,7,15,20,tzinfo=UTC)),datetime(2026,7,16,7,tzinfo=UTC))
        self.assertEqual(next_midnight(datetime(2026,3,8,20,tzinfo=UTC)),datetime(2026,3,9,7,tzinfo=UTC))
        self.assertEqual(next_midnight(datetime(2025,11,2,20,tzinfo=UTC)),datetime(2025,11,3,8,tzinfo=UTC))
        self.assertEqual(next_midnight(datetime(2026,11,1,20,tzinfo=UTC)),datetime(2026,11,2,7,tzinfo=UTC))
        self.assertEqual(next_midnight(datetime(2026,1,16,8,tzinfo=UTC)),datetime(2026,1,17,8,tzinfo=UTC))

    def test_missing_invalid_and_future_dates_fail_closed(self):
        for html in (RESULTS,page(40),page(29)):
            with self.assertRaises(SourceError):source_date(html,datetime(2026,10,28,20,tzinfo=UTC))

    def test_legacy_baseline_uses_start_not_completion_date(self):
        row={'started_at':'2026-10-24T06:00:00+00:00','completed_at':'2026-10-27T00:00:00+00:00'}
        self.assertEqual(imported_date(row),date(2026,10,23))
        row['source_updated_on']='2026-10-20'
        self.assertEqual(imported_date(row),date(2026,10,20))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class SourceRefreshTests(unittest.TestCase):
    def setUp(self):
        postgres.PostgresTests.setUp(self)
        self.db.initialize_source_monitor()
        self.db.sync(self.snapshot,'2026-01-01T00:00:00+00:00',source_updated_on=date(2026,10,26))

    def check(self, day=27, now=NOW):
        with patch.object(Scraper,'_fetch',return_value=page(day)) as fetch:
            result=check_source_update(self.db,now=now)
        self.assertEqual(fetch.call_count,1)
        return result

    def test_daily_check_queues_midnight_and_never_moves_deadline(self):
        first=self.check();self.assertEqual(first['status'],'queued')
        self.assertEqual(datetime.fromisoformat(first['due_at']),next_midnight(NOW))
        again=self.check(now=NOW+timedelta(hours=2))
        self.assertEqual(again['status'],'already_queued');self.assertEqual(again['due_at'],first['due_at'])
        self.assertEqual(self.db.status()['source_updated_on'],'2026-10-26')

    def test_unchanged_and_older_dates_do_not_queue_even_when_import_is_old(self):
        self.assertEqual(self.check(26)['status'],'unchanged')
        self.assertEqual(self.check(25)['status'],'older_source_date')
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM recy.source_refresh_jobs').fetchone()['n'],0)

    def test_no_due_job_performs_no_http(self):
        self.check()
        with patch.object(Scraper,'_fetch') as fetch:
            result=run_due_sync(self.db,now=NOW)
        self.assertEqual(result['status'],'not_due');fetch.assert_not_called()

    def test_midnight_publication_records_date_and_prevents_another_scrape(self):
        self.check()
        with patch.object(Scraper,'_fetch',return_value=page()) as fetch:
            result=run_due_sync(self.db,delay=0,now=next_midnight(NOW))
        self.assertEqual(result['status'],'published')
        self.assertEqual(self.db.status()['source_updated_on'],'2026-10-27')
        with self.db.connect() as c:
            job=c.execute('SELECT * FROM recy.source_refresh_jobs').fetchone()
            self.assertEqual(job['status'],'completed');self.assertIsNotNone(job['completed_sync_run_id'])
        with patch.object(Scraper,'_fetch') as fetch:
            self.assertEqual(run_due_sync(self.db,now=NOW+timedelta(days=10))['status'],'not_due')
            fetch.assert_not_called()
        self.assertEqual(self.check()['status'],'unchanged')

    def test_newer_release_at_midnight_gets_a_new_deadline(self):
        self.check()
        midnight=next_midnight(NOW)
        with patch.object(Scraper,'_fetch',return_value=page(28)) as fetch:
            result=run_due_sync(self.db,now=midnight)
        self.assertEqual(result['status'],'superseded');self.assertEqual(fetch.call_count,1)
        with self.db.connect() as c:
            jobs=c.execute('SELECT * FROM recy.source_refresh_jobs ORDER BY id').fetchall()
            self.assertEqual(jobs[0]['status'],'superseded')
            self.assertEqual(jobs[1]['due_at'],next_midnight(midnight))
        self.assertEqual(self.db.status()['source_updated_on'],'2026-10-26')

    def test_site_rollback_never_completes_a_newer_pending_job(self):
        self.check()
        with patch.object(Scraper,'_fetch',return_value=page(26)):
            with self.assertRaisesRegex(SourceError,'backwards'):
                run_due_sync(self.db,now=NOW+timedelta(days=1))
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM recy.source_refresh_jobs').fetchone()
            self.assertEqual(row['status'],'running');self.assertIsNone(row['lease_owner'])
        self.assertEqual(self.check()['status'],'already_queued')

    def test_failed_fetch_keeps_prior_check_and_job_retryable(self):
        self.check()
        with patch.object(Scraper,'_fetch',side_effect=SourceError('blocked')):
            with self.assertRaises(SourceError):check_source_update(self.db,now=NOW+timedelta(days=1))
            with self.assertRaises(SourceError):run_due_sync(self.db,now=NOW+timedelta(days=1))
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT last_checked_at FROM recy.source_monitor').fetchone()['last_checked_at'],NOW)
            row=c.execute('SELECT * FROM recy.source_refresh_jobs').fetchone()
            self.assertEqual(row['last_error_code'],'SourceError');self.assertIsNone(row['lease_owner'])

    def test_budget_resume_and_dry_run_do_not_mark_imported(self):
        self.check()
        for result in ({'status':'checkpointed'},{'status':'dry_run'}):
            with patch.object(Scraper,'_fetch',return_value=page()),patch('recy.refresh.run_scheduled',return_value=result):
                self.assertEqual(run_due_sync(self.db,now=NOW+timedelta(days=1))['status'],result['status'])
            self.assertEqual(self.db.status()['source_updated_on'],'2026-10-26')
        with self.db.connect() as c:self.assertEqual(c.execute('SELECT status FROM recy.source_refresh_jobs').fetchone()['status'],'running')

    def test_lease_expiry_and_owner_fencing_prevent_overlapping_workers(self):
        self.check();now=NOW+timedelta(days=1);one,two=uuid4(),uuid4()
        job=self.db.claim_source_refresh(now,one,60)
        self.assertIsNone(self.db.claim_source_refresh(now,two,60))
        same=self.db.claim_source_refresh(now+timedelta(seconds=61),two,60)
        self.assertEqual(job['id'],same['id'])
        self.db.finish_source_refresh(job['id'],one,'completed')
        with self.db.connect() as c:self.assertEqual(c.execute('SELECT lease_owner FROM recy.source_refresh_jobs').fetchone()['lease_owner'],two)
        with self.assertRaisesRegex(SourceError,'lease'):
            self.db.sync(self.snapshot,'2026-02-01T00:00:00+00:00',source_updated_on=date(2026,10,27),refresh_lease=(job['id'],one))
        self.assertEqual(self.db.status()['source_updated_on'],'2026-10-26')

    def test_crash_after_publication_is_recovered_without_another_import(self):
        self.check();now=NOW+timedelta(days=1)
        self.db.sync(self.snapshot,'2026-02-01T00:00:00+00:00',source_updated_on=date(2026,10,27))
        with patch.object(Scraper,'_fetch',return_value=page()) as fetch:
            result=run_due_sync(self.db,now=now)
        self.assertEqual(result['status'],'already_current');self.assertEqual(fetch.call_count,1)
        with self.db.connect() as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM recy.sync_runs').fetchone()['n'],2)

    def test_source_only_setup_is_repeatable_and_does_not_create_application_tables(self):
        self.assertEqual(self.db.initialize_source_monitor()['applied'],[])
        with self.db.connect() as c:
            versions=[r['version'] for r in c.execute('SELECT version FROM recy.schema_migrations')]
            self.assertEqual(sorted(versions),['005_source_refresh_schedule.sql','006_source_content_fallback.sql'])
            self.assertTrue(c.execute("SELECT relrowsecurity FROM pg_class WHERE oid='recy.source_monitor'::regclass").fetchone()['relrowsecurity'])


def undated_pages(headers, base_row, final_date=None):
    from html import escape
    from recy.source import upc_column
    seed=Scraper(fetch=lambda _:RESULTS).scrape().rows
    def build(start,end):
        text='<table><tr>'+''.join('<th>'+escape(v)+'</th>' for v in headers)+'</tr>'
        for n in range(start,end+1):
            row=list(seed[n-1] if n<=len(seed) else base_row)
            if n>len(seed):row[headers.index(upc_column(headers))]=str(100000000000+n)
            text+='<tr>'+''.join('<td>'+escape(v)+'</td>' for v in row)+'</tr>'
        return text+'</table><a href="?Rn=2" title="31 to 37">2</a>'
    first=build(1,30)+'<a href="?Rn=2" rel="next">Next</a>'
    last=build(31,37)+'<a href="?Rn=2" rel="next disabled">Next</a>'
    if final_date:last+='<p title="37 products">Database Last Updated '+final_date+'</p>'
    return first,last


class ContentMetadataTests(unittest.TestCase):
    def test_captured_first_and_last_pages_resolve_current_live_metadata(self):
        from recy.scheduled import read_source_page,source_version
        from recy.source import RegistryHTML,SOURCE_URL
        from tests.test_registry import FIXTURES
        first=(FIXTURES/'live_undated_first.html').read_text()
        last=(FIXTURES/'live_metadata_last.html').read_text()
        html=read_source_page(lambda url:first if url==SOURCE_URL else last)
        self.assertEqual(source_version(RegistryHTML(html)),('October 28, 2026',156407))

    def test_real_undated_page_count_and_checkpoint_roundtrip(self):
        from recy.scheduled import source_version,fingerprint,compact_page
        from recy.source import RegistryHTML
        from tests.test_registry import FIXTURES
        html=(FIXTURES/'live_undated_first.html').read_text()
        self.assertEqual(source_version(RegistryHTML(html)),(None,156407))
        self.assertEqual(fingerprint(html),fingerprint(compact_page(html)))

    def test_final_page_resolves_real_date_without_inventing_one(self):
        from recy.scheduled import read_source_page,source_version
        from recy.source import RegistryHTML
        from tests.test_registry import Scraper as Parser
        snapshot=Parser(fetch=lambda _:RESULTS).scrape()
        first,last=undated_pages(snapshot.headers,snapshot.rows[0],'October 28, 2026')
        def fetch(url):return last if 'Rn=2' in url else first
        resolved=read_source_page(fetch)
        self.assertEqual(source_version(RegistryHTML(resolved)),('October 28, 2026',37))
        _,last=undated_pages(snapshot.headers,snapshot.rows[0])
        self.assertEqual(source_version(RegistryHTML(read_source_page(fetch))),(None,37))

    def test_range_titles_must_match_page_arithmetic(self):
        from recy.source import RegistryHTML
        from recy.scheduled import source_version
        html=RESULTS.replace('<p>Total products: 2</p>','')+'<a href="?Rn=2" title="9 to 12">2</a>'
        with self.assertRaises(SourceError):source_version(RegistryHTML(html))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'Disposable PostgreSQL required')
class ContentRefreshTests(unittest.TestCase):
    setUp=SourceRefreshTests.setUp

    def pages(self, final_date=None):return undated_pages(self.snapshot.headers,self.snapshot.rows[0],final_date)

    def test_due_dated_job_can_finish_when_date_disappears(self):
        from recy.scheduled import content_fingerprint
        self.db.record_source_observation(date(2026,10,27),NOW,next_midnight(NOW),'America/Vancouver')
        first,last=self.pages()
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            result=run_due_sync(self.db,delay=0,now=next_midnight(NOW))
        self.assertEqual(result['status'],'published');self.assertEqual(result['detection_method'],'content')
        latest=self.db.status();self.assertIsNone(latest['source_updated_on'])
        self.assertEqual(latest['source_fingerprint'],content_fingerprint(first));self.assertEqual(latest['total'],37)
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            self.assertEqual(check_source_update(self.db,now=NOW+timedelta(days=1))['status'],'unchanged')

    def test_content_changes_queue_only_next_midnight_and_keep_deadline(self):
        first,last=self.pages()
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            job=check_source_update(self.db,now=NOW)
            again=check_source_update(self.db,now=NOW+timedelta(minutes=5))
        self.assertIsNone(job['source_updated_on']);self.assertEqual(job['status'],'queued')
        self.assertEqual(again['status'],'already_queued');self.assertEqual(job['due_at'],again['due_at'])
        with patch.object(Scraper,'_fetch') as fetch:
            self.assertEqual(run_due_sync(self.db,now=NOW)['status'],'not_due');fetch.assert_not_called()

    def test_content_changes_before_execution_supersede_old_job(self):
        first,last=self.pages()
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            check_source_update(self.db,now=NOW)
        first=first.replace('Example','Changed');last=last.replace('Example','Changed')
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            result=run_due_sync(self.db,now=next_midnight(NOW))
        self.assertEqual(result['status'],'superseded')
        with self.db.connect() as c:
            rows=c.execute('SELECT status,due_at FROM recy.source_refresh_jobs ORDER BY id').fetchall()
            self.assertEqual(rows[0]['status'],'superseded');self.assertEqual(rows[1]['due_at'],next_midnight(next_midnight(NOW)))

    def test_date_on_last_page_takes_priority_and_keeps_midnight_gate(self):
        self.db.record_source_observation(date(2026,10,27),NOW,next_midnight(NOW),'America/Vancouver')
        first,last=self.pages('October 28, 2026')
        with patch.object(Scraper,'_fetch',side_effect=lambda url:last if 'Rn=2' in url else first):
            result=run_due_sync(self.db,now=next_midnight(NOW))
        self.assertEqual(result['status'],'superseded')
        with self.db.connect() as c:
            latest=c.execute('SELECT source_updated_on,status FROM recy.source_refresh_jobs ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(latest['source_updated_on'],date(2026,10,28));self.assertEqual(latest['status'],'pending')
