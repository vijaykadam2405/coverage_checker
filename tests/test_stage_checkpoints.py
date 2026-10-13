"""Pause/restart fixtures using local transports only, never production services."""
import asyncio
import sqlite3
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.discovery.sitemap import discover_sitemaps
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.services.checkpoints import ScanProgress
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.web.api import create_app
from test_serial_scan import serial_fixture
from test_react_api import Jobs


@pytest.mark.parametrize('pause_after', ['WEBSITE', 'BROWSER', 'COMPLETED'])
def test_pause_resume_reuses_finished_stages_and_completed_urls(tmp_path, monkeypatch, pause_after):
    config, events, _ = serial_fixture(tmp_path, monkeypatch, render=True)
    stopped = [False]; paused = [False]
    original = ScanProgress.mark
    def mark(self, url, stage):
        original(self, url, stage)
        if url == config.target_url and stage == pause_after and not paused[0]:
            stopped[0] = paused[0] = True
    monkeypatch.setattr(ScanProgress, 'mark', mark)
    first = CoverageAuditor(config, stopped=lambda: stopped[0]); run = first.run_id
    assert asyncio.run(first.run())['status'] == 'CANCELLED'
    checkpoint = queries.checkpoint(config.database_path, run)
    assert checkpoint['available'] and checkpoint['paused_at'] and checkpoint['pause_reason'] == 'USER_PAUSE'
    assert checkpoint['stage'] == {'WEBSITE': 'BROWSER', 'BROWSER': 'CORPUS_URL', 'COMPLETED': 'QUEUE'}[pause_after]
    assert events.count(('website', config.target_url)) == 1
    stopped[0] = False; config.resume = True
    resumed = CoverageAuditor(config, run_id=run)
    assert asyncio.run(resumed.run())['status'] == 'COMPLETED'
    for operation in ('website', 'render', 'presence', 'text'):
        assert events.count((operation, config.target_url)) == 1
        assert events.count((operation, config.target_url+'child')) == 1
    assert events.count(('presence', config.target_url+'file.pdf')) == 1
    assert not any(op in {'website','text'} and url.endswith('.pdf') for op, url in events)
    checkpoint = queries.checkpoint(config.database_path, run)
    assert not checkpoint['paused_at'] and checkpoint['current_url'] is None
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute('SELECT count(*) FROM url_checkpoints WHERE completed=1').fetchone()[0] == 3
        assert connection.execute('SELECT count(*) FROM urls').fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM audit_limits WHERE stage='Historical pause'").fetchone()[0] == 1


def test_partial_browser_resume_reopens_only_browser_not_completed_website(tmp_path, monkeypatch):
    config, events, _ = serial_fixture(tmp_path, monkeypatch, render=True)
    render = CoverageAuditor._crawl_browser; stopped = [False]; partial = [True]
    async def rendering(auditor, browser, client, *, only_url=None):
        if partial[0]:
            events.append(('partial-render', only_url)); stopped[0] = True
            return False
        return await render(auditor, browser, client, only_url=only_url)
    monkeypatch.setattr(CoverageAuditor, '_crawl_browser', rendering)
    first = CoverageAuditor(config, stopped=lambda: stopped[0]); run = first.run_id
    assert asyncio.run(first.run())['status'] == 'CANCELLED'
    assert queries.checkpoint(config.database_path, run)['current_url'] == config.target_url
    partial[0] = stopped[0] = False; config.resume = True
    assert asyncio.run(CoverageAuditor(config, run_id=run).run())['status'] == 'COMPLETED'
    assert events.count(('website', config.target_url)) == 1
    assert events.count(('partial-render', config.target_url)) == 1
    assert events.count(('render', config.target_url)) == 1
    assert events.count(('presence', config.target_url)) == 1


def test_database_reopen_after_crash_keeps_inflight_cursor_ahead_of_shallower_urls(tmp_path):
    path = tmp_path/'audit.sqlite'; db = AuditDatabase(path)
    run = db.create_run('https://example.test/', 'https://api.test/', {})
    for url, depth in [('https://example.test/deep', 10), ('https://example.test/new', 1)]:
        db.add_url(run, DiscoveredURL(url, url, 'STATIC', depth))
    progress = ScanProgress(db, run)
    progress.enter('https://example.test/deep', 'WEBSITE')
    progress.mark('https://example.test/deep', 'WEBSITE')
    db.set_url_state(run, 'https://example.test/deep', 'RENDERING')
    db.close()  # Simulate loss of the process, without writing a pause record.
    db = AuditDatabase(path); progress = ScanProgress(db, run)
    try:
        progress.restore()
        assert progress.preferred_pending()['normalized_url'] == 'https://example.test/deep'
        assert progress.url('https://example.test/deep')['website_completed']
        assert not progress.url('https://example.test/deep')['browser_completed']
        # Completing another saved Corpus check must not erase the interrupted cursor.
        progress.mark('https://example.test/new', 'COMPLETED')
        assert progress.state()['current_url'] == 'https://example.test/deep'
    finally:
        db.close()


def test_corpus_pause_resumes_text_only_without_repeating_presence_or_fetch(tmp_path, monkeypatch):
    config, events, fail = serial_fixture(tmp_path, monkeypatch, blocked=True)
    first = CoverageAuditor(config); run = first.run_id
    assert asyncio.run(first.run())['status'] == 'CORPUS_PAUSED'
    checkpoint = queries.checkpoint(config.database_path, run)
    assert checkpoint['stage'] == 'CORPUS_TEXT' and checkpoint['current_url'] == config.target_url
    assert checkpoint['pause_reason'] == 'CORPUS_UNAVAILABLE'
    fail[0] = False; config.resume = True
    assert asyncio.run(CoverageAuditor(config, run_id=run).run())['status'] == 'LIMITED'
    assert events.count(('website', config.target_url)) == events.count(('presence', config.target_url)) == 1
    assert events.count(('text', config.target_url)) == 2
    assert events[3] == ('text', config.target_url)


@pytest.mark.parametrize('changed_sitemap', [False, True])
def test_sitemap_resume_skips_completed_files_and_resumes_entry_cursor(tmp_path, changed_sitemap):
    db = AuditDatabase(tmp_path/'audit.sqlite'); run = db.create_run('https://example.test/', 'https://api.test/', {})
    progress = ScanProgress(db, run); stop = [False]; requests = []; captured = []
    children = ['first', 'second', 'third']
    class Website:
        async def get(self, url, **kwargs):
            requests.append(url)
            if url.endswith('robots.txt'):
                return FetchResult(url,url,200,{},b'')
            if url.endswith('sitemap.xml'):
                body = '<sitemapindex><sitemap><loc>https://example.test/child.xml</loc></sitemap></sitemapindex>'
            elif url.endswith('child.xml'):
                body = '<urlset>'+''.join(f'<url><loc>https://example.test/{name}</loc></url>' for name in children)+'</urlset>'
            else:
                return FetchResult(url,url,404,{},b'')
            return FetchResult(url,url,200,{'content-type':'application/xml'},body.encode())
    def on_url(url, parent, _):
        captured.append(url)
        db.add_url(run, DiscoveredURL(url,url,'SITEMAP',1,parent))
        if url.endswith('/first') and len(captured) == 1: stop[0] = True
    callbacks = dict(on_sitemap=lambda url,parent,kind,count: db.add_sitemap(run,url,parent,kind,count), on_url=on_url, stopped=lambda: stop[0], progress=progress)
    asyncio.run(discover_sitemaps('https://example.test/', Website(), **callbacks))
    assert captured == ['https://example.test/first']
    assert not progress.state()['sitemap_completed']
    db.close(); db = AuditDatabase(tmp_path/'audit.sqlite'); progress = ScanProgress(db,run)
    callbacks['progress'] = progress; stop[0] = False
    if changed_sitemap: children.insert(0,'inserted')
    try:
        asyncio.run(discover_sitemaps('https://example.test/', Website(), **callbacks))
        counts = Counter(requests)
        assert counts['https://example.test/sitemap.xml'] == 1
        assert counts['https://example.test/robots.txt'] == 1
        assert counts['https://example.test/child.xml'] == 2  # Only incomplete file reopens.
        assert progress.state()['sitemap_completed']
        assert db.connection.execute('SELECT count(*) FROM urls').fetchone()[0] == len(children)
        assert db.connection.execute('SELECT url_count FROM sitemaps WHERE sitemap_url=?',
            ('https://example.test/child.xml',)).fetchone()[0] == len(children)
        assert captured.count('https://example.test/first') == (2 if changed_sitemap else 1)
        before = requests.copy()
        asyncio.run(discover_sitemaps('https://example.test/', Website(), **callbacks))
        assert requests == before
    finally:
        db.close()


def test_probe_cache_is_durable_html_only_and_excludes_cookie_headers(tmp_path):
    from types import SimpleNamespace
    path = tmp_path/'audit.sqlite'; db = AuditDatabase(path); run = db.create_run('https://example.test/', 'https://api.test/', {})
    progress = ScanProgress(db,run)
    progress.save_probe(SimpleNamespace(status_code=200, text='Incomplete adapter metadata'))
    good = FetchResult('https://example.test/news?page=2','https://example.test/news?page=2',200,
        {'content-type':'text/html','set-cookie':'secret-cookie'},b'<h1>Saved article</h1>', captured_at='2026-10-01T00:00:00+00:00')
    progress.save_probe(good)
    for suffix, code, mime, url_only in [('/file.pdf',200,'application/pdf',True),('/bad',429,'text/html',False),('/feed',200,'application/json',False)]:
        url = 'https://example.test'+suffix
        progress.save_probe(FetchResult(url,url,code,{'content-type':mime},b'not-a-public-html-probe',url_only=url_only))
        assert progress.probe(url) is None
    db.close(); db = AuditDatabase(path); progress = ScanProgress(db,run)
    try:
        response = progress.probe(good.requested_url)
        assert response.content == good.content and response.status_code == 200
        assert response.captured_at == good.captured_at
        assert 'set-cookie' not in response.headers
        assert db.connection.execute('SELECT count(*) FROM pagination_probe_checkpoints').fetchone()[0] == 1
    finally:
        db.close()


def test_checkpoint_api_is_readonly_and_does_not_expose_cached_html(tmp_path):
    path = tmp_path/'audit.sqlite'; db = AuditDatabase(path); run = db.create_run('https://example.test/', 'https://api.test/', {})
    progress = ScanProgress(db,run)
    progress.enter('https://example.test/article','BROWSER'); progress.pause('USER_PAUSE')
    progress.save_probe(FetchResult('https://example.test/?page=2','https://example.test/?page=2',200,{'content-type':'text/html'},b'private-html-marker'))
    db.close()
    jobs = Jobs(); client = TestClient(create_app(path,tmp_path/'reports',jobs=jobs))
    response = client.get(f'/api/runs/{run}/checkpoint')
    assert response.status_code == 200 and response.json()['stage'] == 'BROWSER'
    assert 'private-html-marker' not in response.text
    assert client.get(f'/api/runs/{run}/summary').json()['checkpoint'] == response.json()
    assert jobs.active() is None
    assert client.get('/api/runs/999/checkpoint').status_code == 404


def test_mid_pagination_pause_reuses_completed_probe_fetches_after_restart(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    config, events, _ = serial_fixture(tmp_path, monkeypatch)
    config.discovery_sources.add('PAGINATION')
    stopped = [False]; interrupted = [False]
    async def get(self, url, **kwargs):
        events.append(('website', url))
        number = int(parse_qs(urlsplit(url).query).get('page', ['0'])[0])
        status = 200 if number <= 7 else 404
        body = '<h1>Beginning middle ending</h1><a href="/news?page=2">Next</a>' if not number else f'<h1>Meaningful distinct article number {number}</h1>' if status == 200 else '<h1>Page not found</h1>'
        if number == 4 and not interrupted[0]:
            interrupted[0] = stopped[0] = True
        return FetchResult(url, url, status, {'content-type': 'text/html'}, body.encode())
    monkeypatch.setattr('crawler_coverage_auditor.auditor.PoliteHttpClient.get', get)
    first = CoverageAuditor(config, stopped=lambda: stopped[0]); run = first.run_id
    assert asyncio.run(first.run())['status'] == 'CANCELLED'
    assert interrupted[0]
    stopped[0] = False; config.resume = True
    # This fixture intentionally disables rendered discovery; that boundary stays.
    assert asyncio.run(CoverageAuditor(config, run_id=run).run())['status'] == 'LIMITED'
    requests = Counter(url for operation, url in events if operation == 'website')
    assert requests[config.target_url] == 1
    assert requests[config.target_url+'news?page=4'] == 1
    assert all(count == 1 for count in requests.values())
    with sqlite3.connect(config.database_path) as db:
        assert db.execute("SELECT max_page FROM pagination_series WHERE status='VERIFIED'").fetchone()[0] == 7
        assert db.execute('SELECT count(*) FROM pagination_tasks').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM urls WHERE state!='DONE'").fetchone()[0] == 0


def test_crash_between_website_done_and_stage_marker_never_checks_partial_html_first(tmp_path):
    path = tmp_path/'audit.sqlite'; db = AuditDatabase(path); run = db.create_run('https://example.test/', 'https://api.test/', {})
    url = 'https://example.test/'
    db.add_url(run, DiscoveredURL(url,url,'SEED',0))
    progress = ScanProgress(db,run); progress.enter(url,'WEBSITE')
    db.add_http_result(run,url,validity='VALID_PAGE',status_code=200)
    db.set_url_state(run,url,'DONE'); db.close()
    db = AuditDatabase(path); progress = ScanProgress(db,run)
    try:
        progress.restore()
        assert progress.preferred_pending()['normalized_url'] == url
        assert db.connection.execute('SELECT state FROM urls').fetchone()[0] == 'PENDING'
        assert not progress.url(url)['website_completed']
    finally:
        db.close()
