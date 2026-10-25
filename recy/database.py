import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .source import SourceError, upc_column


def now():
    return datetime.now(timezone.utc).isoformat()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def normalize_upc(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9 -]+", value):
        raise ValueError("UPC must contain ASCII digits, optionally separated by spaces or hyphens")
    value = value.replace(" ", "").replace("-", "")
    if not 4 <= len(value) <= 18:
        raise ValueError("UPC must contain 4 to 18 digits")
    return value


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
                source_url TEXT NOT NULL, pages INTEGER NOT NULL, added INTEGER NOT NULL,
                removed INTEGER NOT NULL, total INTEGER NOT NULL, headers TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function("normalize_upc", 1, self._normalize_stored, deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _normalize_stored(value):
        try:
            return normalize_upc(value)
        except ValueError:
            return None

    @staticmethod
    def _latest(db):
        row = db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["headers"] = json.loads(result["headers"])
        return result

    def status(self):
        with self.connect() as db:
            return self._latest(db)

    def sync(self, snapshot, started_at, max_removal_fraction=0.25, dry_run=False):
        if not 0 <= max_removal_fraction <= 1:
            raise ValueError("Removal fraction must be between 0 and 1")
        headers = snapshot.headers
        if (not headers or not all(isinstance(h, str) and h for h in headers) or
                len({h.casefold() for h in headers}) != len(headers)):
            raise SourceError("Invalid snapshot schema")
        column = upc_column(headers)
        if not snapshot.rows or any(len(r) != len(headers) for r in snapshot.rows):
            raise SourceError("Empty or malformed snapshot")
        incoming = set(tuple(row) for row in snapshot.rows)
        if not any(self._normalize_stored(row[headers.index(column)]) for row in incoming):
            raise SourceError("Snapshot contains no usable UPCs")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self._latest(db)
            if latest and started_at <= latest["started_at"]:
                raise SourceError("A newer sync already committed; discard this snapshot")
            if latest and latest["headers"] != headers:
                raise SourceError("Source columns changed; review schema migration before syncing")
            existing = set()
            if latest:
                existing = {tuple(r) for r in db.execute('SELECT * FROM registered_products')}
            added, removed = incoming - existing, existing - incoming
            if existing and len(removed) / len(existing) > max_removal_fraction:
                raise SourceError("Removal threshold exceeded (%s/%s rows); inspect source before raising limit" %
                                  (len(removed), len(existing)))
            report = {"started_at": started_at, "completed_at": now(), "source_url": snapshot.source_url,
                      "pages": snapshot.pages, "added": len(added), "removed": len(removed),
                      "total": len(incoming), "headers": headers, "dry_run": dry_run}
            if dry_run:
                db.rollback()
                return report
            if not latest:
                db.execute('CREATE TABLE registered_products (%s)' %
                           ', '.join(quote(h) + ' TEXT NOT NULL' for h in headers))
                db.execute('CREATE INDEX registered_products_upc ON registered_products '
                           '(normalize_upc(%s))' % quote(column))
            if removed:
                where = ' AND '.join(quote(h) + ' = ?' for h in headers)
                db.executemany('DELETE FROM registered_products WHERE ' + where, sorted(removed))
            if added:
                db.executemany('INSERT INTO registered_products VALUES (%s)' %
                               ', '.join('?' for _ in headers), sorted(added))
            db.execute('''INSERT INTO sync_runs
                (started_at, completed_at, source_url, pages, added, removed, total, headers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                       (started_at, report['completed_at'], snapshot.source_url, snapshot.pages,
                        len(added), len(removed), len(incoming), json.dumps(headers)))
            return report

    def verify(self, raw_upc):
        code = normalize_upc(raw_upc)
        with self.connect() as db:
            # One read transaction keeps metadata and rows on the same snapshot.
            db.execute("BEGIN")
            latest = self._latest(db)
            if not latest:
                return None
            column = upc_column(latest['headers'])
            rows = db.execute('SELECT * FROM registered_products WHERE normalize_upc(%s) = ?' %
                              quote(column), (code,)).fetchall()
            return {"upc": code, "registered": bool(rows), "matches": [dict(r) for r in rows],
                    "last_synced_at": latest["completed_at"], "source_url": latest["source_url"]}
