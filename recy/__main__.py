import argparse
import json
import os
import sys

from .api import create_server
from .database import now
from .backend import get_database
from .source import COUNT_PATTERN, Scraper, SourceError
from .http_support import APIError


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
    worker = sub.add_parser('work', help='Process durable application recovery jobs')
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--limit', type=int, default=20)
    worker.add_argument('--poll-seconds', type=int, default=5)
    staff = sub.add_parser('grant-staff', help='Owner-only initial staff provisioning using database credentials')
    staff.add_argument('--auth-user-id', required=True)
    staff.add_argument('--role', choices=['administrator','payout_processor','operations_reviewer','collector','depot_receiver'], default='administrator')
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
        if args.command == 'work':
            from .app_service import AppService
            from .worker import run_jobs, run_forever
            if not 1<=args.limit<=100 or not 1<=args.poll_seconds<=30:
                raise ValueError('Worker limit must be 1–100 and poll interval 1–30 seconds')
            if args.once:result=run_jobs(AppService(database),args.limit)
            else:
                run_forever(args.poll_seconds)
                return
        elif args.command == 'grant-staff':
            from .http_support import identifier
            target=identifier(args.auth_user_id)
            with database.connect() as c:
                profile=c.execute('SELECT id FROM app.profiles WHERE auth_user_id=%s',(target,)).fetchone()
                if not profile:raise ValueError('The verified user must sign in to provision a profile first')
                c.execute('''INSERT INTO app.staff_roles(user_id,role,granted_by) VALUES (%s,%s,%s)
                    ON CONFLICT (user_id,role) WHERE revoked_at IS NULL DO NOTHING''',(profile['id'],args.role,profile['id']))
                from .app_service import AppService
                AppService.audit(c,'system',None,'initial_staff_granted','profile',profile['id'],{'role':args.role})
            result={'granted':args.role,'user_id':str(profile['id'])}
        elif args.command == 'migrate':
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
    except (SourceError, ValueError, OSError, APIError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
