import argparse
import json
import os
import sys

from .api import create_server
from .database import now
from .backend import get_database
from .source import COUNT_PATTERN, Scraper, SourceError


def main():
    parser = argparse.ArgumentParser(description="Return-It registry mirror")
    parser.add_argument("--db", default=os.environ.get("RECY_DB", "data/registry.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Scrape all registry result pages, validate, then reconcile atomically")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--delay", type=float, default=1.0)
    sync.add_argument("--count-pattern", default=COUNT_PATTERN,
                      help="Regex with capture groups containing total result counts")
    sync.add_argument("--max-removal-fraction", type=float, default=0.25)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    verify = sub.add_parser("verify")
    verify.add_argument("upc")
    sub.add_parser("status")
    sub.add_parser("probe", help="Validate live first, second and last pages without importing")
    sub.add_parser("init-db", help="Apply PostgreSQL schema migration")
    migration = sub.add_parser('migrate', help='Apply versioned PostgreSQL application migrations')
    migration.add_argument('--plan', action='store_true', help='Show pending migrations without changing tables')
    scheduled = sub.add_parser("scheduled-sync", help="Resume a bounded PostgreSQL scrape when due")
    scheduled.add_argument("--interval-days", type=int, default=7)
    scheduled.add_argument("--max-seconds", type=int, default=10800)
    scheduled.add_argument("--delay", type=float, default=1.0)
    scheduled.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "probe":
            print(json.dumps(Scraper().probe(), indent=2, ensure_ascii=False))
            return
        database = get_database(args.db)
        if args.command == 'migrate':
            if not hasattr(database, 'migrate'):
                raise ValueError('migrate requires DATABASE_URL for PostgreSQL')
            result = database.migrate(plan=args.plan)
        elif args.command == "init-db":
            if not hasattr(database, 'initialize'):
                raise ValueError('init-db requires DATABASE_URL for PostgreSQL')
            database.initialize()
            result = {'status': 'initialized'}
        elif args.command == 'scheduled-sync':
            if not hasattr(database, 'checkpoint_start'):
                raise ValueError('scheduled-sync requires DATABASE_URL for PostgreSQL')
            from .scheduled import run_scheduled
            result = run_scheduled(database, args.interval_days, args.max_seconds, args.delay, args.dry_run)
        elif args.command == "sync":
            if args.delay < 0 or not 0 <= args.max_removal_fraction <= 1:
                raise ValueError("Delay must be nonnegative; removal fraction must be between 0 and 1")
            started = now()
            snapshot = Scraper(delay=args.delay, count_pattern=args.count_pattern).scrape()
            result = database.sync(snapshot, started, args.max_removal_fraction, args.dry_run)
        elif args.command == "serve":
            server = create_server(database, args.host, args.port)
            print("Recy API listening on http://%s:%s" % server.server_address, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        elif args.command == "verify":
            result = database.verify(args.upc)
            if result is None:
                raise SourceError("Registry has not been synced")
        else:
            result = database.status()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (SourceError, ValueError, OSError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
