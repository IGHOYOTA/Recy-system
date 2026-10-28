"""Durable, bounded scrape runs for GitHub Actions."""
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html import escape

from .source import RegistryHTML, Scraper, SourceError, SOURCE_URL, text_content


class TimeBudgetReached(Exception):
    pass


def source_version(document):
    match = re.search(r'Database Last Updated\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',
                      text_content(document.text))
    if not match or len(document.result_counts) != 1:
        raise SourceError('Resumable scraping requires source update date and total count')
    return match[1], next(iter(document.result_counts))


def fingerprint(html):
    document = RegistryHTML(html)
    headers, rows = document.products()
    value = [source_version(document), headers, rows]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def compact_page(html):
    """Store table data and relevant paging links, not repeated site assets/navigation."""
    document = RegistryHTML(html)
    headers, rows = document.products()
    version, total = source_version(document)
    numbers = [int(label.replace(',', '')) for _, _, label in document.links
               if re.fullmatch(r'\d[\d,]*', label)]
    # Rn is the verified source's page-number parameter. Keep nearby links and the last.
    current = (int(rows[0][0]) - 1) // 30 + 1 if headers[0] == '#' else None
    parts = ['<p title="%s products">Database Last Updated %s</p><table><tr>' % (total, escape(version))]
    parts.extend('<th>%s</th>' % escape(h) for h in headers)
    parts.append('</tr>')
    for row in rows:
        parts.append('<tr>' + ''.join('<td>%s</td>' % escape(v) for v in row) + '</tr>')
    parts.append('</table>')
    for href, rel, label in document.links:
        numeric = re.fullmatch(r'\d[\d,]*', label)
        if numeric and current is not None:
            n = int(label.replace(',', ''))
            if abs(n - current) > 4 and n != max(numbers):
                continue
        if numeric or re.search(r'next|last', rel + ' ' + label, re.I):
            parts.append('<a href="%s" rel="%s">%s</a>' %
                         (escape(href, quote=True), escape(rel, quote=True), escape(label)))
    return ''.join(parts)


def run_scheduled(database, max_seconds=10800, delay=1.0, dry_run=False, first_html=None, refresh_lease=None):
    if max_seconds < 1 or delay < 0:
        raise ValueError('Invalid time budget or delay')
    scraper = Scraper(delay=delay)
    deadline = time.monotonic() + max_seconds
    fetch = scraper.fetch
    first = first_html if first_html is not None else fetch(SOURCE_URL)
    from .refresh import imported_date, source_date
    observed = source_date(first, datetime.now(timezone.utc))
    baseline = imported_date(database.status())
    if baseline is not None and observed <= baseline:
        return {'status':'already_current','source_updated_on':observed.isoformat()}
    generation = fingerprint(first)
    version = source_version(RegistryHTML(first))
    started_at = database.checkpoint_start(generation)
    cached_pages = database.checkpoint_pages(generation)
    fetched = 0

    def cached_fetch(url):
        nonlocal fetched
        if time.monotonic() >= deadline:
            raise TimeBudgetReached()
        if url == SOURCE_URL:
            return first
        cached = cached_pages.get(url)
        if cached is not None:
            return cached
        html = fetch(url)
        if source_version(RegistryHTML(html)) != version:
            raise SourceError('Registry changed during scrape; resume will start a new checkpoint')
        compact = compact_page(html)
        database.checkpoint_put(generation, url, compact)
        fetched += 1
        if fetched % 20 == 0:
            print('Checkpointed %s additional pages this run' % fetched, file=sys.stderr, flush=True)
        return compact

    scraper.fetch = cached_fetch
    try:
        snapshot = scraper.scrape()
    except TimeBudgetReached:
        return {'status': 'checkpointed', 'new_pages': fetched,
                'message': 'Resume on next scheduled or manual run; active registry unchanged'}
    if fingerprint(fetch(SOURCE_URL)) != generation:
        raise SourceError('Registry changed before publication; active registry unchanged')
    report = database.sync(snapshot, started_at, dry_run=dry_run, source_updated_on=observed, refresh_lease=refresh_lease)
    if not dry_run:
        database.checkpoint_clear(generation)
    return {'status': 'dry_run' if dry_run else 'published', **report}
