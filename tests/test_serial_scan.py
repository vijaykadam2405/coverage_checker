"""End-to-end serial ordering, durable resume, and URL identity regressions."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.validation.normalize import normalize_url
from test_coverage_api import mock_api, response
from test_corpus_text_api import text_result


def serial_fixture(tmp_path, monkeypatch, *, blocked=False, render=False):
    events = []
    fail = [blocked]
    class Website:
        def __init__(self, concurrency, *args, **kwargs):
            assert concurrency == 1
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def wait_turn(self, url): return True
        def observe_response(self, *args): return 0
        async def get(self, url, **kwargs):
            events.append(('website', url))
            html = '<h1>Beginning middle ending</h1>'
            if url.endswith('/'):
                html += '<a href="/child">Child</a><a href="/child#section">Again</a><a href="/child?utm_source=test">Tracked</a><a href="/file.pdf">PDF</a>'
            return FetchResult(url, url, 200, {'content-type': 'text/html'}, html.encode())
    monkeypatch.setattr('crawler_coverage_auditor.auditor.PoliteHttpClient', Website)
    def handler(request):
        body = json.loads(request.content)
        urls = body.get('urls') or [item['url'] for item in body['items']]
        assert len(urls) == 1
        url = urls[0]
        kind = 'presence' if request.url.path == '/v1/held' else 'text'
        events.append((kind, url))
        if fail[0] and kind == 'text':
            return httpx.Response(503)
        if kind == 'text':
            assert not url.endswith('.pdf')
            result = text_result(url, 'Beginning middle ending Child Again Tracked PDF Rendered')
        else:
            result = response(url)
        return httpx.Response(200, json={'results': [result]})
    mock_api(monkeypatch, handler)
    sources = {'STATIC'}
    if render:
        sources.add('RENDERED')
        class Browser:
            error = None
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        monkeypatch.setattr('crawler_coverage_auditor.auditor.BrowserExplorer', lambda *a, **k: Browser())
        async def rendered(auditor, browser, client, *, only_url=None):
            row = dict(auditor.db.report_rows(auditor.run_id, only_url=only_url)[0])
            if row['page_type'] == 'HTML':
                events.append(('render', only_url))
                auditor.db.add_page_snapshot(auditor.run_id, only_url, 'Beginning middle ending Rendered')
            return True
        monkeypatch.setattr(CoverageAuditor, '_crawl_browser', rendered)
    config = AuditConfig(target_url='https://example.test/', corpus_url='https://api.test/',
        database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports',
        respect_robots=False, discovery_sources=sources, use_browser=render,
        max_concurrency=8, corpus_batch_size=100, corpus_interval_seconds=0, max_retries=0)
    return config, events, fail


def test_every_url_is_fully_processed_before_next_fetch_including_render(tmp_path, monkeypatch):
    config, events, _ = serial_fixture(tmp_path, monkeypatch, render=True)
    result = asyncio.run(CoverageAuditor(config).run())
    seed = config.target_url
    child = seed+'child'
    assert events == [('website', seed), ('render', seed), ('presence', seed), ('text', seed),
                      ('website', child), ('render', child), ('presence', child), ('text', child),
                      ('presence', seed+'file.pdf')]
    assert result['status'] == 'COMPLETED'
    with sqlite3.connect(config.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM urls').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM page_snapshots').fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM corpus_matches WHERE text_status='FOUND_FULL'").fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM (SELECT normalized_url FROM urls GROUP BY audit_run_id,normalized_url HAVING COUNT(*)>1)').fetchone()[0] == 0


def test_corpus_failure_pauses_discovery_and_resume_uses_saved_snapshot(tmp_path, monkeypatch):
    config, events, fail = serial_fixture(tmp_path, monkeypatch, blocked=True)
    auditor = CoverageAuditor(config)
    run_id = auditor.run_id
    result = asyncio.run(auditor.run())
    assert result['status'] == 'CORPUS_PAUSED'
    assert events == [('website', config.target_url), ('presence', config.target_url), ('text', config.target_url)]
    with sqlite3.connect(config.database_path) as db:
        assert db.execute('SELECT discovery_completed_at FROM audit_runs').fetchone()[0] is None
        assert db.execute("SELECT state FROM urls WHERE url LIKE '%/child'").fetchone()[0] == 'PENDING'
    fail[0] = False
    result = asyncio.run(CoverageAuditor(config, run_id=run_id).run())
    assert result['status'] == 'LIMITED'
    assert events.count(('website', config.target_url)) == 1
    assert events.count(('presence', config.target_url)) == 1
    assert events[3] == ('text', config.target_url)
    assert events[4] == ('website', config.target_url+'child')


def test_duplicates_merge_evidence_without_legacy_count_limit_or_type_overwrite(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'db.sqlite', max_urls=1)
    auditor = CoverageAuditor(config)
    try:
        assert auditor._add_discovery('https://EXAMPLE.test:443/Page?b=2&a=1#content', 'SITEMAP', 5, None, '')
        url = normalize_url('https://example.test/Page?a=1&b=2')
        auditor.db.add_http_result(auditor.run_id, url, status_code=200, content_type='text/html', validity='VALID_PAGE')
        assert not auditor._add_discovery('https://example.test/Page?utm_source=x&a=1&b=2', 'STATIC', 2, config.target_url, '', 'IMAGE')
        row = dict(auditor.db.report_rows(auditor.run_id)[0])
        assert row['page_type'] == 'HTML' and row['min_depth'] == 2
        assert json.loads(row['discovery_methods']) == ['SITEMAP', 'STATIC']
        assert len(auditor.db.report_rows(auditor.run_id)) == 1
        assert auditor._add_discovery('https://example.test/second', 'STATIC', 3, config.target_url, '')
        assert len(auditor.db.report_rows(auditor.run_id)) == 2
        assert normalize_url('https://example.test/page') != normalize_url('https://example.test/Page')
        assert normalize_url('https://example.test/?page=2') != normalize_url('https://example.test/?page=3')
        assert normalize_url('https://example.test/#/route') != normalize_url('https://example.test/#/other')
    finally:
        auditor.db.close()


def test_missing_api_key_prevents_start_before_creating_inventory(tmp_path, monkeypatch):
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_API_KEY')
    database = tmp_path/'audit.sqlite'
    with pytest.raises(ValueError, match='key is not configured'):
        AuditJobs().start('https://example.test/', database, tmp_path/'reports', 'https://api.test/')
    assert not database.exists()


def test_legacy_pacing_settings_cannot_enable_bulk_or_parallel_workers(tmp_path, monkeypatch):
    jobs = AuditJobs()
    monkeypatch.setattr(jobs, '_launch', lambda config, *args: config)
    config = jobs.start('https://example.test/', tmp_path/'db.sqlite', tmp_path/'reports', 'https://api.test/',
        settings={'max_concurrency': 8, 'corpus_batch_size': 500, 'website_delay_seconds': 0, 'corpus_interval_seconds': 0})
    assert config.max_concurrency == config.corpus_batch_size == 1
    assert config.website_delay_seconds == 1 and config.corpus_interval_seconds == 2
    assert not config.discovery_only


def test_cli_resume_finds_paused_run_even_with_a_saved_completion_timestamp(tmp_path, monkeypatch):
    config, _, _ = serial_fixture(tmp_path, monkeypatch, blocked=True)
    first = CoverageAuditor(config)
    run_id = first.run_id
    assert asyncio.run(first.run())['status'] == 'CORPUS_PAUSED'
    config.resume = True
    resumed = CoverageAuditor(config)
    try:
        assert resumed.run_id == run_id
    finally:
        resumed.db.close()


def test_resume_api_is_explicit_and_read_polling_never_starts_it(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from crawler_coverage_auditor.storage.database import AuditDatabase
    from crawler_coverage_auditor.web.api import create_app
    database = tmp_path/'db.sqlite'
    db = AuditDatabase(database)
    run_id = db.create_run('https://example.test/', 'https://api.test/', {})
    db.finish_run(run_id, 'CORPUS_PAUSED')
    db.close()
    class Jobs:
        calls = []
        def active(self): return None
        def resume(self, selected, *args):
            self.calls.append(selected)
            return SimpleNamespace(run_id=selected, action='discovery', error=None)
    jobs = Jobs()
    client = TestClient(create_app(database, tmp_path/'reports', jobs=jobs))
    assert client.get('/api/state').json()['scan_workflow'] == 'serial-automatic'
    assert client.get(f'/api/runs/{run_id}/summary').status_code == 200
    assert jobs.calls == []
    assert client.post(f'/api/runs/{run_id}/resume', json={}).status_code == 403
    assert client.post(f'/api/runs/{run_id}/resume', json={}, headers={'X-CrawlScope-Client': '1'}).status_code == 202
    assert jobs.calls == [run_id]


def test_pagination_probes_complete_presence_and_text_before_next_website_fetch(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    config, events, _ = serial_fixture(tmp_path, monkeypatch)
    config.discovery_sources.add('PAGINATION')
    async def get(self, url, **kwargs):
        events.append(('website', url))
        number = int(parse_qs(urlsplit(url).query).get('page', ['0'])[0])
        status = 200 if number <= 3 else 404
        html = '<h1>Beginning middle ending</h1><a href="/news?page=2">Next</a>' if number == 0 else f'<h1>Beginning middle ending unique page {number}</h1>' if status == 200 else '<h1>Page not found</h1>'
        return FetchResult(url, url, status, {'content-type': 'text/html'}, html.encode())
    monkeypatch.setattr('crawler_coverage_auditor.auditor.PoliteHttpClient.get', get)
    result = asyncio.run(CoverageAuditor(config).run())
    assert result['unverified_urls'] == 0
    pending = None
    for kind, url in events:
        if kind == 'website':
            assert pending is None, f'{pending} unfinished before {url}'
            number = int(parse_qs(urlsplit(url).query).get('page', ['0'])[0])
            if number <= 3:
                pending = url
        elif kind == 'text':
            assert pending == url
            pending = None
    assert pending is None
    with sqlite3.connect(config.database_path) as db:
        assert db.execute("SELECT max_page FROM pagination_series WHERE status='VERIFIED'").fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM pagination_tasks').fetchone()[0] == 0


def test_dotenv_bootstrap_uses_project_file_without_overwriting_process_settings(monkeypatch):
    import importlib
    from pathlib import Path
    import crawler_coverage_auditor
    calls = []
    monkeypatch.setattr('dotenv.load_dotenv', lambda path, **options: calls.append((path, options)))
    importlib.reload(crawler_coverage_auditor)
    assert calls == [(Path(__file__).resolve().parents[1]/'.env', {'override': False, 'interpolate': False})]
