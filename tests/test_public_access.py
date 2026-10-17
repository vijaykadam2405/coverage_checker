from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.validation.http import FetchResult, PoliteHttpClient
from crawler_coverage_auditor.validation.site_session import (
    SiteSessionStore,
    import_cookies,
    scoped_state,
)


@pytest.mark.parametrize('text', [
    '<title>Research</title><h1>CAPTCHA and Cloudflare</h1><article>We explain how bot detection and access denied errors work.</article>',
    '<title>Contact us</title><main>Get in touch with our public office.</main><form><div class="g-recaptcha"></div></form>',
    '<title>News</title><h1>News about login</h1><main>Public article</main><div hidden><input type="password"></div>',
    '<title>Challenge documentation</title><article>The code uses cf_chl_opt and servicepipe.ru/loaders/ for verification.</article>',
])
def test_vendor_words_widgets_and_login_modals_do_not_block_public_content(text):
    assert detect_access(200, {'server': 'cloudflare'}, text) is None


@pytest.mark.parametrize('code,headers,html,status', [
    (403, {'cf-mitigated': 'challenge'}, '<title>Just a moment</title>', 'BOT_BLOCKED'),
    (200, {}, '<title>Just a moment</title><script>window._cf_chl_opt={cType:"interactive"}</script>', 'CAPTCHA_REQUIRED'),
    (202, {'x-amzn-waf-action': 'challenge'}, '', 'BOT_BLOCKED'),
    (200, {}, '<title>Security check</title><div class="h-captcha"></div>', 'CAPTCHA_REQUIRED'),
    (200, {}, '<title>Sign in</title><input type="password">', 'LOGIN_REQUIRED'),
    (401, {}, '', 'LOGIN_REQUIRED'),
    (429, {}, '', 'RATE_LIMITED'),
    (451, {}, '', 'GEO_BLOCKED'),
])
def test_definitive_walls_are_classified(code, headers, html, status):
    assert detect_access(code, headers, html).status == status


def cookie(name='public_session', value='fixture-secret', **extra):
    return {'name': name, 'value': value, 'domain': 'example.test', 'path': '/', 'expires': -1,
            'httpOnly': True, 'secure': True, 'sameSite': 'Lax', **extra}


def test_session_is_origin_scoped_expired_cookies_removed_and_windows_encrypted(tmp_path):
    store = SiteSessionStore(tmp_path)
    url = 'https://example.test/'
    state = {'cookies': [cookie(), cookie(domain='unrelated.test'), cookie(expires=time.time()-1)],
             'origins': [{'origin': url.rstrip('/'), 'localStorage': [{'name': 'password', 'value': 'never-copy'}]}]}
    path = store.save(url, state, 'Fixture browser')
    assert b'never-copy' not in path.read_bytes()
    import sys
    if sys.platform == 'win32':
        assert b'fixture-secret' not in path.read_bytes()
    saved = store.load(url)
    assert saved['state'] == {'cookies': [cookie()], 'origins': []}
    assert store.load('https://unrelated.test/') is None
    assert store.load('http://example.test/') is None
    assert store.load('https://example.test:8443/') is None
    with pytest.raises(ValueError):
        store.load('https://user:password@example.test/')


def test_cookie_replay_preserves_secure_path_expiry_and_domain():
    client = httpx.Client()
    try:
        import_cookies(client, [cookie(path='/files'), cookie(name='host-only')], 'https://example.test/')
        assert 'public_session=fixture-secret' in client.build_request('GET', 'https://example.test/files/report').headers['cookie']
        assert 'public_session' not in client.build_request('GET', 'https://example.test/news').headers.get('cookie', '')
        assert 'cookie' not in client.build_request('GET', 'http://example.test/files/report').headers
        assert 'cookie' not in client.build_request('GET', 'https://unrelated.test/files/report').headers
        assert scoped_state({'cookies': [cookie(expires=time.time()-1)]}, 'https://example.test/')['cookies'] == []
    finally:
        client.close()


class AccessHandler(BaseHTTPRequestHandler):
    robots_status = 403
    mode = 'public'
    paths = []

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        authorized = self.mode != 'browser' or ('public_session=ready' in self.headers.get('Cookie', '') and self.headers.get('Referer', '').endswith('/'))
        self.send_response(200 if self.path == '/asset.txt' and authorized else 403)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()

    def do_GET(self):
        type(self).paths.append(self.path)
        status, headers, body = 200, {}, ''
        if self.path == '/robots.txt':
            status = self.robots_status
            body = 'User-agent: *\nDisallow: /restricted' if status == 200 else ''
        elif self.path.startswith('/sitemap'):
            status = 404
        elif self.path == '/' and self.mode == 'browser' and 'public_session=ready' not in self.headers.get('Cookie', ''):
            status, headers = 403, {'cf-mitigated': 'challenge'}
            body = '''<title>Just a moment</title><script>
              document.cookie='public_session=ready; path=/; SameSite=Lax';
              setTimeout(()=>location.reload(), 400);</script>'''
        elif self.path == '/':
            if self.mode == 'api-limit':
                body = '''<title>Archive</title><h1>Public archive</h1>
                  <button onclick="fetch('/api/limited').finally(()=>this.remove())">Load More</button>'''
            else:
                body = '<title>Public archive</title><h1>Archive</h1><article>Independent public news articles and documents.</article><a href="/deep/page">Read more</a><a href="/restricted">Restricted</a><a href="/asset.txt">File</a>'
        elif self.path == '/api/limited':
            status, headers, body = 429, {'Retry-After': '120'}, 'Rate limited'
        elif self.path == '/deep/page':
            body = '<h1>Deep public content</h1><article>Public website content mentioning CAPTCHA and Cloudflare but not a challenge.</article>'
        elif self.path == '/restricted':
            status, body = 401, '<h1>Login required</h1>'
        elif self.path == '/asset.txt':
            if self.mode == 'browser' and ('public_session=ready' not in self.headers.get('Cookie', '') or not self.headers.get('Referer', '').endswith('/')):
                status = 403
            else:
                headers, body = {'Content-Type': 'text/plain'}, 'Public downloadable text document.'
        else:
            status = 404
        self.send_response(status)
        self.send_header('Content-Type', headers.pop('Content-Type', 'text/plain' if self.path == '/robots.txt' else 'text/html'))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body.encode())


@pytest.fixture
def website():
    AccessHandler.paths = []
    AccessHandler.robots_status = 403
    AccessHandler.mode = 'public'
    server = ThreadingHTTPServer(('127.0.0.1', 0), AccessHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('robots_status,attempted', [(403, True), (200, True), (503, False), (429, False)])
def test_robots_403_does_not_deny_homepage_but_real_rules_and_unreachable_policy_apply(tmp_path, website, robots_status, attempted):
    AccessHandler.robots_status = robots_status
    config = AuditConfig(target_url=website, database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports',
                         discovery_only=True, max_retries=0)
    result = asyncio.run(CoverageAuditor(config).run())
    assert ('/' in AccessHandler.paths) == attempted
    assert not any('/api/data/search' in path for path in AccessHandler.paths)
    assert result['status'] == 'AWAITING_CORPUS'
    if robots_status == 200:
        assert '/restricted' not in AccessHandler.paths
    if attempted:
        assert '/deep/page' in AccessHandler.paths
        with sqlite3.connect(config.database_path) as connection:
            root = connection.execute('SELECT validity FROM http_results WHERE normalized_url=?', (website,)).fetchone()
            assert root == ('VALID_PAGE',)


def test_browser_recovers_public_cookie_session_and_records_files_without_download(tmp_path, website):
    AccessHandler.mode = 'browser'
    config = AuditConfig(target_url=website, database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports',
                         discovery_only=True, use_browser=True, max_retries=0, max_browser_actions=5,
                         browser_challenge_wait_seconds=3)
    result = asyncio.run(CoverageAuditor(config).run())
    assert result['status'] == 'AWAITING_CORPUS'
    with sqlite3.connect(config.database_path) as connection:
        rows = dict(connection.execute('SELECT normalized_url,validity FROM http_results'))
        assert rows[website] == 'VALID_PAGE'
        assert rows[website+'deep/page'] == 'VALID_PAGE'
        assert rows[website+'asset.txt'] == 'URL_ONLY'
        assert rows[website+'restricted'] == 'LOGIN_REQUIRED'
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE kind='SESSION_REUSED'").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE kind='CORPUS_CHECKING'").fetchone()[0] == 0
    assert '/asset.txt' not in AccessHandler.paths


def test_unresolved_captcha_is_not_crawled_as_valid_content(website):
    async def explore():
        async with BrowserExplorer(actions=1, challenge_wait_seconds=0) as browser:
            assert not browser.error
            # Exercise the same classifier through browser rendering with an actual HTTP response.
            await browser.context.route('**/captcha', lambda route: route.fulfill(status=200,
                content_type='text/html', body='<title>Security check</title><div class="h-captcha"></div><a href="/fake">Challenge link</a>'))
            return await browser.explore(website+'captcha')
    result = asyncio.run(explore())
    assert result.http_status == 200
    assert 'CAPTCHA' in result.error
    assert not result.links


def test_browser_retry_after_cools_http_queue_and_encoding_is_respected():
    async def check():
        async with PoliteHttpClient(1, 1, 0, 'Fixture') as client:
            start = asyncio.get_running_loop().time()
            client.observe_response('https://example.test/', 403, {'Retry-After': '0.04'})
            assert await client.wait_turn('https://example.test/deep')
            assert asyncio.get_running_loop().time() - start >= .035
    asyncio.run(check())
    assert FetchResult('', '', 200, {'content-type': 'text/html; charset=windows-1251'}, 'Новость'.encode('cp1251')).text == 'Новость'


def test_long_http_retry_after_is_reported_without_retrying_early():
    requests = []
    async def check():
        async with PoliteHttpClient(1, 1, 2, 'Fixture') as client:
            await client._client.aclose()
            def response(request):
                requests.append(request)
                return httpx.Response(429, headers={'Retry-After': '120'}, content=b'Limited')
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
            result = await client.get('https://example.test/')
            assert result.status_code == 429
            assert client._next_allowed['example.test'] - asyncio.get_running_loop().time() > 119
    asyncio.run(check())
    assert len(requests) == 1


def test_browser_api_rate_limit_pauses_future_host_navigation(website):
    AccessHandler.mode = 'api-limit'
    async def check():
        async with PoliteHttpClient(1, 2, 0, 'Fixture') as client:
            async with BrowserExplorer(actions=4, on_response=client.observe_response, pace=client.wait_turn) as browser:
                result = await browser.explore(website)
                assert any(item.status_code == 429 for item in result.network)
                assert any('429' in reason for reason in result.limitations)
                assert any('Retry-After 120' in reason for reason in result.limitations)
                assert len([item for item in AccessHandler.paths if item == '/api/limited']) == 1
                requests_before = len(AccessHandler.paths)
                next_result = await browser.explore(website+'deep/page')
                assert next_result.error
                assert len(AccessHandler.paths) == requests_before
    asyncio.run(check())


def test_http_redirect_cookie_policy_does_not_leak_host_only_cookie_or_referrer():
    requests = []
    async def check():
        async with PoliteHttpClient(1, 1, 0, 'Fixture') as client:
            hooks = client._client.event_hooks
            await client._client.aclose()
            def response(request):
                requests.append(request)
                if request.url.host == 'example.test':
                    return httpx.Response(302, headers={'Location': 'https://sub.example.test/files'})
                return httpx.Response(200, content=b'Public content')
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=True,
                                              event_hooks=hooks)
            from http.cookiejar import DefaultCookiePolicy
            client._client.cookies.jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
            client.use_browser_session('https://example.test/', [cookie()])
            result = await client.get('https://example.test/', referer='https://example.test/parent')
            assert result.status_code == 200
    asyncio.run(check())
    assert requests[0].headers['cookie'] == 'public_session=fixture-secret'
    assert requests[0].headers['referer'] == 'https://example.test/parent'
    assert 'cookie' not in requests[1].headers
    assert 'referer' not in requests[1].headers
