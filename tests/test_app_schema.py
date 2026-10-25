"""Application migration tests: only the disposable recy_test database is touched."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlsplit
from uuid import uuid4


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class ApplicationSchemaTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from recy.postgres import PostgresDatabase
        self.pg = psycopg
        url = os.environ['TEST_DATABASE_URL']
        if urlsplit(url).path != '/recy_test':
            raise RuntimeError('Refusing to reset any database except recy_test')
        self.db = PostgresDatabase(url)
        with self.db.connect() as c:
            c.execute('DROP SCHEMA IF EXISTS app CASCADE')
            c.execute('DROP SCHEMA IF EXISTS recy CASCADE')
            # Test fixture only. The production migration never creates/changes Auth.
            c.execute('CREATE SCHEMA IF NOT EXISTS auth')
            c.execute('CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY)')
            c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF; END $$")
        self.db.migrate()
        with self.db.connect() as c:
            self.user = c.execute("INSERT INTO app.profiles(first_name,last_name) VALUES ('Test','User') RETURNING id").fetchone()['id']
            self.staff = c.execute("INSERT INTO app.profiles(first_name,last_name) VALUES ('Test','Staff') RETURNING id").fetchone()['id']
            c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'payout_processor',%s)", (self.staff,self.staff))
            c.execute("INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,'administrator',%s)", (self.staff,self.staff))
            self.wallet,self.available,self.reserved = uuid4(),uuid4(),uuid4()
            c.execute('INSERT INTO app.wallets(id,user_id,available_account_id,reserved_account_id) VALUES (%s,%s,%s,%s)',
                      (self.wallet,self.user,self.available,self.reserved))
            c.execute("INSERT INTO app.ledger_accounts(id,wallet_id,purpose) VALUES (%s,%s,'user_available'),(%s,%s,'user_reserved')",
                      (self.available,self.wallet,self.reserved,self.wallet))
            self.platform = c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_rewards'").fetchone()['id']
            self.clearing = c.execute("SELECT id FROM app.ledger_accounts WHERE purpose='platform_payout_clearing'").fetchone()['id']
            self.machine = c.execute("INSERT INTO app.machines(machine_code,name,status,operating_mode) VALUES ('TEST-01','Test','active','live') RETURNING id").fetchone()['id']
            self.rate = c.execute('SELECT id FROM app.reward_rates').fetchone()['id']
            adjustment=c.execute("INSERT INTO app.wallet_adjustments(wallet_id,amount_cents,reason_code,requested_by,approved_by,approved_at,status) VALUES (%s,700,'test_funding',%s,%s,now(),'approved') RETURNING id",
                                 (self.wallet,self.staff,self.staff)).fetchone()['id']
            self.initial_tx=self.post(c,'adjustment','test-funding',[(self.available,700),(self.platform,-700)])
            c.execute("UPDATE app.wallet_adjustments SET status='posted',posted_at=now(),ledger_transaction_id=%s WHERE id=%s", (self.initial_tx,adjustment))

    def post(self,c,kind,key,entries,session=None,payout=None):
        payload=json.dumps([{'account_id':str(a),'amount_cents':v} for a,v in entries])
        return c.execute('SELECT app.post_journal(%s,%s,%s::jsonb,%s,%s) AS id',
                         (kind,key,payload,session,payout)).fetchone()['id']

    def balance(self):
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM app.wallet_balances WHERE wallet_id=%s',(self.wallet,)).fetchone()
            return int(row['available_cents']),int(row['reserved_cents'])

    def tearDown(self):
        with self.db.connect() as c:
            c.execute('DROP SCHEMA IF EXISTS app CASCADE')

    def request_payout(self,c,amount=500,key=None):
        key=key or str(uuid4())
        payout=c.execute("INSERT INTO app.payout_requests(user_id,wallet_id,destination_email,amount_cents,request_idempotency_key,request_hash) VALUES (%s,%s,'test@example.com',%s,%s,'hash') RETURNING id",
                         (self.user,self.wallet,amount,key)).fetchone()['id']
        tx=self.post(c,'payout_reserve','reserve:'+key,[(self.reserved,amount),(self.available,-amount)],payout=payout)
        c.execute('UPDATE app.payout_requests SET reserve_transaction_id=%s WHERE id=%s',(tx,payout))
        return payout

    def start_session(self,c):
        login=c.execute('INSERT INTO app.machine_logins(user_id,machine_id,supabase_auth_session_id) VALUES (%s,%s,%s) RETURNING id',
                        (self.user,self.machine,uuid4())).fetchone()['id']
        return c.execute('INSERT INTO app.collection_sessions(user_id,machine_id,machine_login_id,reward_rate_id) VALUES (%s,%s,%s,%s) RETURNING id',
                         (self.user,self.machine,login,self.rate)).fetchone()['id']

    def test_migration_is_repeatable_and_keeps_registry_checkpoints(self):
        with self.db.connect() as c:
            c.execute("INSERT INTO recy.scrape_state VALUES (1,'existing','2026-10-25T00:00:00+00:00')")
            c.execute("INSERT INTO recy.scrape_pages VALUES ('page2516','saved')")
        self.assertEqual(self.db.migrate()['applied'],[])
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM app.reward_rates').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT html FROM recy.scrape_pages').fetchone()['html'],'saved')
            self.assertEqual(c.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='app'").fetchone()['n'],30)

    def test_migration_plan_and_checksum_protect_applied_history(self):
        from recy.source import SourceError
        self.assertEqual(self.db.migrate(plan=True)['pending'],[])
        with self.db.connect() as c:
            c.execute("UPDATE recy.schema_migrations SET checksum='invalid' WHERE version='002_application_schema.sql'")
        with self.assertRaisesRegex(SourceError,'checksum mismatch'):
            self.db.migrate()
        self.assertEqual(self.balance(),(700,0))

    def test_private_tables_have_rls_and_views_are_invoker(self):
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace WHERE n.nspname='app' AND x.relkind='r' AND NOT x.relrowsecurity").fetchone()['n'],0)
            self.assertFalse(c.execute("SELECT has_schema_privilege('anon','app','USAGE') AS allowed").fetchone()['allowed'])
            self.assertFalse(c.execute("SELECT has_function_privilege('anon','app.post_journal(text,text,jsonb,uuid,uuid,uuid)','EXECUTE') AS allowed").fetchone()['allowed'])
            self.assertEqual(c.execute("SELECT count(*) AS n FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace WHERE n.nspname='app' AND x.relkind='v' AND NOT ('security_invoker=true'=ANY(x.reloptions))").fetchone()['n'],0)

    def test_minimum_no_fee_partial_and_full_payout(self):
        for amount in (0,499):
            with self.assertRaises(self.pg.errors.CheckViolation):
                with self.db.connect() as c:self.request_payout(c,amount)
        with self.db.connect() as c:p=self.request_payout(c,500)
        self.assertEqual(self.balance(),(200,500))
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM app.payout_requests WHERE id=%s',(p,)).fetchone()
            self.assertEqual(row['fee_cents'],0)
            self.assertEqual(row['wallet_debit_cents'],row['recipient_amount_cents'])
            self.assertEqual((row['processing_due_at']-row['requested_at']).total_seconds(),86400)
        # A separate fresh wallet can withdraw its entire $7; no leftover minimum.
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:self.request_payout(c,500)

    def test_full_balance_payout(self):
        with self.db.connect() as c:self.request_payout(c,700)
        self.assertEqual(self.balance(),(0,700))

    def test_reservation_must_commit_with_payout(self):
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:
                c.execute("INSERT INTO app.payout_requests(user_id,wallet_id,destination_email,amount_cents,request_idempotency_key,request_hash) VALUES (%s,%s,'test@example.com',500,'missing-reserve','hash')",(self.user,self.wallet))
        self.assertEqual(self.balance(),(700,0))

    def test_payment_completion_does_not_deduct_twice(self):
        with self.db.connect() as c:p=self.request_payout(c)
        with self.db.connect() as c:
            c.execute("UPDATE app.payout_requests SET status='processing',assigned_to=%s,processing_started_at=now() WHERE id=%s",(self.staff,p))
        with self.db.connect() as c:
            tx=self.post(c,'payout_settle','settle',[(self.clearing,500),(self.reserved,-500)],payout=p)
            c.execute("UPDATE app.payout_requests SET status='paid',processed_by=%s,payment_reference='BANK-TEST',paid_at=now(),completed_at=now(),settle_transaction_id=%s WHERE id=%s",(self.staff,tx,p))
        self.assertEqual(self.balance(),(200,0))
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:c.execute("UPDATE app.payout_requests SET status='cancelled' WHERE id=%s",(p,))

    def test_cancel_restores_available_once(self):
        with self.db.connect() as c:p=self.request_payout(c)
        with self.db.connect() as c:
            tx=self.post(c,'payout_release','release',[(self.available,500),(self.reserved,-500)],payout=p)
            c.execute("UPDATE app.payout_requests SET status='cancelled',completed_at=now(),release_transaction_id=%s WHERE id=%s",(tx,p))
        self.assertEqual(self.balance(),(700,0))
        with self.assertRaises(self.pg.errors.UniqueViolation):
            with self.db.connect() as c:self.post(c,'payout_release','duplicate-release',[(self.available,500),(self.reserved,-500)],payout=p)

    def test_concurrent_payouts_cannot_overspend(self):
        barrier=Barrier(2)
        def request():
            barrier.wait()
            try:
                with self.db.connect() as c:self.request_payout(c)
                return 'ok'
            except self.pg.errors.CheckViolation:return 'insufficient'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:request(),range(2)))
        self.assertCountEqual(outcomes,['ok','insufficient'])
        self.assertEqual(self.balance(),(200,500))

    def test_ledger_immutable_and_idempotent(self):
        with self.db.connect() as c:
            tx=self.post(c,'adjustment','test-funding',[(self.available,700),(self.platform,-700)])
            self.assertEqual(tx,self.initial_tx)
        self.assertEqual(self.balance(),(700,0))
        for query in ('DELETE FROM app.ledger_entries','UPDATE app.ledger_entries SET amount_cents=999','TRUNCATE app.ledger_entries'):
            with self.assertRaises(self.pg.errors.CheckViolation):
                with self.db.connect() as c:c.execute(query)
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:
                c.execute('INSERT INTO app.ledger_entries(transaction_id,account_id,amount_cents) VALUES (%s,%s,1)',(self.initial_tx,self.clearing))

    def test_unbalanced_journal_rolls_back(self):
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:self.post(c,'adjustment','unbalanced',[(self.available,1),(self.platform,-2)])
        self.assertEqual(self.balance(),(700,0))

    def test_wrong_wallet_owner_rejected(self):
        with self.assertRaises(self.pg.errors.ForeignKeyViolation):
            with self.db.connect() as c:
                c.execute("INSERT INTO app.payout_requests(user_id,wallet_id,destination_email,amount_cents,request_idempotency_key,request_hash) VALUES (%s,%s,'test@example.com',500,'wrong-owner','hash')",(self.staff,self.wallet))

    def test_only_submission_ends_session_and_schedules_logout(self):
        with self.db.connect() as c:s=self.start_session(c)
        with self.db.connect() as c:
            # No database timeout closes the session, however old its last scan is.
            c.execute("UPDATE app.collection_sessions SET last_scan_at=now()-interval '1 hour' WHERE id=%s",(s,))
            self.assertEqual(c.execute('SELECT collection_status FROM app.collection_sessions WHERE id=%s',(s,)).fetchone()['collection_status'],'active')
            c.execute("INSERT INTO app.session_submissions(session_id,machine_id,idempotency_key,request_hash,claimed_accepted_count,claimed_rejected_count,final_sequence_no,machine_end_event_id,end_reason,expected_attempt_count,manifest_hash) VALUES (%s,%s,'end','hash',0,0,0,%s,'inactivity',0,'manifest')",(s,self.machine,uuid4()))
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM app.collection_sessions WHERE id=%s',(s,)).fetchone()
            self.assertEqual(row['collection_status'],'submitted')
            self.assertEqual((row['credit_due_at']-row['ended_at']).total_seconds(),60)
            login=c.execute('SELECT * FROM app.machine_logins WHERE id=%s',(row['machine_login_id'],)).fetchone()
            self.assertEqual(login['logout_status'],'revocation_pending')
            self.assertIsNotNone(login['backend_closed_at'])
            self.assertEqual(c.execute('SELECT count(*) AS n FROM app.work_jobs').fetchone()['n'],2)

    def test_test_machine_cannot_create_creditable_session(self):
        with self.db.connect() as c:c.execute("UPDATE app.machines SET operating_mode='test' WHERE id=%s",(self.machine,))
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:self.start_session(c)

    def test_empty_session_finalizes_only_after_validated_submission(self):
        with self.db.connect() as c:s=self.start_session(c)
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:
                c.execute("UPDATE app.collection_sessions SET reconciliation_status='no_credit',accepted_count=0,rejected_count=0,reward_total_cents=0,reconciled_at=now() WHERE id=%s",(s,))
        with self.db.connect() as c:
            c.execute("INSERT INTO app.session_submissions(session_id,machine_id,idempotency_key,request_hash,claimed_accepted_count,claimed_rejected_count,final_sequence_no,machine_end_event_id,end_reason,expected_attempt_count,manifest_hash,status,validated_at) VALUES (%s,%s,'empty','hash',0,0,0,%s,'inactivity',0,'manifest','validated',now())",(s,self.machine,uuid4()))
            c.execute("UPDATE app.collection_sessions SET reconciliation_status='no_credit',accepted_count=0,rejected_count=0,reward_total_cents=0,reconciled_at=now() WHERE id=%s",(s,))
        self.assertEqual(self.balance(),(700,0))

    def test_no_new_bottle_can_be_invented_after_submission(self):
        with self.db.connect() as c:
            s=self.start_session(c)
            c.execute("INSERT INTO app.session_submissions(session_id,machine_id,idempotency_key,request_hash,claimed_accepted_count,claimed_rejected_count,final_sequence_no,machine_end_event_id,end_reason,expected_attempt_count,manifest_hash) VALUES (%s,%s,'end','hash',0,0,0,%s,'inactivity',0,'manifest')",(s,self.machine,uuid4()))
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:
                c.execute("INSERT INTO app.bottle_events(machine_event_id,session_id,user_id,machine_id,sequence_no,upc,eligibility_status,status,reason_code,observed_at,payload_hash) VALUES (%s,%s,%s,%s,1,'001234567890','ineligible','rejected','not_registered',now(),'hash')",
                          (uuid4(),s,self.user,self.machine))

    def test_captured_bottles_credit_seven_cents_each_with_repeated_upc(self):
        with self.db.connect() as c:
            s=self.start_session(c)
            sync=c.execute("INSERT INTO recy.sync_runs(started_at,completed_at,source_url,pages,added,removed,total,headers) VALUES ('2026-10-25T00:00:00+00:00','2026-10-25T00:00:01+00:00','test',1,1,0,1,'[\"UPC\"]') RETURNING id").fetchone()['id']
            event_ids=[uuid4(),uuid4()]
            for seq,event in enumerate(event_ids,1):
                c.execute("""INSERT INTO app.bottle_events(machine_event_id,session_id,user_id,machine_id,sequence_no,upc,
                    eligibility_status,status,reason_code,registry_sync_run_id,capture_event_id,captured_at,
                    observed_at,decided_at,reward_cents,payload_hash)
                    VALUES (%s,%s,%s,%s,%s,'001234567890','eligible','accepted','registered',%s,%s,now(),now(),now(),7,'test')""",
                          (event,s,self.user,self.machine,seq,sync,uuid4()))
            c.execute("""INSERT INTO app.session_submissions(session_id,machine_id,idempotency_key,request_hash,
                claimed_accepted_count,claimed_rejected_count,final_sequence_no,machine_end_event_id,end_reason,
                expected_attempt_count,manifest_hash,sealed_machine_event_ids,status,validated_at)
                VALUES (%s,%s,'two-bottles','hash',2,0,2,%s,'inactivity',2,'manifest',%s,'validated',now())""",
                      (s,self.machine,uuid4(),event_ids))
            tx=self.post(c,'session_credit','session:'+str(s),[(self.available,14),(self.platform,-14)],session=s)
            c.execute("""UPDATE app.collection_sessions SET reconciliation_status='posted',accepted_count=2,
                rejected_count=0,reward_total_cents=14,credit_transaction_id=%s,credit_posted_at=now(),
                reconciled_at=now() WHERE id=%s""",(tx,s))
        self.assertEqual(self.balance(),(714,0))
        with self.assertRaises(self.pg.errors.UniqueViolation):
            with self.db.connect() as c:
                self.post(c,'session_credit','duplicate-credit',[(self.available,14),(self.platform,-14)],session=s)
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:c.execute("UPDATE app.bottle_events SET status='rejected',reward_cents=0 WHERE session_id=%s",(s,))

    def test_reward_rate_and_period_are_protected(self):
        with self.assertRaises(self.pg.errors.CheckViolation):
            with self.db.connect() as c:c.execute('UPDATE app.reward_rates SET amount_per_bottle_cents=10')
        with self.assertRaises(self.pg.errors.ExclusionViolation):
            with self.db.connect() as c:c.execute("INSERT INTO app.reward_rates(version,amount_per_bottle_cents,effective_from) VALUES ('overlap',7,now())")
