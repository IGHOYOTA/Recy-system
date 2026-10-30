"""Daily source-date checks and durable midnight jobs; no weekly age-based refresh."""
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from importlib.resources import files
from uuid import uuid4
from zoneinfo import ZoneInfo

from .source import RegistryHTML, Scraper, SourceError, SOURCE_URL
from .scheduled import run_scheduled, source_version, content_fingerprint, read_source_page

SOURCE_TIMEZONE = 'America/Vancouver'


@lru_cache(maxsize=16)
def local_zone(name):
    # Use the declared tzdata dependency, not a potentially stale host database.
    ZoneInfo(name)  # Validate IANA names and reject file paths.
    with files('tzdata').joinpath('zoneinfo/'+name).open('rb') as stream:
        return ZoneInfo.from_file(stream,key=name)


def source_date(html, now):
    document = RegistryHTML(html)
    document.products()  # A block/error page must never count as a successful check.
    version, _ = source_version(document)
    if version is None:
        return None
    try:
        result = datetime.strptime(version, '%B %d, %Y').date()
    except ValueError:
        raise SourceError('Source database update date is invalid') from None
    if result > now.astimezone(local_zone(SOURCE_TIMEZONE)).date():
        raise SourceError('Source database update date is in the future')
    return result


def imported_date(latest):
    if not latest:
        return None
    known = latest.get('source_updated_on')
    if known is not None:
        return known if hasattr(known, 'year') else datetime.strptime(known, '%Y-%m-%d').date()
    # Old publications did not retain the source date. Use START, not completion:
    # a multi-day scrape must not hide an update released while it was running.
    return datetime.fromisoformat(latest['started_at']).astimezone(local_zone(SOURCE_TIMEZONE)).date()


def next_midnight(now, zone=SOURCE_TIMEZONE):
    local = now.astimezone(local_zone(zone))
    return datetime.combine(local.date()+timedelta(days=1), time.min, local_zone(zone)).astimezone(timezone.utc)


def check_source_update(database, zone=SOURCE_TIMEZONE, now=None):
    now = now or datetime.now(timezone.utc)
    local_zone(zone)  # Validate configuration before fetching.
    html = read_source_page(Scraper(delay=1).fetch)
    observed = source_date(html, now)
    return database.record_source_observation(observed, now, next_midnight(now, zone), zone,
                                              content_fingerprint(html))


def run_due_sync(database, max_seconds=10800, delay=1.0, dry_run=False, now=None):
    if max_seconds < 1 or delay < 0:
        raise ValueError('Invalid time budget or delay')
    now = now or datetime.now(timezone.utc)
    owner = uuid4()
    job = database.claim_source_refresh(now, owner, max_seconds+600)
    if job is None:
        return {'status':'not_due', 'message':'No changed source date is due for scraping'}
    try:
        html = read_source_page(Scraper(delay=delay).fetch)
        observed = source_date(html, now)
        probe=content_fingerprint(html)
        latest=database.status()
        baseline = imported_date(latest)
        if observed is None:
            current=latest and latest.get('source_fingerprint')==probe
            # A dated release already observed before the date vanished must still
            # be imported unless a matching snapshot was published after detection.
            if current and (job['source_updated_on'] is None or
                            datetime.fromisoformat(latest['started_at'])>=job['detected_at']):
                database.finish_source_refresh(job['id'],owner,'completed')
                return {'status':'already_current','detection_method':'content'}
            if job['source_updated_on'] is None and job['source_fingerprint']!=probe:
                database.record_source_observation(None,now,next_midnight(now,job['timezone']),job['timezone'],probe)
                database.finish_source_refresh(job['id'],owner,'superseded')
                return {'status':'superseded','detection_method':'content'}
            result=run_scheduled(database,max_seconds=max_seconds,delay=delay,dry_run=dry_run,
                                 first_html=html,refresh_lease=(job['id'],owner))
            status='completed' if result['status'] in ('published','already_current') else 'running'
            database.finish_source_refresh(job['id'],owner,status)
            return {'job_id':job['id'],'source_updated_on':None,'detection_method':'content',**result}
        if job['source_updated_on'] is None:
            database.record_source_observation(observed,now,next_midnight(now,job['timezone']),job['timezone'],probe)
            database.finish_source_refresh(job['id'],owner,'superseded')
            return {'status':'superseded','detection_method':'date'}
        if observed != job['source_updated_on']:
            if observed > job['source_updated_on']:
                # A newer release detected after midnight gets its OWN next-midnight
                # deadline; never run it early on an old version's queued job.
                database.record_source_observation(observed, now, next_midnight(now, job['timezone']), job['timezone'])
                database.finish_source_refresh(job['id'], owner, 'superseded')
                return {'status':'superseded','message':'A newer source date is queued for its next midnight'}
            raise SourceError('Source date moved backwards; queued job retained for retry')
        if baseline is not None and observed <= baseline:
            result = {'status':'already_current','source_updated_on':observed.isoformat()}
            database.finish_source_refresh(job['id'], owner, 'completed')
            return result
        result = run_scheduled(database, max_seconds=max_seconds, delay=delay,
                               dry_run=dry_run, first_html=html, refresh_lease=(job['id'],owner))
        status = 'completed' if result['status'] in ('published','already_current') else 'running'
        database.finish_source_refresh(job['id'], owner, status)
        return {'job_id':job['id'], 'source_updated_on':observed.isoformat(), **result}
    except Exception as exc:
        database.finish_source_refresh(job['id'], owner, 'running', type(exc).__name__)
        raise
