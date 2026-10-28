"""Supabase/PostgreSQL backend. Schema initialization is an explicit CLI step."""
import json
import hashlib
from contextlib import contextmanager
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from .database import normalize_upc, now, quote
from .source import SourceError, upc_column

LOCK_ID = 72637901


class PostgresDatabase:
    def __init__(self, url):
        if not url.startswith(('postgresql://', 'postgres://')):
            raise ValueError('DATABASE_URL must contain only a PostgreSQL URL starting with '
                             'postgresql://; remove any duplicated DATABASE_URL= prefix')
        try:
            parsed = urlparse(url)
            if not parsed.hostname or (parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError('Invalid database host or port')
            conninfo_to_dict(url)
        except (psycopg.ProgrammingError, ValueError):
            raise ValueError('DATABASE_URL is malformed; URL-encode password characters '
                             'and check the Session pooler connection string') from None
        self.url = url

    @contextmanager
    def connect(self):
        # SSL by default for remote Supabase; local integration tests explicitly disable it.
        options = {} if 'sslmode' in parse_qs(urlparse(self.url).query) else {'sslmode': 'require'}
        db = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=15, **options)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        migration = files('recy').joinpath('migrations/001_initial.sql').read_text()
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            db.execute(migration)

    def migrate(self, plan=False):
        """Apply reviewed, checksummed migrations atomically; registry init stays separate."""
        migrations = sorted((p for p in files('recy').joinpath('migrations').iterdir()
                             if p.name.endswith('.sql')), key=lambda p: p.name)
        with self.connect() as db:
            db.execute("SET LOCAL lock_timeout = '10s'")
            db.execute("SET LOCAL statement_timeout = '90s'")
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID + 1,))
            exists = db.execute("SELECT to_regclass('recy.schema_migrations') AS name").fetchone()['name']
            applied = {r['version']: r['checksum'] for r in db.execute(
                'SELECT version,checksum FROM recy.schema_migrations')} if exists else {}
            pending = []
            for resource in migrations:
                sql = resource.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if resource.name in applied:
                    if applied[resource.name] != checksum:
                        raise SourceError('Applied migration checksum mismatch: ' + resource.name)
                else:
                    pending.append((resource.name, checksum, sql))
            if plan:
                return {'pending': [p[0] for p in pending], 'applied': sorted(applied)}
            if not db.execute("SELECT to_regclass('auth.users') AS name").fetchone()['name']:
                raise SourceError('Application migration requires Supabase auth.users (or the test-only Auth stub)')
            db.execute('CREATE SCHEMA IF NOT EXISTS recy')
            db.execute('''CREATE TABLE IF NOT EXISTS recy.schema_migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('REVOKE ALL ON recy.schema_migrations FROM PUBLIC')
            for version, checksum, sql in pending:
                db.execute(sql)
                db.execute('INSERT INTO recy.schema_migrations(version,checksum) VALUES (%s,%s)',
                           (version, checksum))
            return {'applied': [p[0] for p in pending]}

    @staticmethod
    def _latest(db):
        row = db.execute('SELECT * FROM recy.sync_runs ORDER BY id DESC LIMIT 1').fetchone()
        if row:
            row['headers'] = json.loads(row['headers'])
            if row.get('source_updated_on') is not None:
                row['source_updated_on'] = row['source_updated_on'].isoformat()
        return row

    def status(self):
        with self.connect() as db:
            return self._latest(db)

    def verify(self, raw_upc):
        code = normalize_upc(raw_upc)
        with self.connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            latest = self._latest(db)
            if not latest:
                return None
            column = upc_column(latest['headers'])
            rows = db.execute('SELECT * FROM recy.registered_products WHERE '
                              'recy.normalize_upc(%s) = %%s' % quote(column), (code,)).fetchall()
            return {'upc': code, 'registered': bool(rows), 'matches': rows,
                    'last_synced_at': latest['completed_at'], 'source_url': latest['source_url']}

    def sync(self, snapshot, started_at, max_removal_fraction=0.25, dry_run=False, source_updated_on=None, refresh_lease=None):
        headers = snapshot.headers
        if not 0 <= max_removal_fraction <= 1:
            raise ValueError('Removal fraction must be between 0 and 1')
        if (not headers or not all(isinstance(h, str) and h and len(h.encode()) <= 63 for h in headers)
                or len({h.casefold() for h in headers}) != len(headers)):
            raise SourceError('Invalid snapshot schema')
        column = upc_column(headers)
        if not snapshot.rows or any(len(r) != len(headers) for r in snapshot.rows):
            raise SourceError('Empty or malformed snapshot')
        incoming = sorted(set(tuple(r) for r in snapshot.rows))
        if not any(self._usable_upc(r[headers.index(column)]) for r in incoming):
            raise SourceError('Snapshot contains no usable UPCs')
        fields = ', '.join(quote(h) for h in headers)
        with self.connect() as db:
            if refresh_lease is not None:
                # Fence publication against a crashed worker whose lease was reclaimed.
                # Keep lock order identical to source observation: schedule, then registry.
                db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID+2,))
                valid=db.execute('''SELECT id FROM recy.source_refresh_jobs WHERE id=%s AND lease_owner=%s
                    AND lease_until>clock_timestamp() AND status='running' FOR UPDATE''',refresh_lease).fetchone()
                if not valid:raise SourceError('Source refresh lease expired or was replaced; publication rejected')
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            latest = self._latest(db)
            if latest and started_at <= latest['started_at']:
                raise SourceError('A newer sync already committed; discard this snapshot')
            if latest and headers != latest['headers']:
                raise SourceError('Source columns changed; review schema migration before syncing')
            if not latest:
                db.execute('CREATE TABLE recy.registered_products (%s)' %
                           ', '.join(quote(h) + ' TEXT NOT NULL' for h in headers))
                db.execute('CREATE INDEX registered_products_upc ON recy.registered_products '
                           '(recy.normalize_upc(%s))' % quote(column))
                db.execute('REVOKE ALL ON recy.registered_products FROM PUBLIC')
            db.execute('CREATE TEMP TABLE incoming (LIKE recy.registered_products) ON COMMIT DROP')
            with db.cursor().copy('COPY incoming (%s) FROM STDIN' % fields) as copy:
                for row in incoming:
                    copy.write_row(row)
            added = db.execute('SELECT count(*) AS n FROM (SELECT * FROM incoming EXCEPT '
                               'SELECT * FROM recy.registered_products) AS delta').fetchone()['n']
            removed = db.execute('SELECT count(*) AS n FROM (SELECT * FROM recy.registered_products '
                                 'EXCEPT SELECT * FROM incoming) AS delta').fetchone()['n']
            # '#' is a display position, so inserting a brand can renumber many rows.
            # Apply the deletion guard to product content, while still mirroring '#'.
            content = ', '.join(quote(h) for h in headers if h != '#')
            previous_products = db.execute('SELECT count(*) AS n FROM (SELECT DISTINCT %s '
                                           'FROM recy.registered_products) AS products' % content).fetchone()['n']
            removed_products = db.execute('SELECT count(*) AS n FROM (SELECT %s FROM '
                                          'recy.registered_products EXCEPT SELECT %s FROM incoming) '
                                          'AS products' % (content, content)).fetchone()['n']
            if previous_products and removed_products / previous_products > max_removal_fraction:
                raise SourceError('Removal threshold exceeded (%s/%s products)' % (removed_products, previous_products))
            report = dict(started_at=started_at, completed_at=now(), source_url=snapshot.source_url,
                          pages=snapshot.pages, added=added, removed=removed, total=len(incoming),
                          headers=headers, dry_run=dry_run)
            if dry_run:
                db.rollback()
                return report
            # DELETE/INSERT are transactional and preserve the table and its lookup index.
            db.execute('DELETE FROM recy.registered_products')
            db.execute('INSERT INTO recy.registered_products SELECT * FROM incoming')
            publication=db.execute('''INSERT INTO recy.sync_runs
                (started_at,completed_at,source_url,pages,added,removed,total,headers)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                       (started_at, report['completed_at'], snapshot.source_url, snapshot.pages,
                        added, removed, len(incoming), json.dumps(headers))).fetchone()
            if source_updated_on is not None:
                # Same transaction as publication: a crash cannot label an old snapshot current.
                db.execute('UPDATE recy.sync_runs SET source_updated_on=%s WHERE id=%s',
                           (source_updated_on,publication['id']))
                report['source_updated_on']=str(source_updated_on)
            return report

    def initialize_source_monitor(self):
        """Explicit source-only migration; never upgrade unrelated application tables."""
        name='005_source_refresh_schedule.sql'
        sql=files('recy').joinpath('migrations/'+name).read_text()
        checksum=hashlib.sha256(sql.encode()).hexdigest()
        with self.connect() as db:
            db.execute("SET LOCAL lock_timeout='10s'")
            db.execute("SET LOCAL statement_timeout='90s'")
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID+1,))
            db.execute('''CREATE TABLE IF NOT EXISTS recy.schema_migrations (
                version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('REVOKE ALL ON recy.schema_migrations FROM PUBLIC')
            old=db.execute('SELECT checksum FROM recy.schema_migrations WHERE version=%s',(name,)).fetchone()
            if old:
                if old['checksum']!=checksum:raise SourceError('Applied migration checksum mismatch: '+name)
                return {'applied':[]}
            db.execute(sql)
            db.execute('INSERT INTO recy.schema_migrations(version,checksum) VALUES (%s,%s)',(name,checksum))
            return {'applied':[name]}

    def record_source_observation(self, observed, checked_at, due_at, zone):
        from .refresh import imported_date
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID+2,))
            # Serialize with publications; no HTTP is performed while holding this lock.
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            baseline=imported_date(self._latest(db))
            db.execute('''INSERT INTO recy.source_monitor VALUES (1,%s,%s)
                ON CONFLICT(id) DO UPDATE SET last_checked_at=EXCLUDED.last_checked_at,
                observed_source_date=EXCLUDED.observed_source_date
                WHERE recy.source_monitor.last_checked_at<=EXCLUDED.last_checked_at''',(checked_at,observed))
            if baseline is not None and observed<=baseline:
                return {'status':'unchanged' if observed==baseline else 'older_source_date',
                        'source_updated_on':observed.isoformat(),'imported_date':baseline.isoformat()}
            job=db.execute('''INSERT INTO recy.source_refresh_jobs(source_updated_on,detected_at,due_at,timezone)
                VALUES (%s,%s,%s,%s) ON CONFLICT(source_updated_on) DO NOTHING RETURNING *''',
                (observed,checked_at,due_at,zone)).fetchone()
            queued=job is not None
            if job is None:job=db.execute('SELECT * FROM recy.source_refresh_jobs WHERE source_updated_on=%s',(observed,)).fetchone()
            return {'status':'queued' if queued else 'already_queued','source_updated_on':observed.isoformat(),
                    'due_at':job['due_at'].isoformat(),'timezone':job['timezone'],'job_id':job['id']}

    def claim_source_refresh(self, now_at, owner, lease_seconds):
        from datetime import timedelta
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID+2,))
            if db.execute("SELECT 1 FROM recy.source_refresh_jobs WHERE lease_until>%s",(now_at,)).fetchone():return None
            job=db.execute('''SELECT * FROM recy.source_refresh_jobs WHERE status IN ('pending','running')
                AND due_at<=%s AND (lease_until IS NULL OR lease_until<=%s)
                ORDER BY source_updated_on DESC FOR UPDATE SKIP LOCKED LIMIT 1''',(now_at,now_at)).fetchone()
            if job is None:return None
            return db.execute('''UPDATE recy.source_refresh_jobs SET status='running',attempts=attempts+1,
                lease_owner=%s,lease_until=%s,last_attempt_at=%s,last_error_code=NULL WHERE id=%s RETURNING *''',
                (owner,now_at+timedelta(seconds=lease_seconds),now_at,job['id'])).fetchone()

    def finish_source_refresh(self, job_id, owner, status, error=None):
        with self.connect() as db:
            db.execute('''UPDATE recy.source_refresh_jobs SET status=%s,lease_owner=NULL,lease_until=NULL,
                last_error_code=%s,completed_at=CASE WHEN %s IN ('completed','superseded') THEN now() ELSE NULL END,
                completed_sync_run_id=CASE WHEN %s='completed' THEN (SELECT max(id) FROM recy.sync_runs) ELSE NULL END
                WHERE id=%s AND lease_owner=%s''',(status,error,status,status,job_id,owner))

    @staticmethod
    def _usable_upc(value):
        try:
            return normalize_upc(value)
        except ValueError:
            return None

    def checkpoint_start(self, fingerprint):
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            state = db.execute('SELECT * FROM recy.scrape_state WHERE id=1').fetchone()
            if state and state['fingerprint'] == fingerprint:
                return state['started_at']
            db.execute('DELETE FROM recy.scrape_pages')
            db.execute('DELETE FROM recy.scrape_state')
            started_at = now()
            db.execute('INSERT INTO recy.scrape_state VALUES (1,%s,%s)', (fingerprint, started_at))
            return started_at

    def checkpoint_pages(self, fingerprint):
        with self.connect() as db:
            rows = db.execute('SELECT p.url,p.html FROM recy.scrape_pages p JOIN recy.scrape_state s '
                              'ON s.id=1 WHERE s.fingerprint=%s', (fingerprint,)).fetchall()
            return {row['url']: row['html'] for row in rows}

    def checkpoint_put(self, fingerprint, url, html):
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            state = db.execute('SELECT fingerprint FROM recy.scrape_state WHERE id=1').fetchone()
            if not state or state['fingerprint'] != fingerprint:
                raise SourceError('Another scrape replaced this checkpoint')
            db.execute('INSERT INTO recy.scrape_pages VALUES (%s,%s) ON CONFLICT (url) '
                       'DO UPDATE SET html=EXCLUDED.html', (url, html))

    def checkpoint_clear(self, fingerprint):
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            state = db.execute('SELECT fingerprint FROM recy.scrape_state WHERE id=1').fetchone()
            if state and state['fingerprint'] == fingerprint:
                db.execute('DELETE FROM recy.scrape_pages')
                db.execute('DELETE FROM recy.scrape_state')
