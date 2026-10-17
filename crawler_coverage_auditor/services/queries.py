"""Bounded local dashboard queries. No target or Corpus network requests."""
from __future__ import annotations

import csv
import io
import json
import math
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from crawler_coverage_auditor.analysis.coverage import FOUND_STATUSES, metrics
from crawler_coverage_auditor.analysis.dashboard import build_analysis, detail_row
from crawler_coverage_auditor.analysis.diagnostics import (
    diagnostic_summary,
    issue_info,
    split_boundaries,
)
from crawler_coverage_auditor.analysis.states import METHOD_LABELS, public_states
from crawler_coverage_auditor.config import (
    CAPTURE_TYPES,
    capture_category,
    resource_subtype,
)
from crawler_coverage_auditor.services.checkpoints import checkpoint_snapshot
from crawler_coverage_auditor.services.discovery_tasks import (
    source_work_remaining,
    task_summary,
)
from crawler_coverage_auditor.services.inventory import (
    _connect_readonly,
    load_report_rows,
    load_run_sections,
)
from crawler_coverage_auditor.services.read_cache import cached_read
from crawler_coverage_auditor.validation.site_session import SiteSessionStore


def _json(value, default=None):
    try:
        return json.loads(value or '{}')
    except (ValueError, TypeError):
        return default if default is not None else {}


def host(url):
    return (urlsplit(url).hostname or '').lower()


def site_key(url):
    parts = urlsplit(url)
    return (parts.hostname or '').lower().removeprefix('www.') + (f':{parts.port}' if parts.port and parts.port not in {80, 443} else '')


def run(database, run_id):
    with closing(_connect_readonly(database)) as connection:
        row = connection.execute('SELECT * FROM audit_runs WHERE id=?', (run_id,)).fetchone()
        remaining = source_work_remaining(connection, run_id) if row else False
    if row is None:
        return None
    result = dict(row)
    result['config'] = _json(result.pop('config_json'))
    result['discovery_work_remaining'] = remaining
    return result


def checkpoint(database, run_id):
    with closing(_connect_readonly(database)) as connection:
        return checkpoint_snapshot(connection, run_id)


def history(database, limit=100):
    if not Path(database).is_file():
        return []
    with closing(_connect_readonly(database)) as connection:
        rows = connection.execute('SELECT * FROM audit_runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) | {'config': _json(row['config_json']), 'discovery_work_remaining': source_work_remaining(connection,row['id'])} for row in rows]


def _cte(connection):
    connection.create_function('url_host', 1, host, deterministic=True)
    connection.create_function('capture_category', 3, capture_category, deterministic=True)
    ucols = {row[1] for row in connection.execute('PRAGMA table_info(urls)')}
    ccols = {row[1] for row in connection.execute('PRAGMA table_info(corpus_matches)')}
    optional = lambda name: f'u.{name}' if name in ucols else f'NULL AS {name}'
    cap = 'u.capture_enabled=1' if 'capture_enabled' in ucols else '1=1'
    hcols = {row[1] for row in connection.execute('PRAGMA table_info(http_results)')}
    attempts = ','.join(f'h.{name}' if name in hcols else f'NULL AS {name}' for name in
                        ('last_attempt_validity', 'last_attempt_error', 'last_attempt_at'))
    provider = ','.join(f'c.{name}' if name in ccols else f'NULL AS {name}' for name in ('provider', 'evidence_json', 'text_status'))
    return f"""WITH inventory AS (
        SELECT u.id,u.url,u.normalized_url,u.canonical_url,u.page_type,u.min_depth,
            {optional('navigation_depth')},{optional('first_discovery_method')},{optional('discovered_at')},
            u.discovery_methods,u.parent_url,u.anchor_text,u.state,
            COALESCE({('u.host' if 'host' in ucols else 'NULL')},url_host(u.normalized_url)) AS host,
            h.status_code,h.final_url,h.content_type,h.validity,h.title,h.h1,h.text_length,
            h.fingerprint,h.error AS http_error,h.fetched_at,{attempts},
            c.match_status,c.corpus_url,c.document_id,c.similarity,c.corpus_text_length,c.checked_at,{provider},
            capture_category(u.page_type,u.normalized_url,COALESCE(h.content_type,'')) AS category,
            CASE WHEN h.validity='VALID_PAGE' OR (h.validity='URL_ONLY' AND u.page_type IN ('DOCUMENT','IMAGE','RESOURCE')) THEN 1 ELSE 0 END AS eligible,
            CASE WHEN c.match_status IN ({','.join(repr(s) for s in sorted(FOUND_STATUSES))}) THEN 1 ELSE 0 END AS found
        FROM urls u LEFT JOIN http_results h ON h.audit_run_id=u.audit_run_id AND h.normalized_url=u.normalized_url
        LEFT JOIN corpus_matches c ON c.audit_run_id=u.audit_run_id AND c.normalized_url=u.normalized_url
        WHERE u.audit_run_id=? AND {cap}) """


def _aggregate(connection, cte, run_id, where='1=1', args=()):
    return dict(connection.execute(cte + f"""SELECT COUNT(*) AS discovered,
        COALESCE(SUM(eligible),0) AS eligible,COALESCE(SUM(eligible*found),0) AS found,
        COALESCE(SUM(eligible AND match_status='NOT_FOUND'),0) AS missing,
        COALESCE(SUM(eligible AND match_status='NOT_TRACKED'),0) AS not_tracked,
        COALESCE(SUM(eligible AND category='HTML'),0) AS html,
        COALESCE(SUM(eligible AND category IN ('PDF','DOCUMENT','IMAGE','MEDIA','OTHER')),0) AS files,
        COUNT(DISTINCT host) AS host_count,
        COALESCE(SUM(validity IS NULL),0) AS pending_website,
        COALESCE(SUM(state='FETCHING'),0) AS fetching,
        COALESCE(SUM(state='RENDERING'),0) AS rendering,
        COALESCE(SUM(state='DONE' AND validity IS NOT NULL),0) AS processed,
        COALESCE(SUM(state='PENDING'),0) AS queued_urls,
        COALESCE(SUM(match_status IN ({','.join(repr(s) for s in sorted(FOUND_STATUSES))},'NOT_FOUND')),0) AS saved_corpus_checks,
        COALESCE(SUM(validity='VALID_PAGE' AND last_attempt_validity IS NOT NULL AND last_attempt_validity!='VALID_PAGE'),0) AS retained_after_retry,
        COALESCE(SUM(eligible=0 AND validity IS NOT NULL AND validity!='RENDER_REQUIRED'),0) AS failed,
        MAX(navigation_depth) AS max_depth FROM inventory WHERE {where}""", (run_id, *args)).fetchone())


def _decorate(row, detail=False):
    row = dict(row)
    row.update(public_states(row))
    row['subtype'] = resource_subtype(row['page_type'], row['url'], row.get('content_type') or '')
    row['methods'] = _json(row.get('discovery_methods'), [])
    row['method_label'] = ', '.join(METHOD_LABELS.get(value, value.replace('_', ' ').title()) for value in row['methods'])
    row['updated_at'] = row.get('checked_at') or row.get('fetched_at') or row.get('discovered_at')
    if detail:
        row['detail'] = detail_row(row) | {'Host': row['host'], 'Subtype': row['subtype'],
            'Normalized URL': row['normalized_url'], 'H1': row.get('h1'), 'Minimum discovery depth': row['min_depth'],
            'Navigation depth': row.get('navigation_depth'), 'Anchor text': row.get('anchor_text'),
            'First discovery source': METHOD_LABELS.get(row.get('first_discovery_method'), 'Not recorded in this historical scan'),
            'Website validation': row['website_label'], 'Corpus checked at': row.get('checked_at'),
            'Live HTML extraction': row['text_label'], 'Corpus HTML completeness': row['text_coverage_label'],
            'Saved text similarity %': row['text_coverage_percent'],
            'Text comparison method': 'Distinct live words present in Corpus (duplicate-resistant; not exact semantic completeness)' if row['text_coverage_method'] == 'unique_word_coverage' else 'Token-set similarity (not exact text completeness)' if row['text_coverage_method'] else 'Not available'}
        if row['text_coverage_method'] == 'unique_word_coverage':
            row['detail']['Saved word coverage %'] = row['detail'].pop('Saved text similarity %')
            row['detail'].pop('Text similarity %', None)
        row['detail']['Depth'] = row.get('navigation_depth')
        row['detail']['Latest website attempt'] = row.get('last_attempt_validity')
        row['detail']['Latest website attempt time'] = row.get('last_attempt_at')
        row['detail']['Latest retry issue'] = row.get('last_attempt_error')
    else:
        for field in ('evidence_json', 'corpus_text_length', 'similarity', 'fingerprint', 'corpus_url', 'document_id', 'h1', 'anchor_text', 'canonical_url', 'final_url'):
            row.pop(field, None)
    return row


def extraction_health(connection, cte, run_id):
    result = dict(connection.execute(cte + """SELECT
        SUM(validity='VALID_PAGE') AS valid_html,
        SUM(validity='VALID_PAGE' AND text_length>0) AS with_text,
        SUM(validity='VALID_PAGE' AND text_length=0) AS zero_text,
        SUM(validity='VALID_PAGE' AND text_length IS NULL) AS unavailable_text,
        SUM(validity='RENDER_REQUIRED') AS render_required
        FROM inventory WHERE page_type='HTML'""", (run_id,)).fetchone())
    result = {key: int(value or 0) for key, value in result.items()}
    lengths = connection.execute(cte + "SELECT text_length FROM inventory WHERE page_type='HTML' AND validity='VALID_PAGE' AND text_length IS NOT NULL ORDER BY text_length LIMIT 2 OFFSET ?",
        (run_id, max(0, (result['valid_html'] - result['unavailable_text'] - 1) // 2))).fetchall()
    count = result['valid_html'] - result['unavailable_text']
    result['median_characters'] = (lengths[0][0] if count % 2 else sum(row[0] for row in lengths) / len(lengths)) if lengths else None
    result['duplicate_fingerprint_groups'] = connection.execute(cte + "SELECT COUNT(*) FROM (SELECT fingerprint FROM inventory WHERE page_type='HTML' AND validity='VALID_PAGE' AND fingerprint IS NOT NULL GROUP BY fingerprint HAVING COUNT(*)>1)", (run_id,)).fetchone()[0]
    result['extraction_rate'] = round(100 * result['with_text'] / result['valid_html'], 2) if count else None
    return result


@cached_read
def summary(database, run_id, compact=False, *, diagnostics=True):
    with closing(_connect_readonly(database)) as connection:
        cte = _cte(connection)
        total = _aggregate(connection, cte, run_id)
        categories = []
        for row in connection.execute(cte + "SELECT category,COUNT(*) AS discovered,SUM(eligible) AS eligible,SUM(eligible*found) AS found,SUM(eligible AND match_status='NOT_FOUND') AS missing,SUM(eligible AND match_status='NOT_TRACKED') AS not_tracked FROM inventory GROUP BY category", (run_id,)):
            categories.append(dict(row) | metrics(row['eligible'], row['found'], row['missing'], row['not_tracked']))
        by_type = {item['category']: item for item in categories}
        for category in sorted(CAPTURE_TYPES):
            by_type.setdefault(category, {'category': category, 'discovered': 0} | metrics(0, 0, 0))
        if compact:
            latest = connection.execute('SELECT * FROM audit_events WHERE audit_run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone() if connection.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events'").fetchone() else None
            return {'counts': {kind: item['eligible'] for kind, item in by_type.items()}, 'html_coverage': by_type['HTML'],
                    'types': by_type,
                    'metrics': total | metrics(total['eligible'], total['found'], total['missing'], total['not_tracked']), 'latest_event': _event(dict(latest)) if latest else None}
        hosts = [dict(row) for row in connection.execute(cte + 'SELECT host,COUNT(*) AS count,SUM(eligible) AS eligible FROM inventory GROUP BY host ORDER BY count DESC,host', (run_id,))]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counters = {}
        for key, table in [('sitemaps', 'sitemaps'), ('pagination_series', 'pagination_series'), ('filters', 'filter_definitions'), ('boundaries', 'audit_limits'), ('errors', 'errors')]:
            counters[key] = connection.execute(f'SELECT COUNT(*) FROM {table} WHERE audit_run_id=?', (run_id,)).fetchone()[0] if table in tables else 0
        counters['browser_pages'] = connection.execute("SELECT COUNT(*) FROM audit_events WHERE audit_run_id=? AND kind='RENDERING'", (run_id,)).fetchone()[0] if 'audit_events' in tables else 0
        counters['warnings'] = connection.execute("SELECT COUNT(*) FROM audit_events WHERE audit_run_id=? AND severity='WARNING'", (run_id,)).fetchone()[0] if 'audit_events' in tables else 0
        stages = [row[0] for row in connection.execute('SELECT DISTINCT stage FROM audit_events WHERE audit_run_id=? ORDER BY stage LIMIT 30', (run_id,))] if 'audit_events' in tables else []
        latest = connection.execute('SELECT * FROM audit_events WHERE audit_run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone() if 'audit_events' in tables else None
        queued = connection.execute(cte + "SELECT COUNT(*) FROM inventory WHERE state='PENDING' AND validity IS NULL", (run_id,)).fetchone()[0]
        processing = [_decorate(row) for row in connection.execute(cte + "SELECT * FROM inventory WHERE state IN ('FETCHING','RENDERING','CHECKING_CORPUS') ORDER BY id DESC LIMIT 3", (run_id,))]
        limits, notes_count = [], 0
        if 'audit_limits' in tables:
            connection.create_function('boundary_impact', 4, lambda stage, reason, url, validity:
                issue_info(stage, reason, url=url, readable=validity == 'VALID_PAGE')[1])
            boundary_sql = """FROM audit_limits l LEFT JOIN http_results h
                ON h.audit_run_id=l.audit_run_id AND h.normalized_url=l.url
                WHERE l.audit_run_id=?"""
            note_condition = "boundary_impact(l.stage,l.reason,l.url,h.validity) IN ('Historical','Diagnostic')"
            notes_count = connection.execute(f'SELECT COUNT(*) {boundary_sql} AND {note_condition}', (run_id,)).fetchone()[0]
            counters['boundaries'] -= notes_count
            limits = [dict(row) for row in connection.execute(f'SELECT l.* {boundary_sql} AND NOT ({note_condition}) ORDER BY l.id DESC LIMIT 20', (run_id,))]
        return {'counts': {kind: item['eligible'] for kind, item in by_type.items()}, 'total': total['eligible'],
                'discovery_tasks': task_summary(connection, run_id),
                'checkpoint': checkpoint_snapshot(connection, run_id),
                'metrics': total | metrics(total['eligible'], total['found'], total['missing'], total['not_tracked']), 'types': by_type,
                'html_coverage': by_type['HTML'], 'hosts': hosts, 'health': extraction_health(connection, cte, run_id) if diagnostics else None,
                'counters': counters | {'queued': queued}, 'latest_event': _event(dict(latest)) if latest else None, 'processing_urls': processing,
                'boundaries': limits, 'notes_count': notes_count, 'activity_stages': stages, 'analysis': {'max_depth': total['max_depth']},
                'public_session_available': SiteSessionStore(Path(os.getenv('CRAWLSCOPE_SESSION_DIR', str(Path(database).parent / 'sessions')))).path(
                    connection.execute('SELECT target_url FROM audit_runs WHERE id=?', (run_id,)).fetchone()[0]).is_file()}


def _filters(filters):
    clauses, args = ['1=1'], []
    for key, column in [('host', 'host'), ('type', 'category'), ('validation_status', 'validity'), ('http_status', 'status_code')]:
        if filters.get(key) not in {None, '', 'ALL', 'all'}:
            if key == 'type' and filters[key] == 'FILES':
                clauses.append("category IN ('PDF','DOCUMENT','IMAGE','MEDIA','OTHER')")
            else:
                clauses.append(f'{column}=?'); args.append(filters[key])
    if filters.get('depth') not in {None, '', 'all'}:
        if filters['depth'] == 'unknown':
            clauses.append('navigation_depth IS NULL')
        else:
            clauses.append('navigation_depth=?'); args.append(filters['depth'])
    if filters.get('method'):
        clauses.append('EXISTS(SELECT 1 FROM json_each(discovery_methods) WHERE value=?)'); args.append(filters['method'])
    if filters.get('search'):
        value = '%' + str(filters['search']).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        clauses.append("(url LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')"); args.extend([value, value])
    status = filters.get('corpus_status')
    statuses = {'found': 'eligible=1 AND found=1', 'missing': "eligible=1 AND match_status='NOT_FOUND'",
                'unverified': "eligible=1 AND found=0 AND COALESCE(match_status,'') NOT IN ('NOT_FOUND','NOT_TRACKED')",
                'not_tracked': "eligible=1 AND match_status='NOT_TRACKED'",
                'failed': "eligible=0 AND validity IS NOT NULL AND validity!='RENDER_REQUIRED'",
                'processing': "state IN ('FETCHING','RENDERING','CHECKING_CORPUS') OR validity='RENDER_REQUIRED'",
                'pending': 'eligible=1 AND match_status IS NULL', 'eligible': 'eligible=1'}
    if status in statuses:
        clauses.append(statuses[status])
    return ' AND '.join(clauses), args


def url_page(database, run_id, filters=None, page=1, page_size=50):
    filters = filters or {}
    where, args = _filters(filters)
    sorts = {'url': 'url', 'host': 'host', 'type': 'category', 'depth': 'navigation_depth',
             'status': 'match_status', 'http': 'status_code', 'updated': 'COALESCE(checked_at,fetched_at,discovered_at)', 'title': 'title'}
    order = 'DESC' if filters.get('order') == 'desc' else 'ASC'
    sort = sorts.get(filters.get('sort'), 'url')
    with closing(_connect_readonly(database)) as connection:
        cte = _cte(connection)
        scoped = _aggregate(connection, cte, run_id, where, args)
        host_where, host_args = _filters({'host': filters.get('host')})
        host_summary = scoped if (where, args) == (host_where, host_args) else _aggregate(connection, cte, run_id, host_where, host_args)
        total = scoped['discovered']; pages = max(1, math.ceil(total / page_size)); page = min(max(1, page), pages)
        rows = connection.execute(cte + f'SELECT * FROM inventory WHERE {where} ORDER BY {sort} {order},id {order} LIMIT ? OFFSET ?',
            (run_id, *args, page_size, (page - 1) * page_size)).fetchall()
    return {'items': [_decorate(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size, 'pages': pages,
            'summary': scoped | metrics(scoped['eligible'], scoped['found'], scoped['missing'], scoped['not_tracked']),
            'host_summary': host_summary | metrics(host_summary['eligible'], host_summary['found'], host_summary['missing'], host_summary['not_tracked'])}


def url_detail(database, run_id, url_id):
    with closing(_connect_readonly(database)) as connection:
        row = connection.execute(_cte(connection) + 'SELECT * FROM inventory WHERE id=?', (run_id, url_id)).fetchone()
        if row is None:
            return None
        result = _decorate(row, True)
        result['boundaries'] = [dict(item) for item in connection.execute('SELECT stage,url,reason FROM audit_limits WHERE audit_run_id=? AND url=? LIMIT 20', (run_id, row['normalized_url']))]
        return result


def export_csv(database, run_id, filters):
    where, args = _filters(filters)
    with closing(_connect_readonly(database)) as connection:
        cursor = connection.execute(_cte(connection) + f'SELECT * FROM inventory WHERE {where} ORDER BY url', (run_id, *args))
        buffer = io.StringIO(); writer = csv.writer(buffer)
        writer.writerow(['URL', 'Host', 'Type', 'Subtype', 'Website status', 'Corpus status', 'Live HTML text', 'Navigation depth', 'Discovery source', 'HTTP', 'Parent URL', 'Text comparison', 'Text similarity %', 'Estimated word coverage %', 'Processed at'])
        yield '\ufeff' + buffer.getvalue(); buffer.seek(0); buffer.truncate()
        for row in cursor:
            item = _decorate(row)
            word_method = item['text_coverage_method'] == 'unique_word_coverage'
            values = [item['url'], item['host'], item['category'], item['subtype'], item['website_label'], item['corpus_label'], item['text_label'], item['navigation_depth'], item['method_label'], item['status_code'], item['parent_url'], item['text_coverage_label'], None if word_method else item['text_coverage_percent'], item['text_coverage_percent'] if word_method else None, item['updated_at']]
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in values])
            yield buffer.getvalue(); buffer.seek(0); buffer.truncate()


def _event(item, later_activity=False, run_status=None):
    item['label'] = item['kind'].replace('_', ' ').title()
    issue, impact, action = issue_info(item['stage'], item['message'], url=item.get('url'))
    if item['severity'] in {'WARNING', 'ERROR'}:
        continuation = 'Later scan activity is recorded; this affected boundary remains uncertain.' if later_activity else 'No later activity recorded yet; check current scan status.'
        if run_status in {'CORPUS_PAUSED','CANCELLED','FAILED'}:
            continuation += ' The scan is currently paused/stopped or needs attention.'
        item.update(issue=issue, impact=impact, next_step=action, continued=continuation)
    # A whitelist avoids exporting credentials/headers from old technical records.
    details = _json(item.pop('details_json', None))
    item['details'] = {key: value for key, value in details.items() if key in {'http_status', 'maximum', 'last_successful_url', 'corpus_status', 'text_status', 'page_content_available'}}
    return item


def activity(database, run_id, limit=80, after_id=0, severity='', stage='', kind=''):
    with closing(_connect_readonly(database)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events'").fetchone():
            return {'items': [], 'cursor': after_id, 'has_more': False}
        clauses, args = ['audit_run_id=?', 'id>?'], [run_id, after_id]
        for field, value in [('severity', severity), ('stage', stage), ('kind', kind)]:
            if value:
                clauses.append(f'{field}=?'); args.append(value)
        order = 'ASC' if after_id else 'DESC'
        rows = connection.execute(f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id {order} LIMIT ?", (*args, limit + 1)).fetchall()
        selected = rows[:limit]
        latest_id = connection.execute('SELECT MAX(id) FROM audit_events WHERE audit_run_id=?', (run_id,)).fetchone()[0] or 0
        run_status = connection.execute('SELECT status FROM audit_runs WHERE id=?', (run_id,)).fetchone()[0]
    return {'items': [_event(dict(row), row['id'] < latest_id, run_status) for row in selected], 'cursor': max([after_id, *(row['id'] for row in selected)]), 'has_more': len(rows) > limit}


def live_urls(database, run_id, limit=20, type='', host_filter='', errors=False, missing=False):
    filters = {'sort': 'updated', 'order': 'desc', 'type': type, 'host': host_filter,
               'corpus_status': 'failed' if errors else 'missing' if missing else 'all'}
    # Live activity only needs recent rows, not two whole-inventory aggregates
    # on every poll. The inventory endpoint still returns its full counters.
    where, args = _filters(filters)
    with closing(_connect_readonly(database)) as connection:
        rows = connection.execute(_cte(connection) + f'''SELECT * FROM inventory WHERE {where}
            ORDER BY COALESCE(checked_at,fetched_at,discovered_at) DESC,id DESC LIMIT ?''',
            (run_id, *args, limit)).fetchall()
    return [_decorate(row) for row in rows]


def sites(database):
    if not Path(database).is_file():
        return []
    # Target sites, not discovered resource/subdomain hosts; no history double count.
    with closing(_connect_readonly(database)) as connection:
        connection.create_function('site_key', 1, site_key, deterministic=True)
        selected = connection.execute('SELECT MAX(id) AS id,site_key(target_url) AS site FROM audit_runs GROUP BY site_key(target_url) ORDER BY id DESC').fetchall()
    return [{'site': row['site'], 'run': run(database, row['id']), 'summary': summary(database, row['id'], compact=True)} for row in selected]


def analysis(database, run_id):
    rows = load_report_rows(database, run_id)
    sections = load_run_sections(database, run_id)
    result = build_analysis(rows, sections, include_details=False)
    result.pop('details', None)
    result['sections_table'] = result.pop('sections')
    with closing(_connect_readonly(database)) as connection:
        cte = _cte(connection)
        result['sources'] = [dict(row) for row in connection.execute(cte + "SELECT COALESCE(first_discovery_method,'Historical first source unknown') AS source,COUNT(*) AS count FROM inventory GROUP BY first_discovery_method ORDER BY count DESC", (run_id,))]
        result['also_sources'] = [dict(row) for row in connection.execute(cte + 'SELECT j.value AS source,COUNT(*) AS count FROM inventory,json_each(discovery_methods) j GROUP BY j.value ORDER BY count DESC', (run_id,))]
        result['http_outcomes'] = [dict(row) for row in connection.execute(cte + "SELECT COALESCE(validity,'Awaiting validation') AS outcome,COUNT(*) AS count FROM inventory GROUP BY validity ORDER BY count DESC", (run_id,))]
        for item in result['sources'] + result['also_sources']:
            item['source_key'] = item['source']
            item['source'] = METHOD_LABELS.get(item['source'], item['source'].replace('_', ' ').title())
        outcomes = {'VALID_PAGE': 'Valid HTML / metadata', 'URL_ONLY': 'File URL collected',
            'RENDER_REQUIRED': 'Needs browser rendering', 'HTTP_ERROR': 'HTTP request failed',
            'ROBOTS_RESTRICTED': 'Robots restriction', 'BOT_BLOCKED': 'Bot restriction',
            'RATE_LIMITED': 'Rate limited', 'LOGIN_REQUIRED': 'Login required', 'SOFT_404': 'Soft 404',
            'REPEATED_PAGE': 'Repeated content', 'FETCH_ERROR': 'Network request failed'}
        for item in result['http_outcomes']:
            item['outcome'] = outcomes.get(item['outcome'], item['outcome'].replace('_', ' ').title())
        result['pagination_probes'] = [dict(row) for row in connection.execute(
            'SELECT series_key,page_number,normalized_url,validity FROM pagination_pages WHERE audit_run_id=? ORDER BY series_key,page_number LIMIT 200', (run_id,))]
        result['subtypes'] = []
        subtype_counts = {}
        for row in connection.execute(cte + 'SELECT page_type,normalized_url,content_type FROM inventory', (run_id,)):
            subtype = resource_subtype(row['page_type'], row['normalized_url'], row['content_type'] or '')
            subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
        result['subtypes'] = [{'Type': key, 'URLs': value} for key, value in sorted(subtype_counts.items(), key=lambda item: -item[1])]
    boundaries, notes = split_boundaries(sections['limits'], rows)
    result.update(sections={key: value[:200] for key, value in sections.items()}, section_totals={key: len(value) for key, value in sections.items()},
                  boundaries=boundaries[:200], notes_count=len(notes), diagnostics=diagnostic_summary(sections['errors'] + sections['limits'], rows))
    current = run(database, run_id)
    comparison = lambda value: {key: value['config'].get(key) for key in (
        'capture_types', 'include_subdomains', 'external_resources', 'discovery_sources', 'corpus_provider',
        'use_browser', 'reuse_public_session', 'respect_robots', 'max_urls', 'max_browser_pages',
        'max_browser_actions', 'max_filter_values', 'maximum_pagination_probe', 'browser_challenge_wait_seconds',
        'max_concurrency', 'website_delay_seconds', 'request_timeout_seconds', 'max_retries')}
    comparable = [item for item in history(database) if site_key(item['target_url']) == site_key(current['target_url']) and comparison(item) == comparison(current) and item['corpus_url'] == current['corpus_url']]
    result['history'] = [{'Run': item['id'], 'Started': item['started_at'], **{key: value for key, value in summary(database, item['id'], compact=True)['metrics'].items() if key in {'discovered', 'eligible', 'found', 'missing', 'unverified', 'verified_coverage'}}} for item in comparable[:12]]
    return result
