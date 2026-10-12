"""Disjoint resource counts; collected URLs are not all valid HTML pages."""
from collections import defaultdict

from crawler_coverage_auditor.analysis.coverage import is_found, is_inventory_url
from crawler_coverage_auditor.config import capture_category

CATEGORIES = ('HTML pages', 'PDFs', 'Other documents', 'Images', 'APIs / feeds', 'Other resources')


def inventory_category(row: dict) -> str:
    kind = row.get('page_type', 'HTML')
    if kind == 'DOCUMENT':
        category = capture_category(kind, row.get('url', ''), row.get('content_type') or '')
        return {'PDF': 'PDFs', 'DOCUMENT': 'Other documents'}.get(category, 'Other resources')
    return {'HTML': 'HTML pages', 'IMAGE': 'Images', 'API': 'APIs / feeds', 'FEED': 'APIs / feeds'}.get(kind, 'Other resources')


def inventory_breakdown(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[inventory_category(row)].append(row)
    result = []
    for category in CATEGORIES:
        group = groups[category]
        ready = [row for row in group if is_inventory_url(row)]
        found = sum(is_found(row) for row in ready)
        missing = sum(row.get('match_status') == 'NOT_FOUND' for row in ready)
        pending = sum(row.get('validity') is None for row in group)
        result.append({'Type': category, 'Collected URLs': len(group), 'Ready for corpus': len(ready),
                       'Failed / excluded': len(group) - len(ready) - pending, 'Website check pending': pending,
                       'Found in corpus': found, 'Missing from corpus': missing, 'Corpus unverified': len(ready) - found - missing})
    return result


def html_content_duplicates(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if row.get('page_type') == 'HTML' and row.get('validity') == 'VALID_PAGE' and row.get('fingerprint'):
            groups[row['fingerprint']].append(row['url'])
    return [{'URL count': len(urls), 'URLs with identical extracted text': ' | '.join(urls)} for urls in groups.values() if len(urls) > 1]
