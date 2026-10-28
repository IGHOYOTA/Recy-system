# Change-driven Return-It refresh

The scheduler compares the exact registered-brand page's **Database Last Updated**
date with the version last successfully imported. It does not refresh merely
because a week has elapsed. Product table columns, leading-zero UPC handling,
atomic publication, removal guards and page checkpointing remain unchanged.

## Daily flow

1. At **11:23 p.m. America/Vancouver**, request only the first unfiltered results
   page at `https://www.return-it.ca/registeredbrands/?upc=&St=&flvr=&type=&size=`.
   Validate that it is a real product page with a source date and one result count.
2. Compare dates. An equal or older source date causes no scrape. A newer source
   date creates a durable job for the upcoming Vancouver midnight. Repeated checks
   for the same date preserve its original deadline instead of postponing it.
   An empty database queues its initial import for midnight.
3. At **12:00 midnight**, claim only an eligible job. Revalidate the first page,
   resume matching checkpoints, then publish only after every page and the final
   source-version check succeed. Record the imported source date in the publication
   transaction. A failed or incomplete run never advances that date.
4. At **3:15, 6:15 and 9:15 a.m.**, recovery invocations resume unfinished due jobs.
   These are continuations, not independent daily/full refreshes. A job has a
   bounded lease and owner fencing; crashed leases can expire safely. With no
   eligible job the runner does not contact Return-It.

The checker runs once daily, so a release posted after that day's check will be
noticed at the next check. A date comparison cannot detect unannounced changes
made while the displayed date remains identical. The scheduler never discards a
needed update solely because it is several days old: “recent” means newer than
our imported version, not an arbitrary lookback window.

## Source-date baseline

New successful scheduled imports store `recy.sync_runs.source_updated_on`. Compare
against this actual source version, rather than a completion timestamp: a multi-day
crawl can finish after another release has already appeared.

Historical publications lack that metadata. For them, use the successful run's
**start date in Vancouver** as the initial baseline. This implements the requested
site-date-versus-run-date comparison without treating a failed attempt or a late
completion as an imported version. It cannot distinguish same-day changes for
those legacy snapshots. The next changed-version publication records exact
provenance automatically. Explicit manual `sync` publications without source
metadata use the same legacy baseline rule.

If the source advances while a job is waiting, a newer version gets its own next-
midnight deadline. The old version is superseded, not used to start the new release
early. A backwards date, future date, malformed page or blocked request is surfaced
as a failure and does not advance successful monitoring/import state.

## Clock rules and GitHub schedules

The selected business timezone is `America/Vancouver`, with current bundled
`tzdata` rather than potentially outdated host timezone data. B.C. adopted permanent
Pacific time, UTC−7, after March 8, 2026; there is no November 2026 clock rollback.
See [the Province's time guidance](https://www2.gov.bc.ca/gov/content/governments/celebrating-british-columbia/daylight-saving-time).

The workflow uses UTC cron slots corresponding to that current business clock:

| Purpose | Pacific time | UTC cron |
| --- | --- | --- |
| Check the update date | 23:23 | `23 6 * * *` |
| Start a due import | 00:00 | `0 7 * * *` |
| Resume due imports | 03:15, 06:15, 09:15 | `15 10,13,16 * * *` |

The stored due timestamp is calculated using the named IANA timezone. Historical
winter/summer and the permanent-time transition are tested. Review these cron slots
if the business timezone or future time legislation changes.

GitHub schedules can start late or be dropped under heavy load; recovery slots catch
up eligible work. An exact midnight service guarantee needs a scheduler with that
SLA. See [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Database state and safe rollout

Migration `005_source_refresh_schedule.sql` adds a source-date column to sync history
and two private, RLS-enabled tables in `recy`:

- `source_monitor`: last successful first-page check and observed source date.
- `source_refresh_jobs`: observed version, original deadline, status, lease,
  attempts, error code and successful publication reference.

`python -m recy init-source-monitor` applies and checksum-tracks only this source
migration. Run `init-db` first. GitHub's reviewed default-branch workflow explicitly
runs these two source-only initialization commands, so scheduled checks do not
silently apply unrelated app migrations. The standard full `migrate` command also
recognizes migration 005 and verifies its checksum. Existing migrations are not
modified. The authoritative product table's schema is unchanged.

No active production scrape is cancelled or restarted by this code change. Existing
matching checkpoints can resume when a newer-date job becomes due. On an approved
merge, initialization/checking starts; a code push never forces an immediate scrape.

## Registry freshness for machines

Previously an import older than eight days made bottle verification unavailable.
That would incorrectly reject bottles if the source simply stayed unchanged.
The new logic accepts an older publication when a successful source check within
the last **48 hours** confirms its imported date is still current. Historical
publications use the legacy run-start baseline above. For a known imported version,
an older/regressed observed date does not count as confirmation.

If checks stop succeeding, or a newer source date is seen while an already-old
snapshot is waiting for replacement, the old publication does not receive a
freshness extension. The original eight-day rule still accepts normally recent
publications. Machine acceptance never makes an upstream request per bottle.

## Operations

```sh
# One-time source-only initialization (also done explicitly by the workflow):
python -m recy init-db
python -m recy init-source-monitor

# One first-page check; queue next midnight only for a newer date:
python -m recy check-source-update --timezone America/Vancouver

# No-op before due; otherwise resume with a bounded budget:
python -m recy scheduled-sync --max-seconds 10800

# Validate a due job without advancing publication/job completion:
python -m recy scheduled-sync --dry-run
```

The workflow dispatch dropdown offers `check` and `scrape` for these operations.
The old `--interval-days` argument is intentionally removed. Explicit `sync` remains
an operator override, outside the automatic workflow.

JSON output distinguishes `queued`, `already_queued`, `unchanged`,
`older_source_date`, `not_due`, `checkpointed`, `published`, `already_current`,
`superseded` and `dry_run`. Read the two private tables in Supabase's **recy** schema
and the existing GitHub Actions logs to inspect deadlines, progress and failures.
No Supabase client grants or browser-facing scheduling controls are introduced.
