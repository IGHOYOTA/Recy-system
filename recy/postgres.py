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

    def sync(self, snapshot, started_at, max_removal_fraction=0.25, dry_run=False):
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
            db.execute('''INSERT INTO recy.sync_runs
                (started_at,completed_at,source_url,pages,added,removed,total,headers)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (started_at, report['completed_at'], snapshot.source_url, snapshot.pages,
                        added, removed, len(incoming), json.dumps(headers)))
            return report

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
