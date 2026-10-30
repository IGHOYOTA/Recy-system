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
    counts=set(document.result_counts)
    if len(counts)!=1:
        raise SourceError('Resumable scraping requires one unambiguous product total')
    return match[1] if match else None, next(iter(counts))


def content_fingerprint(html):
    document=RegistryHTML(html)
    headers,rows=document.products()
    _,total=source_version(document)
    return hashlib.sha256(json.dumps([total,headers,rows]).encode()).hexdigest()


def fingerprint(html):
    document = RegistryHTML(html)
    headers, rows = document.products()
    value = [source_version(document), headers, rows]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def read_source_page(fetch):
    """Resolve metadata omitted on the first page from the verified final page."""
    html=fetch(SOURCE_URL)
    document=RegistryHTML(html)
    version,total=source_version(document)
    if version is not None:
        return html
    headers,rows=document.products()
    links={int(label.replace(',','')):href for href,_,label in document.links if re.fullmatch(r'\d[\d,]*',label)}
    last=max(links) if links else 1
    if last>1:
        final=RegistryHTML(fetch(Scraper._page_url(SOURCE_URL,links[last])))
        final_headers,final_rows=final.products()
        final_date,final_total=source_version(final)
        ended=any('disabled' in rel and re.search(r'next',rel+' '+label,re.I) for _,rel,label in final.links)
        if headers!=final_headers or total!=final_total or not ended or (last-1)*len(rows)+len(final_rows)!=total:
            raise SourceError('Final-page metadata or page size does not match the first page')
        if final_date:
            # This is the actual source date from another page, never an invented date.
            html+='<p title="%s products">Database Last Updated %s</p>'%(total,escape(final_date))
    return html


def compact_page(html, verified_version=None):
    """Store table data and relevant paging links, not repeated site assets/navigation."""
    document = RegistryHTML(html)
    headers, rows = document.products()
    version, total = source_version(document)
    if version is None and verified_version is not None:
        version=verified_version[0]
    numbers = [int(label.replace(',', '')) for _, _, label in document.links
               if re.fullmatch(r'\d[\d,]*', label)]
    # Rn is the verified source's page-number parameter. Keep nearby links and the last.
    current = (int(rows[0][0]) - 1) // 30 + 1 if headers[0] == '#' else None
    label='Database Last Updated '+version if version else 'Source update date unavailable'
    parts = ['<p title="%s products">%s</p><table><tr>' % (total, escape(label))]
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
    first = first_html if first_html is not None else read_source_page(fetch)
    from .refresh import imported_date, source_date
    observed = source_date(first, datetime.now(timezone.utc))
    baseline = imported_date(database.status())
    if observed is not None and baseline is not None and observed <= baseline:
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
        page_version=source_version(RegistryHTML(html))
        if page_version[1]!=version[1] or (page_version[0] is not None and page_version[0]!=version[0]):
            raise SourceError('Registry changed during scrape; resume will start a new checkpoint')
        compact = compact_page(html,version)
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
    if fingerprint(read_source_page(fetch)) != generation:
        raise SourceError('Registry changed before publication; active registry unchanged')
    report = database.sync(snapshot, started_at, dry_run=dry_run, source_updated_on=observed,
                           refresh_lease=refresh_lease, source_fingerprint=content_fingerprint(first))
    if not dry_run:
        database.checkpoint_clear(generation)
    return {'status': 'dry_run' if dry_run else 'published', **report}
