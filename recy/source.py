"""Public HTML source adapter with numbered result-page traversal."""

import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

REGISTRY_URL = "https://www.return-it.ca/registeredbrands/"
SEARCH_FIELDS = ("upc", "St", "flvr", "type", "size")
SOURCE_URL = REGISTRY_URL + "?" + urlencode(dict.fromkeys(SEARCH_FIELDS, ""))
USER_AGENT = "Mozilla/5.0 (compatible; RecyRegistry/0.1; public registry mirror)"
# Override only after inspecting the source's actual result-count wording.
COUNT_PATTERN = r"(?:of\s+([\d,]+)\s+(?:results|records|products)|([\d,]+)\s+(?:results|records|products)\s+(?:found|returned)|(?:total\s*(?:results|records|products)?\s*[:=]\s*)([\d,]+))"


class SourceError(RuntimeError):
    pass


@dataclass
class Snapshot:
    headers: list
    rows: list
    pages: int
    source_url: str = SOURCE_URL


def text_content(parts):
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def repair_cell_text(html):
    """Preserve stray '<' text without consuming the following closing cell.

    Some source flavours contain '<Orange & Grapefrui' without a closing '>'.
    HTMLParser otherwise consumes the cell terminator as part of that tag.
    Complete tags, including quoted attributes containing '<', remain intact.
    """
    token = re.compile(r'''<(?:[^<>"']|"[^"]*"|'[^']*')*>|<''')

    def repair(match):
        body = token.sub(lambda t: '&lt;' if t[0] == '<' else t[0], match[2])
        return match[1] + body + match[3]

    return re.sub(r'(<t[dh]\b[^>]*>)(.*?)(</t[dh]\s*>)', repair, html,
                  flags=re.I | re.S)


def upc_column(headers):
    matches = [h for h in headers if re.sub(r"[^a-z]", "", h.lower()) in
               ("upc", "upccode", "upccodes", "universalproductcode")]
    if len(matches) != 1:
        raise SourceError("Expected exactly one UPC column; found headers: %r" % headers)
    return matches[0]


class RegistryHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.links = []
        self.types = []
        self.text = []
        self.result_counts = set()
        self.page_ranges = []
        self._link_range = None
        self._table = None
        self._row = None
        self._cell = None
        self._select = None
        self._link = None
        self._ignore = 0
        self.feed(repair_cell_text(html))
        if not self.result_counts and self.page_ranges:
            page,start,end=max(self.page_ranges)
            numeric=[int(label.replace(',','')) for _,_,label in self.links if re.fullmatch(r'\d[\d,]*',label)]
            # Range titles on Return-It's global final-page link supply the total
            # when the old date/count paragraph is absent. Traversal and sampled
            # last-page size still independently validate completeness.
            if numeric and page==max(numeric) and start==(page-1)*30+1 and 0<end-start+1<=30:
                self.result_counts.add(end)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        count = re.fullmatch(r"([\d,]+) products", attrs.get("title", ""), re.I)
        if tag == "p" and count:
            self.result_counts.add(int(count[1].replace(",", "")))
        if tag in ("script", "style"):
            self._ignore += 1
        if tag == "select":
            self._select = attrs.get("name")
        if tag == "option" and self._select == "type" and attrs.get("value"):
            self.types.append(attrs["value"])
        if tag == "table":
            if self._table is not None:
                raise SourceError("Nested result tables are unsupported")
            self._table = []
        if tag == "tr" and self._table is not None:
            self._row = []
        if tag in ("td", "th") and self._row is not None:
            if attrs.get("colspan", "1") != "1" or attrs.get("rowspan", "1") != "1":
                raise SourceError("Spanning table cells require source adapter review")
            self._cell = (tag, [])
        if tag == "br" and self._cell:
            self._cell[1].append(" ")
        if tag == "a":
            page_range = re.fullmatch(r'([\d,]+)\s+to\s+([\d,]+)',attrs.get('title',''),re.I)
            self._link_range = tuple(int(v.replace(',','')) for v in page_range.groups()) if page_range else None
            rel = attrs.get("rel", "")
            if "disabled" in attrs.get("class", "").split() or attrs.get("aria-disabled") == "true":
                rel += " disabled"
            hint = attrs.get("aria-label", "") + " " + attrs.get("title", "")
            if re.search(r"\blast\b", hint, re.I):
                rel += " last"
            if re.search(r"\bnext\b", hint, re.I):
                rel += " next"
            self._link = [attrs.get("href", ""), rel, []]

    def handle_data(self, data):
        if not self._ignore:
            self.text.append(data)
        if self._cell is not None:
            self._cell[1].append(data)
        if self._link is not None:
            self._link[2].append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._ignore = max(0, self._ignore - 1)
        if tag == "select":
            self._select = None
        if tag in ("td", "th") and self._cell is not None:
            self._row.append((self._cell[0], text_content(self._cell[1])))
            self._cell = None
        if tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        if tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        if tag == "a" and self._link is not None:
            label=text_content(self._link[2])
            if self._link_range and re.fullmatch(r'\d[\d,]*',label):
                self.page_ranges.append((int(label.replace(',','')), *self._link_range))
            self.links.append((self._link[0], self._link[1], text_content(self._link[2])))
            self._link = None
            self._link_range = None

    def products(self):
        candidates = []
        for table in self.tables:
            if not table:
                continue
            headers = [v for _, v in table[0]]
            try:
                upc_column(headers)
            except SourceError:
                continue
            if not all(headers) or len(set(h.casefold() for h in headers)) != len(headers):
                raise SourceError("Empty or duplicate source column names")
            rows = []
            for row in table[1:]:
                if [v for _, v in row] == headers:
                    continue
                if len(row) != len(headers):
                    raise SourceError("Result row width differs from table header")
                rows.append(tuple(v for _, v in row))
            candidates.append((headers, rows))
        if len(candidates) != 1:
            raise SourceError("Expected one result table with a UPC header; source may have changed or blocked access")
        return candidates[0]


class Scraper:
    def __init__(self, fetch=None, delay=1.0, count_pattern=COUNT_PATTERN):
        self.fetch = fetch or self._fetch
        self.delay = delay
        self.count_pattern = re.compile(count_pattern, re.I)
        self._last_request = 0.0

    def _fetch(self, url):
        time.sleep(max(0, self.delay - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT,
                                        "Accept": "text/html"})
        try:
            with urlopen(request, timeout=45) as response:
                parsed = urlparse(response.url)
                if (parsed.scheme != "https" or parsed.netloc != "www.return-it.ca" or
                        parsed.path.rstrip("/") != "/registeredbrands"):
                    raise SourceError("Source redirected outside registry")
                if "text/html" not in response.headers.get("Content-Type", ""):
                    raise SourceError("Expected HTML from registry")
                payload = response.read(20_000_001)
                if len(payload) > 20_000_000:
                    raise SourceError("Registry response exceeds 20 MB")
                return payload.decode(response.headers.get_content_charset() or "utf-8")
        except (OSError, UnicodeError) as exc:
            raise SourceError("Registry request failed: %s" % exc) from exc

    def scrape(self):
        # Empty search fields are significant: the bare page displays only a form.
        pending = {1: SOURCE_URL}
        visited = {}
        headers = None
        all_rows = set()
        expected = None
        declared_last = None
        numbered_pages = set()
        row_counts = {}
        while pending:
            number = min(pending)
            url = pending.pop(number)
            if len(visited) >= 10000:
                raise SourceError("Pagination exceeds 10,000-page limit")
            if url in visited.values():
                raise SourceError("Pagination loop: same URL assigned to multiple pages")
            try:
                document = RegistryHTML(self.fetch(url))
                page_headers, rows = document.products()
            except SourceError as exc:
                raise SourceError('Registry page %s (%s): %s' % (number, url, exc)) from exc
            if headers is None:
                headers = page_headers
            if headers != page_headers:
                raise SourceError("Source schema changed during scrape")
            if not rows:
                raise SourceError("Empty result page; snapshot rejected")
            if all_rows.intersection(rows):
                raise SourceError("Overlapping result pages; pagination or source data changed")
            visited[number] = url
            row_counts[number] = len(rows)
            all_rows.update(rows)
            matches = self.count_pattern.findall(text_content(document.text))
            counts = document.result_counts | {int(value.replace(",", "")) for match in matches
                      for value in (match if isinstance(match, tuple) else (match,)) if value}
            if len(counts) > 1:
                raise SourceError("Conflicting source result counts")
            if counts:
                count = counts.pop()
                if expected is not None and count != expected:
                    raise SourceError("Result count changed during pagination; retry later")
                expected = count
            for href, rel, label in document.links:
                relations = rel.lower().split()
                if "disabled" in relations:
                    continue
                numeric = re.fullmatch(r"\d[\d,]*", label)
                next_page = "next" in relations or re.fullmatch(
                    r"next(?:\s+page)?\s*[»›>]?|[»›]", label, re.I)
                last_page = "last" in relations or re.fullmatch(
                    r"last(?:\s+page)?\s*[»›>]*", label, re.I)
                if not (numeric or next_page or last_page):
                    continue
                target = self._page_url(url, href)
                if numeric:
                    target_number = int(label.replace(",", ""))
                    numbered_pages.add(target_number)
                elif last_page:
                    query = parse_qs(urlparse(target).query)
                    values = [v[0] for k, v in query.items()
                              if k not in SEARCH_FIELDS and len(v) == 1 and v[0].isdigit()]
                    if len(values) != 1:
                        raise SourceError("Cannot determine last-page number from link")
                    target_number = int(values[0])
                    if declared_last is not None and declared_last != target_number:
                        raise SourceError("Last-page number changed during scrape")
                    declared_last = target_number
                else:
                    target_number = number + 1
                if not 1 <= target_number <= 10000:
                    raise SourceError("Invalid page number")
                known_url = visited.get(target_number) or pending.get(target_number)
                # Page 1 may have both a bare search URL and an explicit page=1 link.
                if target_number != 1 and known_url is not None and known_url != target:
                    raise SourceError("Conflicting URLs for the same result page")
                if target_number not in visited:
                    pending[target_number] = target

        last = max(visited)
        if set(visited) != set(range(1, last + 1)):
            raise SourceError("Missing numbered result pages; snapshot rejected")
        if declared_last is not None and last != declared_last:
            raise SourceError("Did not reach declared last result page")
        if expected is not None:
            if sum(row_counts.values()) != expected:
                raise SourceError("Incomplete results: expected %s rows, got %s" %
                                  (expected, sum(row_counts.values())))
        else:
            # Numbered pagination can establish coverage without a record total.
            # A single uncounted table cannot prove that it is the complete registry.
            if last < 2 or (last not in numbered_pages and declared_last != last):
                raise SourceError("Cannot prove complete results: no total count or numbered last page")
            page_size = row_counts[1]
            if any(row_counts[n] != page_size for n in range(1, last)) or row_counts[last] > page_size:
                raise SourceError("Inconsistent result page sizes; snapshot rejected")
        if not all_rows:
            raise SourceError("Empty registry snapshot rejected")
        return Snapshot(headers, sorted(all_rows), len(visited))

    def probe(self):
        """Read real first, second and last pages without importing a partial DB."""
        first = RegistryHTML(self.fetch(SOURCE_URL))
        headers, rows = first.products()
        if not rows:
            raise SourceError("No products on first result page")
        links = {1: SOURCE_URL}
        for href, rel, label in first.links:
            if "disabled" not in rel.split() and re.fullmatch(r"\d[\d,]*", label):
                number = int(label.replace(",", ""))
                if not 1 <= number <= 10000:
                    raise SourceError("Invalid page number")
                if number != 1:
                    links[number] = self._page_url(SOURCE_URL, href)
        last = max(links)
        samples = {}
        total_counts = set(first.result_counts)
        for number in sorted({1, min(2, last), last}):
            if number not in links:
                raise SourceError("Probe could not find sample page link")
            document = first if number == 1 else RegistryHTML(self.fetch(links[number]))
            page_headers, page_rows = document.products()
            if page_headers != headers or not page_rows:
                raise SourceError("Sample page has missing rows or inconsistent columns")
            total_counts.update(document.result_counts)
            if headers[0] == "#":
                expected_numbers = [str((number - 1) * len(rows) + i + 1)
                                    for i in range(len(page_rows))]
                if [row[0] for row in page_rows] != expected_numbers:
                    raise SourceError("Sample row numbers do not match requested page")
            samples[str(number)] = {"rows": len(page_rows), "first_row": page_rows[0],
                                    "last_row": page_rows[-1]}
        if len(total_counts) != 1:
            raise SourceError("Missing or inconsistent registry total in sampled pages")
        total = total_counts.pop()
        if (last - 1) * len(rows) + samples[str(last)]["rows"] != total:
            raise SourceError("Last-page size does not match advertised product total")
        return {"source_url": SOURCE_URL, "columns": headers, "total_products": total,
                "last_page": last, "sampled_pages": samples, "full_import": False}

    @staticmethod
    def _page_url(current, href):
        parsed = urlparse(urljoin(current, href))
        if (parsed.scheme != "https" or parsed.netloc != "www.return-it.ca" or
                parsed.path.rstrip("/") != "/registeredbrands"):
            raise SourceError("Pagination link left registry")
        query = parse_qs(parsed.query, keep_blank_values=True)
        for field in SEARCH_FIELDS:
            if query.get(field, [""]) != [""]:
                raise SourceError("Pagination link changed unfiltered registry search")
            query[field] = [""]
        # Preserve the site's actual pagination parameter instead of assuming 'page'.
        return REGISTRY_URL + "?" + urlencode(sorted(query.items()), doseq=True)
