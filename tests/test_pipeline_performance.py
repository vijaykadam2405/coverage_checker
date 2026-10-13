"""Regression coverage for local overhead; no production website/Corpus calls."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from crawler_coverage_auditor.analysis import discovery_coverage as areas
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.browser import BrowserDiscovery
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.services.inventory import load_report_rows
from crawler_coverage_auditor.services.revision import BackendRevision
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.validation.publication import enrich_archive_rows


def test_discovery_checkpoints_reduce_commits_without_losing_urls_edges_or_events(tmp_path):
    db = AuditDatabase(tmp_path/'audit.sqlite')
    run = db.create_run('https://example.test/', 'https://api.test/', {})
    trace = []
    db.connection.set_trace_callback(trace.append)
    try:
        with db.discovery_batch():
            for n in range(250):
                url = f'https://example.test/{n}'
                assert db.add_url(run, DiscoveredURL(url, url, 'STATIC', 2, 'https://example.test/', str(n)))
                db.event(run, 'Website', 'DISCOVERED', 'Collected', url)
        assert sum(sql == 'COMMIT' for sql in trace) == 5  # Previously 500.
        for table in ('urls', 'url_edges', 'audit_events'):
            assert db.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 250
        assert not db.connection.in_transaction
        db.connection.set_trace_callback(None)
        assert not db.add_url(run, DiscoveredURL('https://example.test/0', 'https://example.test/0', 'JS_DISCOVERY', 1, 'https://example.test/other'))
        row = db.connection.execute("SELECT * FROM urls WHERE normalized_url='https://example.test/0'").fetchone()
        assert set(json.loads(row['discovery_methods'])) == {'STATIC', 'JS_DISCOVERY'}
        assert row['navigation_depth'] == 1
        assert db.connection.execute('SELECT count(*) FROM url_edges').fetchone()[0] == 251
    finally:
        db.close()


def test_error_keeps_prior_checkpoints_rolls_back_chunk_and_leaves_source_resumable(tmp_path):
    db = AuditDatabase(tmp_path/'audit.sqlite')
    run = db.create_run('https://example.test/', 'https://api.test/', {})
    db.add_url(run, DiscoveredURL('https://example.test/', 'https://example.test/', 'SEED', 0))
    db.set_url_state(run, 'https://example.test/', 'FETCHING')
    try:
        with pytest.raises(RuntimeError):
            with db.discovery_batch():
                for n in range(125):
                    url = f'https://example.test/{n}'
                    db.add_url(run, DiscoveredURL(url, url, 'STATIC', 1))
                raise RuntimeError('Interrupted extraction')
        assert db.connection.execute('SELECT count(*) FROM urls').fetchone()[0] == 101
        assert db.connection.execute("SELECT state FROM urls WHERE normalized_url='https://example.test/'").fetchone()[0] == 'FETCHING'
        assert not db.connection.in_transaction
        db.event(run, 'Website', 'RESUMED', 'Normal writes still commit')
        other = sqlite3.connect(tmp_path/'audit.sqlite')
        assert other.execute('SELECT count(*) FROM audit_events').fetchone()[0] == 1
        other.close()
    finally:
        db.close()


def test_shared_controls_are_indexed_once_with_identical_counts_and_provenance(monkeypatch):
    rows = [dict(url=f'https://example.test/news?year={2020+n%3}&category={n%2}',
                 normalized_url=f'https://example.test/news?year={2020+n%3}&category={n%2}',
                 page_type='HTML', validity='VALID_PAGE', match_status='NOT_FOUND', discovery_methods='["FILTER_GET"]') for n in range(6)]
    filters = [dict(source_url=f'https://example.test/article/{n}', form_action='https://example.test/news',
                    control_name='year', control_type='select', form_method='get', request_value=str(year))
               for n in range(500) for year in (2020, 2021, 2022)]
    original = areas.count_row
    calls = []
    def counted(group):
        calls.append(len(group))
        return original(group)
    monkeypatch.setattr(areas, 'count_row', counted)
    result = areas.discovery_coverage(rows, {'filters': filters})
    assert len(result['controls']) == 500
    assert len(calls) == 4  # One matching group, plus three area assessments.
    assert len({c['Website page'] for c in result['controls']}) == 500
    assert all(c['Valid URLs'] == c['Missing'] == 6 and c['Learned values'] == 3 for c in result['controls'])


def test_filter_index_handles_repeated_parameters_and_distinct_value_dependencies():
    rows = [dict(url='https://example.test/news?category=A&category=B', page_type='HTML', validity='VALID_PAGE', match_status='NOT_FOUND'),
            dict(url='https://other.test/news?category=A', page_type='HTML', validity='VALID_PAGE', match_status='EXACT_MATCH')]
    filters = [dict(source_url='https://example.test/start', form_action='https://example.test/news', control_name='category', control_type='select', request_value=v) for v in ('A', 'B')]
    result = areas.discovery_coverage(rows, {'filters': filters})
    assert result['controls'][0]['Candidates'] == 1  # Not counted twice for two values.
    assert result['controls'][0]['Missing'] == 1


def test_archive_enrichment_loads_only_necessary_historical_article_snapshots():
    saved = json.dumps({'website_publication': {'date': '2021-01-01', 'year': '2021', 'source': 'website publication metadata'}})
    rows = [dict(normalized_url='https://example.test/news/a-proper-article-title', page_type='HTML', validity='VALID_PAGE', content_evidence_json=saved),
            dict(normalized_url='https://example.test/contact', page_type='HTML', validity='VALID_PAGE'),
            dict(normalized_url='https://example.test/news/another-proper-article-title', page_type='HTML', validity='HTTP_ERROR'),
            dict(normalized_url='https://example.test/news/old-useful-article-heading', h1='Old useful article heading', page_type='HTML', validity='VALID_PAGE')]
    reads = []
    def snapshot(url):
        reads.append(url)
        return '06/10/2020 10:20 AM Old useful article heading Some body content'
    enriched = enrich_archive_rows(rows, snapshot)
    assert reads == [rows[-1]['normalized_url']]
    assert json.loads(enriched[-1]['content_evidence_json'])['website_publication']['year'] == '2020'
    assert all('cleaned_text' not in row for row in enriched)


def test_live_url_reads_skip_full_inventory_aggregates(tmp_path, monkeypatch):
    from test_react_api import make_inventory
    path = tmp_path/'audit.sqlite'; run = make_inventory(path)
    expected = queries.url_page(path, run, {'sort': 'updated', 'order': 'desc', 'corpus_status': 'all'}, page_size=20)['items']
    monkeypatch.setattr(queries, '_aggregate', lambda *args, **kwargs: pytest.fail('Live rows must not aggregate the whole inventory'))
    assert queries.live_urls(path, run) == expected
    assert all(row['match_status'] == 'NOT_FOUND' for row in queries.live_urls(path, run, missing=True))


def test_unchanged_static_filters_skip_rewrites_but_new_values_and_dependencies_are_explored(tmp_path):
    auditor = CoverageAuditor(AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite'))
    html = '<form action="/news" method="get"><select name="year"><option value="2020">2020</option></select></form>'
    try:
        auditor._analyze_static_filters(html, 'https://example.test/news?category=A', 1)
        changes = auditor.db.connection.total_changes
        auditor._analyze_static_filters(html, 'https://example.test/news?category=A', 1)
        assert auditor.db.connection.total_changes == changes
        auditor._analyze_static_filters(html, 'https://example.test/news?category=B', 1)
        auditor._analyze_static_filters(html.replace('</select>', '<option value="2021">2021</option></select>'), 'https://example.test/news?category=A', 1)
        urls = {row['normalized_url'] for row in auditor.db.report_rows(auditor.run_id)}
        assert urls == {'https://example.test/news?category=A&year=2020', 'https://example.test/news?category=B&year=2020', 'https://example.test/news?category=A&year=2021'}
        auditor._analyze_static_filters(html, 'https://example.test/news?category=A', 0)
        assert auditor.db.connection.execute('SELECT min_depth FROM urls WHERE normalized_url=?',
            ('https://example.test/news?category=A&year=2020',)).fetchone()[0] == 1
    finally:
        auditor.db.close()


def test_source_remains_fetching_until_every_static_link_is_persisted(tmp_path, monkeypatch):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite', discovery_sources={'STATIC'}, respect_robots=False)
    auditor = CoverageAuditor(config)
    try:
        auditor._add_discovery(config.target_url, 'SEED', 0, None, '')
        row = auditor.db.report_rows(auditor.run_id)[0]
        original = auditor._add_discovery
        def interrupted(*args, **kwargs):
            state = auditor.db.connection.execute('SELECT state FROM urls WHERE normalized_url=?', (config.target_url,)).fetchone()[0]
            assert state == 'FETCHING'
            original(*args, **kwargs)
            raise RuntimeError('Interrupted discovery')
        monkeypatch.setattr(auditor, '_add_discovery', interrupted)
        response = FetchResult(config.target_url, config.target_url, 200, {'content-type': 'text/html'}, b'<h1>Meaningful complete content</h1><a href="/child">Child</a>')
        with pytest.raises(RuntimeError):
            asyncio.run(auditor._process_url(SimpleNamespace(), row, response=response))
        assert auditor.db.connection.execute('SELECT state FROM urls WHERE normalized_url=?', (config.target_url,)).fetchone()[0] == 'FETCHING'
    finally:
        auditor.db.close()


def test_backend_revision_detects_source_changes_not_secrets_or_audit_data(tmp_path):
    package = tmp_path/'crawler_coverage_auditor'; package.mkdir()
    code = package/'example.py'; code.write_text('value = 1\n')
    now = [0.0]
    revision = BackendRevision(tmp_path, clock=lambda: now[0])
    assert not revision.update_available()
    (tmp_path/'.env').write_text('SECRET=not-to-be-exposed')
    (tmp_path/'coverage.sqlite').write_text('runtime change')
    now[0] = 5
    assert not revision.update_available()
    code.write_text('value = 100\n'); now[0] = 10
    assert revision.update_available()
    assert 'not-to-be-exposed' not in revision.loaded
    assert not BackendRevision(tmp_path).update_available()


def test_current_publication_evidence_does_not_load_snapshot_text_in_reports(tmp_path, monkeypatch):
    import crawler_coverage_auditor.services.inventory as inventory
    db = AuditDatabase(tmp_path/'audit.sqlite')
    run = db.create_run('https://example.test/', 'https://api.test/', {})
    url = 'https://example.test/news/an-article-with-useful-heading'
    db.add_url(run, DiscoveredURL(url, url, 'STATIC', 1))
    db.add_http_result(run, url, validity='VALID_PAGE', h1='An article with useful heading')
    db.add_page_snapshot(run, url, 'Large saved text not needed by this report')
    db.content_evidence(run, url, {'website_publication': {'date': '2021-01-01', 'year': '2021', 'source': 'website publication metadata'}})
    trace = []; db.connection.set_trace_callback(trace.append)
    try:
        rows = db.report_rows(run)
        assert rows[0]['normalized_url'] == url
        assert not any('FROM page_snapshots' in sql for sql in trace)
    finally:
        db.close()
    connect = inventory._connect_readonly
    def traced(path):
        connection = connect(path); connection.set_trace_callback(trace.append)
        return connection
    monkeypatch.setattr(inventory, '_connect_readonly', traced)
    trace.clear(); assert len(load_report_rows(tmp_path/'audit.sqlite', run)) == 1
    assert not any('FROM page_snapshots' in sql for sql in trace)


def test_rendered_text_parses_once_for_multiple_script_notes_and_source_stays_resumable(tmp_path, monkeypatch):
    from crawler_coverage_auditor.discovery.static_links import Link
    from crawler_coverage_auditor.validation.text_extract import extract_text
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite', respect_robots=False, discovery_sources={'STATIC', 'RENDERED'})
    auditor = CoverageAuditor(config); auditor._browser_user_agent = 'Fixture browser'
    class Context:
        async def cookies(self, urls): return []
    class Browser:
        context = Context()
        async def explore(self, url):
            return BrowserDiscovery((Link(url+'child', 'Child', 'a'),), (),
                html='<h1>Complete meaningful rendered content</h1>', final_url=url, http_status=200,
                script_errors=tuple({'url': url, 'message': f'Optional script {n}', 'stack': ''} for n in range(3)))
    reads = []
    def counted(html):
        reads.append(1); return extract_text(html)
    monkeypatch.setattr('crawler_coverage_auditor.auditor.extract_text', counted)
    original = auditor._add_discovery
    def checked(*args, **kwargs):
        assert auditor.db.connection.execute('SELECT state FROM urls WHERE normalized_url=?', (config.target_url,)).fetchone()[0] == 'RENDERING'
        return original(*args, **kwargs)
    try:
        original(config.target_url, 'SEED', 0, None, '')
        auditor.db.add_http_result(auditor.run_id, config.target_url, validity='VALID_PAGE')
        monkeypatch.setattr(auditor, '_add_discovery', checked)
        client = SimpleNamespace(browser_cookies=lambda url: [], use_browser_session=lambda *args: None)
        assert asyncio.run(auditor._crawl_browser(Browser(), client, only_url=config.target_url))
        assert len(reads) == 1
        assert auditor.db.connection.execute("SELECT count(*) FROM audit_events WHERE kind='JAVASCRIPT_DIAGNOSTIC'").fetchone()[0] == 3
        assert auditor.db.connection.execute('SELECT state FROM urls WHERE normalized_url=?', (config.target_url,)).fetchone()[0] == 'DONE'
    finally:
        auditor.db.close()
