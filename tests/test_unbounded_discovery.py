"""No live website or Corpus calls: exercise the removed discovery count ceilings."""
import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import LEGACY_DISCOVERY_LIMITS, AuditConfig
from crawler_coverage_auditor.discovery.browser import BrowserDiscovery, BrowserExplorer
from crawler_coverage_auditor.discovery.max_page import find_maximum_page
from crawler_coverage_auditor.discovery.sitemap import discover_sitemaps
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.web.api import create_app


def test_old_saved_limits_do_not_survive_config_or_worker_start(tmp_path, monkeypatch):
    old = dict(zip(LEGACY_DISCOVERY_LIMITS, (20000, 300, 60, 100, 1048576)))
    jobs = AuditJobs()
    monkeypatch.setattr(jobs, '_launch', lambda config, *args: config)
    config = jobs.start('https://example.test/', tmp_path/'audit.sqlite', tmp_path/'reports',
                        'https://api.test/', settings=old)
    assert all(getattr(config, key) is None for key in LEGACY_DISCOVERY_LIMITS)
    assert config.max_concurrency == config.corpus_batch_size == 1
    assert config.website_delay_seconds == 1 and config.corpus_interval_seconds == 2

    saved = tmp_path/'capture_settings.json'
    saved.write_text(json.dumps(old | {'capture_types': ['HTML'], 'use_browser': True}))
    client = TestClient(create_app(tmp_path/'audit.sqlite', tmp_path/'reports'))
    settings = client.get('/api/state').json()['settings']
    assert not set(LEGACY_DISCOVERY_LIMITS) & settings.keys()
    assert settings['capture_types'] == ['HTML'] and settings['use_browser']
    db = AuditDatabase(tmp_path/'audit.sqlite')
    run_id = db.create_run('https://example.test/', 'https://api.test/', old | {'capture_types': ['HTML']})
    db.close()
    resumed = jobs.resume(run_id, tmp_path/'audit.sqlite', tmp_path/'reports')
    assert resumed.resume and resumed.capture_types == {'HTML'}
    assert all(getattr(resumed, key) is None for key in LEGACY_DISCOVERY_LIMITS)


def test_inventory_continues_after_old_twenty_thousand_threshold(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite')
    auditor = CoverageAuditor(config)
    try:
        auditor.db.connection.executemany('INSERT INTO urls(audit_run_id,url,normalized_url,min_depth,discovery_methods) VALUES(?,?,?,?,?)',
            [(auditor.run_id, f'https://example.test/old/{n}', f'https://example.test/old/{n}', 1, '["STATIC"]') for n in range(20000)])
        auditor.db.connection.commit()
        assert auditor._add_discovery(config.target_url+'new', 'STATIC', 100, None, '')
        assert auditor._add_discovery(config.target_url+'deeper', 'STATIC', 101, None, '')
        assert not auditor._add_discovery(config.target_url+'new#section', 'STATIC', 102, None, '')
        assert auditor.db.connection.execute('SELECT count(*) FROM urls WHERE audit_run_id=?', (auditor.run_id,)).fetchone()[0] == 20002
        assert not auditor._limited
    finally:
        auditor.db.close()


def test_get_filters_explore_all_site_provided_values(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite', max_filter_values=1)
    auditor = CoverageAuditor(config)
    try:
        options = ''.join(f'<option value="{n}">{n}</option>' for n in range(1, 151))
        auditor._analyze_static_filters(f'<form method="get"><select name="category">{options}</select></form>', config.target_url, 0)
        rows = auditor.db.report_rows(auditor.run_id)
        assert len(rows) == 150
        assert any('category=150' in row['url'] for row in rows)
        assert not auditor._limited
    finally:
        auditor.db.close()


def test_browser_exploration_continues_after_three_hundred_pages(tmp_path):
    config = AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite', respect_robots=False)
    auditor = CoverageAuditor(config)
    calls = []
    class Browser:
        async def explore(self, url):
            calls.append(url)
            return BrowserDiscovery((), ())
    try:
        auditor._browser_seen = {config.target_url+str(n) for n in range(300)}
        url = config.target_url+'301'
        auditor._add_discovery(url, 'STATIC', 2, None, '')
        auditor.db.add_http_result(auditor.run_id, url, status_code=200, validity='VALID_PAGE')
        client = SimpleNamespace(browser_cookies=lambda url: [])
        assert asyncio.run(auditor._crawl_browser(Browser(), client, only_url=url))
        assert calls == [url] and not auditor._limited
    finally:
        auditor.db.close()


def test_numeric_pagination_has_no_old_probe_ceiling():
    probes = []
    async def valid(page):
        probes.append(page)
        return page <= 2_100_123
    assert asyncio.run(find_maximum_page(valid, maximum_probe=1048576)) == 2_100_123
    assert max(probes) > 1048576


def test_recursive_sitemaps_continue_after_one_thousand_files():
    found, checked = [], []
    class Client:
        async def get(self, url, **kwargs):
            if url.endswith('/sitemap.xml'):
                entries = ''.join(f'<sitemap><loc>https://example.test/maps/{n}.xml</loc></sitemap>' for n in range(1005))
                entries += '<sitemap><loc>https://example.test/sitemap.xml</loc></sitemap>'
                content = f'<sitemapindex>{entries}</sitemapindex>'.encode()
            elif '/maps/' in url:
                number = url.rsplit('/', 1)[-1].split('.')[0]
                content = f'<urlset><url><loc>https://example.test/pages/{number}</loc></url></urlset>'.encode()
            else:
                return FetchResult(url, url, 404, {}, b'')
            return FetchResult(url, url, 200, {}, content)
    asyncio.run(discover_sitemaps('https://example.test/', Client(),
        on_sitemap=lambda *args: checked.append(args[0]), on_url=lambda url, *args: found.append(url)))
    assert len(found) == 1005 and len(checked) == 1006
    assert 'https://example.test/pages/1004' in found


def test_unbounded_numeric_probing_still_allows_cancellation():
    from crawler_coverage_auditor.discovery.max_page import PaginationUnverified
    calls = 0
    async def valid(page):
        nonlocal calls
        calls += 1
        if calls > 5:
            raise PaginationUnverified('User stopped scan')
        return True
    import pytest
    with pytest.raises(PaginationUnverified, match='User stopped'):
        asyncio.run(find_maximum_page(valid))


def test_browser_load_more_and_filters_have_no_count_budget(monkeypatch):
    # Published synthetic page delivered locally by the browser's route handler.
    async def exercise():
        async def immediate_ready(self, page, **kwargs):
            await page.wait_for_timeout(1)
            return True
        monkeypatch.setattr('crawler_coverage_auditor.discovery.readiness.ContentReadiness.wait', immediate_ready)
        async with BrowserExplorer(actions=1, max_filter_values=1,
                sources={'RENDERED', 'FILTER_BROWSER', 'LOAD_MORE'}) as browser:
            assert not browser.error, browser.error
            original = browser.context.new_page
            async def quick_page():
                page = await original()
                async def immediate(*args, **kwargs):
                    await asyncio.sleep(.001)
                monkeypatch.setattr(page, 'wait_for_timeout', immediate)
                monkeypatch.setattr(page, 'wait_for_load_state', immediate)
                return page
            monkeypatch.setattr(browser.context, 'new_page', quick_page)
            options = ''.join(f'<option value="{n}">{n}</option>' for n in range(1, 106))
            html = f'''<main><h1>Published archive</h1><select name="year" onchange="result.innerHTML='<a href=/archive/'+this.value+'>Item</a>'">{options}</select><div id="result"></div></main>'''
            async def serve(route):
                await route.fulfill(status=200, content_type='text/html', body=html)
            await browser.context.route('https://fixture.test/**', serve)
            result = await browser.explore('https://fixture.test/filters')
            assert not result.error, result.error
            assert len(result.states) == 105
            assert any(link.url.endswith('/archive/105') for link in result.links)
            html = '''<main><h1>Published list</h1><div id="result"></div><button id="more" onclick="n++;result.innerHTML='<a href=/item/'+n+'>Item</a>';if(n===65)this.remove()">Load More</button></main><script>let n=0;</script>'''
            result = await browser.explore('https://fixture.test/more')
            assert not result.error, result.error
            assert {f'https://fixture.test/item/{n}' for n in range(1, 66)} <= {link.url for link in result.links}
            assert not any('budget' in reason or 'safeguard' in reason for reason in result.limitations)
    asyncio.run(exercise())
