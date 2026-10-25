"""Choose PostgreSQL for deployment; retain SQLite for local development."""
import os

from .database import Database


def get_database(path=None):
    url = os.environ.get("DATABASE_URL")
    if url:
        from .postgres import PostgresDatabase
        return PostgresDatabase(url)
    if os.environ.get("RECY_REQUIRE_POSTGRES") == "1":
        raise ValueError("DATABASE_URL is required; SQLite fallback is disabled")
    return Database(path or os.environ.get('RECY_DB', 'data/registry.sqlite3'))
