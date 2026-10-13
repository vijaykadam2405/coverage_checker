"""Discovery performance regressions against controlled pages, never live services."""
import asyncio
from types import SimpleNamespace

import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.browser import BrowserDiscovery, BrowserExplorer
from crawler_coverage_auditor.discovery.readiness import ContentReadiness
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.validation.text_extract import extract_text


class Request:
    def __init__(self, url, kind='fetch'):
        self.url, self.resource_type = url, kind


@pytest.mark.parametrize('sample_request, tracked', [
    (Request('https://www.google-analytics.com/g/collect'), False),
    (Request('https://www.google-analytics.com/api/articles'), True),
    (Request('https://www.google-analytics.com/script.js', 'script'), True),
    (Request('https://example.test/analytics'), True),
    (Request('https://api.example.test/data'), True),
    (Request('https://external-api.test/data'), True),
    (Request('https://example.test/image.jpg', 'image'), False),
])
def test_readiness_ignores_only_non_content_or_known_external_telemetry(sample_request, tracked):
    readiness = ContentReadiness('https://example.test/')
    readiness.started(sample_request)
    assert bool(readiness.pending) == tracked
    readiness.finished(sample_request)
    assert not readiness.pending


def test_analytics_website_itself_is_not_excluded_from_readiness():
    readiness = ContentReadiness('https://www.google-analytics.com/')
    request = Request('https://google-analytics.com/api/articles')
    readiness.started(request)
    assert request in readiness.pending
    readiness = ContentReadiness('https://news.google-analytics.com/')
    sibling = Request('https://api.google-analytics.com/articles')
    readiness.started(sibling)
    assert sibling in readiness.pending


def test_readiness_keeps_waiting_for_slow_real_content_not_background_traffic():
    now = [0.0]
    readiness = ContentReadiness('https://example.test/', clock=lambda: now[0])
    data = Request('https://example.test/api/articles')
    readiness.started(data)
    readiness.started(Request('https://www.google-analytics.com/g/collect'))
    class Page:
        async def wait_for_timeout(self, milliseconds):
            now[0] += milliseconds / 1000
            if now[0] >= .8:
                readiness.finished(data)
    assert asyncio.run(readiness.wait(Page()))
    assert 1.3 <= now[0] < 1.4  # Real body completion plus the full 500ms quiet period.


def test_readiness_honors_cancellation_and_discloses_unfinished_content():
    now = [0.0]
    readiness = ContentReadiness('https://example.test/', clock=lambda: now[0])
    request = Request('https://example.test/api/slow')
    readiness.started(request)
    class Page:
        async def wait_for_timeout(self, milliseconds):
            now[0] += milliseconds / 1000
    assert not asyncio.run(readiness.wait(Page(), stopped=lambda: now[0] >= .3))
    assert now[0] < .4 and request in readiness.pending
    assert not asyncio.run(readiness.wait(Page(), timeout=.5))
    assert request in readiness.pending


def test_http_response_is_decoded_once_and_child_discovery_is_preserved(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite',
        respect_robots=False, discovery_sources={'STATIC'})
    auditor = CoverageAuditor(config)
    class CountingResponse(FetchResult):
        reads = 0
        @property
        def text(self):
            self.reads += 1
            return super().text
    response = CountingResponse(config.target_url, config.target_url, 200, {'content-type': 'text/html'},
        b'<main><h1>Valid article</h1><p>Meaningful article text.</p><a href="/child">Child</a><a href="/report.pdf">Report</a></main>')
    try:
        auditor._add_discovery(config.target_url, 'SEED', 0, None, '')
        row = dict(auditor.db.report_rows(auditor.run_id)[0])
        asyncio.run(auditor._process_url(SimpleNamespace(), row, response=response))
        assert response.reads == 1
        assert {row['url'] for row in auditor.db.report_rows(auditor.run_id)} == {
            config.target_url, config.target_url+'child', config.target_url+'report.pdf'}
        assert 'Meaningful article text.' in auditor.db.page_snapshot(auditor.run_id, config.target_url)
    finally:
        auditor.db.close()


def test_pending_queue_uses_depth_ordered_index_and_duplicates_do_not_rewrite_urls(tmp_path):
    db = AuditDatabase(tmp_path/'audit.sqlite')
    try:
        run_id = db.create_run('https://example.test/', 'https://api.test/', {})
        item = DiscoveredURL('https://example.test/child', 'https://example.test/child', 'STATIC', 3,
            'https://example.test/', 'Child')
        assert db.add_url(run_id, item)
        changes = db.connection.total_changes
        assert not db.add_url(run_id, item)
        assert db.connection.total_changes == changes
        plan = ' '.join(row['detail'] for row in db.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM urls WHERE audit_run_id=? AND state='PENDING' ORDER BY min_depth,id LIMIT 1", (run_id,)))
        assert 'urls_run_pending_depth' in plan and 'TEMP B-TREE' not in plan
        shallow = DiscoveredURL('https://example.test/shallow', 'https://example.test/shallow', 'STATIC', 1)
        db.add_url(run_id, shallow)
        assert db.pending_urls(run_id, limit=1)[0]['normalized_url'] == shallow.normalized_url
    finally:
        db.close()


def test_browser_identity_is_read_once_for_the_shared_context(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite',
        respect_robots=False, discovery_sources={'STATIC', 'RENDERED'})
    auditor = CoverageAuditor(config)
    pages, sessions = [], []
    class Identity:
        async def evaluate(self, expression): return 'Fixture browser'
        async def close(self): pass
    class Context:
        async def new_page(self):
            pages.append(1)
            return Identity()
        async def cookies(self, urls): return []
    class Browser:
        context = Context()
        async def explore(self, url):
            return BrowserDiscovery((), (), html='<h1>Complete rendered content</h1>', final_url=url, http_status=200)
    client = SimpleNamespace(browser_cookies=lambda url: [], use_browser_session=lambda *args: sessions.append(args))
    try:
        for path in ('first', 'second'):
            url = config.target_url+path
            auditor._add_discovery(url, 'STATIC', 1, None, '')
            auditor.db.add_http_result(auditor.run_id, url, status_code=200, validity='VALID_PAGE')
            assert asyncio.run(auditor._crawl_browser(Browser(), client, only_url=url))
        assert len(pages) == 1 and len(sessions) == 2
        assert all(item[-1] == 'Fixture browser' for item in sessions)
    finally:
        auditor.db.close()


def test_filters_do_not_reload_the_initial_or_already_restored_baseline():
    visits = []
    async def exercise():
        async with BrowserExplorer(sources={'RENDERED', 'FILTER_BROWSER'}) as browser:
            async def serve(route):
                visits.append(route.request.url)
                await route.fulfill(content_type='text/html', body='''<main><h1>Baseline content</h1>
                    <select name="year" onchange="result.innerHTML='<a href=/article/'+this.value+'>Article</a>'">
                    <option value="2026">2026</option><option value="2021">2021</option></select><div id="result"></div></main>''')
            await browser.context.route('https://fixture.test/**', serve)
            result = await browser.explore('https://fixture.test/')
            assert not result.error, result.error
            assert {state['value'] for state in result.states} == {'2026', '2021'}
            assert {f'https://fixture.test/article/{year}' for year in (2026, 2021)} <= {link.url for link in result.links}
            assert 'Article' not in extract_text(result.html)[0]  # Baseline, not the last filter state.
    asyncio.run(exercise())
    assert visits == ['https://fixture.test/'] * 3  # Previously initial + 2 values + 2 restores.


def test_browser_discovery_avoids_background_networkidle_waits_and_retains_delayed_api(monkeypatch):
    timings, inventories = {}, {}
    async def run(legacy):
        async with BrowserExplorer(sources={'RENDERED', 'API', 'LOAD_MORE'}) as browser:
            async def serve(route):
                url = route.request.url
                if 'google-analytics.com' in url:
                    await asyncio.sleep(.02)
                    await route.fulfill(content_type='application/json', headers={'Access-Control-Allow-Origin': '*'}, body='{}')
                elif '/api/articles' in url:
                    await asyncio.sleep(.7)
                    await route.fulfill(content_type='application/json', body='{"url":"/deep/article"}')
                else:
                    await route.fulfill(content_type='text/html', body='''<main><h1>Public archive</h1><div id="result"></div></main>
                    <script>setInterval(()=>fetch('https://www.google-analytics.com/g/collect?t='+Date.now()).catch(()=>{}),100);
                    fetch('/api/articles').then(r=>r.json()).then(d=>result.innerHTML='<a href='+d.url+'>Delayed article</a>');</script>''')
            await browser.context.route('https://fixture.test/**', serve)
            await browser.context.route('https://www.google-analytics.com/**', serve)
            if legacy:
                async def old_wait(self, page, **kwargs):
                    await page.wait_for_timeout(200)
                    try:
                        await page.wait_for_load_state('networkidle', timeout=3500)
                    except Exception:
                        await page.wait_for_timeout(300)
                    return True
                with monkeypatch.context() as patch:
                    patch.setattr(ContentReadiness, 'wait', old_wait)
                    started = asyncio.get_running_loop().time()
                    result = await browser.explore('https://fixture.test/')
            else:
                started = asyncio.get_running_loop().time()
                result = await browser.explore('https://fixture.test/')
            timings[legacy] = asyncio.get_running_loop().time() - started
            assert not result.error, result.error
            inventories[legacy] = {link.url for link in result.links}
            assert 'https://fixture.test/deep/article' in inventories[legacy]
            assert not any('pending' in reason for reason in result.limitations)
    asyncio.run(run(True))
    asyncio.run(run(False))
    assert inventories[True] == inventories[False]
    assert timings[False] < timings[True]
    print(f"Controlled noisy-page discovery: previous wait {timings[True]:.2f}s, optimized wait {timings[False]:.2f}s; same discovered URLs")
