"""Conservative publication evidence from website content, never Corpus dates."""
import json
import re
from datetime import date
from urllib.parse import unquote, urlsplit

from crawler_coverage_auditor.discovery.page import parsed_html, parsed_json


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip()[:10])
        return parsed if 1900 <= parsed.year <= 2099 else None
    except ValueError:
        return None


def _words(value):
    return re.findall(r'\w+', value.casefold())


def _article_heading(url, heading=None):
    parts = [part for part in urlsplit(url).path.split('/') if part]
    if not any(part.lower() in {'blog', 'news', 'article', 'articles', 'news-releases', 'press-releases'} for part in parts[:-1]):
        return None
    heading_words = _words(heading or '')
    if len(heading_words) < 4:
        heading_words = _words(unquote(parts[-1]))
    if len(heading_words) < 4:
        return None
    return heading_words


def snapshot_publication(text, url, heading=None):
    """Recover an article's displayed date only beside its corroborated heading."""
    heading_words = _article_heading(url, heading)
    if not text or not heading_words:
        return None
    dates = set()
    for match in re.finditer(r'\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s+', text, re.I):
        following = _words(text[match.end():match.end() + 500])
        if following[:len(heading_words)] != heading_words:
            continue
        try:
            dates.add(date(int(match[3]), int(match[2]), int(match[1])))
        except ValueError:
            pass
    if len(dates) != 1:
        return None
    parsed = dates.pop()
    return {'date': parsed.isoformat(), 'year': str(parsed.year),
            'source': 'website date/time immediately before corroborated article heading'}


def publication_evidence(html, url):
    """Read page-level published metadata; conflicting or unrelated dates are unknown."""
    soup = parsed_html(html, mutable=True)
    dates = {_date(item.get('content') or item.get('datetime')) for item in soup.select(
        'meta[property="article:published_time"], meta[itemprop="datePublished"], time[itemprop="datePublished"][datetime]')}
    current = urlsplit(url)

    def collect(item):
        if isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            collect(item.get('@graph', []))
            kinds = item.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            identity = item.get('url') or item.get('mainEntityOfPage')
            if isinstance(identity, dict):
                identity = identity.get('@id')
            if not isinstance(identity, str) or not isinstance(kinds, list):
                return
            declared = urlsplit(identity)
            if (declared.scheme, declared.netloc, declared.path.rstrip('/'), declared.query) != (current.scheme, current.netloc, current.path.rstrip('/'), current.query):
                return
            if set(kinds) & {'Article', 'NewsArticle', 'BlogPosting', 'Report'}:
                dates.add(_date(item.get('datePublished')))

    for script in soup.select('script'):
        if script.get('type', '').split(';')[0].strip().lower() != 'application/ld+json':
            continue
        try:
            collect(parsed_json(script.get_text()))
        except (TypeError, ValueError):
            pass
    dates.discard(None)
    if len(dates) > 1:
        return None
    if dates:
        parsed = dates.pop()
        return {'date': parsed.isoformat(), 'year': str(parsed.year), 'source': 'website publication metadata'}
    heading = soup.select_one('h1')
    for item in soup.select('script, style, nav, header, footer, aside'):
        item.decompose()
    return snapshot_publication(soup.get_text(' ', strip=True), url, heading.get_text(' ', strip=True) if heading and heading.parent else None)


def enrich_archive_rows(rows, snapshots):
    """Read historical snapshots lazily, only when publication evidence is missing.

    Accept a mapping or an indexed lookup callable. New scans already persist
    publication evidence, so reporting normally need not read their large texts.
    """
    lookup = snapshots if callable(snapshots) else snapshots.get
    result = []
    for original in rows:
        row = dict(original)
        if row.get('page_type') == 'HTML' and row.get('validity') == 'VALID_PAGE':
            try:
                evidence = json.loads(row.get('content_evidence_json') or '{}')
            except (TypeError, ValueError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            if not evidence.get('website_publication') and _article_heading(row['normalized_url'], row.get('h1')):
                publication = snapshot_publication(lookup(row['normalized_url']), row['normalized_url'], row.get('h1'))
                if publication:
                    evidence['website_publication'] = publication
                    row['content_evidence_json'] = json.dumps(evidence)
        result.append(row)
    return result
