"""Saved, overlapping URL-backed discovery coverage; never invent states/maxima."""
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from crawler_coverage_auditor.discovery.pagination import (
    NUMERIC_PAGE_PARAMETERS,
    numeric_page_parameter,
    pagination_series_key,
)
from crawler_coverage_auditor.validation.normalize import (
    classify_parameter,
    normalize_url,
)

from .coverage import coverage_summary, is_inventory_url
from .inventory_segments import _methods, row_archive_year
from .pagination_gaps import contiguous_ranges


def address(row):
    return row.get('normalized_url') or row.get('url') or ''


def location(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/') or '/'


def learned_names(filters):
    names = defaultdict(set)
    for item in filters:
        name = item.get('control_name')
        if name and name != 'tab' and classify_parameter(name) not in {'PAGINATION', 'TRACKING', 'SESSION', 'SORT'}:
            names[location(item.get('form_action') or item.get('source_url') or '')].add(name)
    return names


def filter_label(row, names=None):
    url = address(row)
    learned = (names or {}).get(location(url), set())
    values = sorted((key, value) for key, value in parse_qsl(urlsplit(url).query)
                    if classify_parameter(key) == 'FILTER' or key in learned)
    return ', '.join(f'{key}={value}' for key, value in values)


def pagination_parameter(row):
    numeric = numeric_page_parameter(address(row))
    if numeric:
        return numeric[0], str(numeric[1]), 'Page number'
    for key, value in parse_qsl(urlsplit(address(row)).query):
        if key.lower() in {'offset', 'start'} and value.isdigit():
            return key, value, 'Offset'
        if key.lower() in {'cursor', 'cursormark'} and value:
            return key, value, 'Cursor'
        # A short CMS offset key alone is ambiguous; require link evidence.
        if key.lower() == 'o' and value.isdigit() and re.fullmatch(
                r'\s*(?:\d+|next(?: page)?|previous(?: page)?|older|newer|[>»]+)\s*',
                row.get('anchor_text') or '', re.I):
            return key, value, 'Offset'
    return None


def family(url, parameter):
    return normalize_url(pagination_series_key(url, parameter))


def archive_candidate(row):
    return bool(row_archive_year(row) or re.search(
        r'(?:^|/)(?:archives?|news-archive)(?:/|$)', urlsplit(address(row)).path, re.I))


def count_row(group):
    summary = coverage_summary(group)
    pending = sum(not row.get('validity') for row in group if row.get('page_type') != 'IMAGE')
    failed = sum(bool(row.get('validity')) and not is_inventory_url(row) for row in group
                 if row.get('page_type') != 'IMAGE')
    return {'Candidates': len(group), 'Valid URLs': summary['expected_urls'],
            'Found': summary['found_urls'], 'Missing': summary['missing_urls'],
            'Unverified': summary['unverified_urls'], 'Not tracked': summary['not_tracked_urls'],
            'Awaiting website': pending, 'Failed / excluded': failed,
            'Coverage %': summary['url_coverage_percent'],
            'Verification %': summary['verification_completion_percent']}


def status_row(area, group, signals, explanation, limited=False):
    counts = count_row(group)
    if not group and not signals:
        status = 'No evidence recorded'
        explanation = f'No {area.lower()} URL evidence was saved in this scan. N/A does not prove the website has none.'
    elif not counts['Valid URLs']:
        status = 'Awaiting website validation' if counts['Awaiting website'] else 'Detected; no measurable URLs'
    elif counts['Not tracked'] == counts['Valid URLs']:
        status = 'Not tracked by Corpus'
    elif counts['Coverage %'] is None:
        status = 'Awaiting Corpus verification'
    elif counts['Unverified'] or counts['Awaiting website'] or counts['Failed / excluded']:
        status = 'Partial analysis'
    else:
        status = 'Observed URLs checked'
    if limited:
        explanation += ' Exploration limits are recorded; completeness is not established.'
    return {'Area': area, **counts, 'Status': status, 'Evidence': explanation}


def discovery_coverage(rows, sections):
    filters = sections.get('filters', [])
    names = learned_names(filters)
    definitions = {}
    saved_paths = defaultdict(list)
    for item in sections.get('pagination', []):
        parameter, template = item.get('parameter_name'), item.get('template_url')
        if parameter and template:
            key = family(template, parameter)
            definitions[key] = dict(item)
            saved_paths[location(template)].append((key, item))
    page_groups = defaultdict(list)
    pagination_rows, filter_rows, archive_rows = [], [], []
    for row in rows:
        url = address(row)
        parameter = pagination_parameter(row)
        key = family(url, parameter[0]) if parameter else None
        if key is None:
            for saved_key, item in saved_paths.get(location(url), []):
                if family(url, item['parameter_name']) == saved_key:
                    key = saved_key
                    break
        if key is not None:
            page_groups[key].append(row)
            if key not in definitions:
                definitions[key] = {'template_url': url, 'parameter_name': parameter[0],
                                    'status': 'OBSERVED', 'max_page': None,
                                    'evidence': 'Pagination URL observed in saved inventory; no boundary proof was recorded.'}
        if key is not None or 'PAGINATION' in _methods(row):
            pagination_rows.append(row)
        if filter_label(row, names) or _methods(row) & {'FILTER_GET', 'FILTER_BROWSER'}:
            filter_rows.append(row)
        if archive_candidate(row):
            archive_rows.append(row)

    pagination = []
    for key, item in sorted(definitions.items()):
        group = page_groups.get(key, [])
        numbers = [(int(value[1]), row) for row in group if (value := pagination_parameter(row))
                   and value[2] == 'Page number']
        missing = [number for number, row in numbers
                   if is_inventory_url(row) and row.get('match_status') == 'NOT_FOUND']
        default_kind = 'Page number' if item['parameter_name'] == '__path__' or item['parameter_name'].lower() in NUMERIC_PAGE_PARAMETERS else 'Observed parameter'
        kind = next((value[2] for row in group if (value := pagination_parameter(row))), default_kind)
        maximum = item.get('max_page') if item.get('status') == 'VERIFIED' else None
        observed = max([item.get('observed_max') or 0, *(n for n, row in numbers if is_inventory_url(row))]) or None
        pagination.append({'Series': item['template_url'], 'Parameter': item['parameter_name'],
                           'Kind': kind, 'Observed maximum': observed if kind == 'Page number' else None,
                           'Website maximum': maximum, 'Maximum verified': maximum is not None,
                           **count_row(group), 'Missing ranges': ', '.join(str(a) if a == b else f'{a}-{b}'
                                                                        for a, b in contiguous_ranges(missing)),
                           'Boundary status': item.get('status', 'OBSERVED'), 'Evidence': item.get('evidence')})

    control_groups = defaultdict(list)
    for item in filters:
        control_groups[(item.get('source_url'), item.get('control_name'), item.get('control_type'))].append(item)
    # A shared site-wide form may be recorded on thousands of source pages.
    # Index URL-backed states once; preserve each control's provenance without
    # reparsing the complete inventory and recounting identical groups per form.
    state_index = defaultdict(list)
    for row in filter_rows:
        values_by_name = defaultdict(set)
        for name, value in parse_qsl(urlsplit(address(row)).query):
            values_by_name[name].add(value)
        for name, values in values_by_name.items():
            state_index[(location(address(row)), name)].append((values, row))
    control_counts = {}
    controls = []
    for (source, name, kind), items in sorted(control_groups.items(), key=lambda pair: str(pair[0])):
        values = {str(item['request_value']) for item in items if item.get('request_value') not in {None, ''}}
        paths = {location(item.get('form_action') or source or '') for item in items}
        signature = (name, frozenset(paths), frozenset(values))
        if signature not in control_counts:
            group = [row for path in paths for state_values, row in state_index.get((path, name), [])
                     if not values or state_values & values]
            control_counts[signature] = count_row(group)
        counts = control_counts[signature]
        controls.append({'Website page': source, 'Control': name, 'Control type': kind,
                         'Method': items[0].get('form_method'), 'Learned values': len(values),
                         **counts, 'Assessment': 'URL-backed filter states' if counts['Candidates'] else
                         'No independent URL state recorded; DOM-only controls and same-page sections cannot be scored as separate pages.'})

    limits = sections.get('limits', [])
    limited = lambda word: any(word in ((x.get('stage') or '') + ' ' + (x.get('reason') or '')).lower() for x in limits)
    verified = sum(item['Maximum verified'] for item in pagination)
    years = {row_archive_year(row) for row in archive_rows} - {None}
    descriptions = [
        status_row('Pagination', pagination_rows, len(pagination),
                   f'{len(pagination)} observed/detected series; {verified} verified numeric boundaries. Coverage is for observed pagination URLs, not every linked article. Unproved maxima remain unknown.', limited('pagination')),
        status_row('Filters', filter_rows, len(controls),
                   f'{len(controls)} recorded controls. Coverage is for independent URL-backed filter states; DOM-only values are not independent pages. Dependent combinations may remain unexplored.', limited('filter')),
        status_row('Archives', archive_rows, len(years),
                   f'{len(years)} evidenced years. Coverage uses URL dates, archive paths or website publication evidence, never Corpus capture dates. Undated article years remain unknown.', limited('archive')),
    ]
    return {'groups': {'Pagination': pagination_rows, 'Filters': filter_rows, 'Archives': archive_rows},
            'status': descriptions, 'pagination': pagination, 'controls': controls, 'filter_names': names}
