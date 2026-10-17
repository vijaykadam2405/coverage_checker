from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.discovery.local_browser import (
    browser_engine,
    local_browser_dir,
)


def test_service_configuration_is_not_a_browser_connection(monkeypatch):
    monkeypatch.delenv('CRAWLSCOPE_BROWSER_ENGINE', raising=False)
    monkeypatch.setenv('CAMOFOX_ENABLED', '1')
    monkeypatch.setenv('CAMOFOX_URL', 'http://friend-system.invalid:9377')
    assert browser_engine() == 'camoufox'
    monkeypatch.setenv('CRAWLSCOPE_BROWSER_ENGINE', 'camoufox')
    assert browser_engine() == 'camoufox'
    assert local_browser_dir().is_relative_to(Path(__file__).resolve().parents[1] / 'data')
    monkeypatch.setenv('CRAWLSCOPE_BROWSER_ENGINE', 'http://friend-system.invalid')
    with pytest.raises(ValueError):
        browser_engine()


@pytest.mark.parametrize('status,headers,permitted,tag', [
    (200, {'content-type': 'text/html'}, True, None),
    (403, {'content-type': 'text/html'}, True, None),
    (200, {'content-type': 'application/json'}, True, None),
    (200, {'content-type': 'application/pdf'}, False, 'download'),
    (200, {'content-type': 'image/png'}, False, 'img'),
    (200, {'content-type': 'text/html', 'content-disposition': 'attachment'}, False, 'download'),
    (200, {}, False, None),
    (405, {'content-type': 'text/html'}, False, None),
    (429, {'content-type': 'text/html'}, False, None),
    (401, {'content-type': 'text/html'}, False, None),
    (503, {'content-type': 'text/html'}, False, None),
    (302, {'location': 'https://example.test/page'}, True, None),
])
def test_firefox_header_guard_never_reads_bodies(status, headers, permitted, tag):
    calls = []
    class Response:
        async def dispose(self):
            calls.append('disposed')
        async def body(self):
            raise AssertionError('Must never read the body')
    response = Response()
    response.status, response.headers = status, headers
    class RequestAPI:
        async def head(self, url, **options):
            assert options['max_redirects'] == 0
            calls.append('HEAD')
            return response
    async def check():
        browser = BrowserExplorer()
        browser.context = SimpleNamespace(request=RequestAPI())
        request = SimpleNamespace(url='https://example.test/opaque', method='GET')
        assert await browser._headers_permit(request) is permitted
        if tag:
            assert browser._route_links[request.url].tag == tag
        if status == 429:
            assert 'example.test' in browser._paused_hosts
    asyncio.run(check())
    assert calls == ['HEAD', 'disposed']


def test_firefox_header_guard_skips_post_and_reports_failures():
    async def check():
        browser = BrowserExplorer()
        assert not await browser._headers_permit(SimpleNamespace(method='POST', url='https://example.test/api'))
        assert not await browser._headers_permit(SimpleNamespace(method='GET', url='https://example.test/api'))
        assert len(browser._route_limitations) == 2
    asyncio.run(check())


def test_local_camoufox_keeps_public_discovery_and_file_url_only_rules(monkeypatch):
    pytest.importorskip('camoufox')
    if not (local_browser_dir() / 'version.json').exists():
        pytest.skip('Optional local browser has not been installed')
    requests = []
    class Website(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def mime(self):
            return 'application/pdf' if self.path == '/opaque-file' else 'application/json' if self.path == '/api' else 'text/html'
        def do_HEAD(self):
            requests.append(('HEAD', self.path))
            self.send_response(200)
            self.send_header('Content-Type', self.mime())
            self.end_headers()
        def do_GET(self):
            requests.append(('GET', self.path))
            body = json.dumps({'url': '/api-discovered-page'}) if self.path == '/api' else '''<html><body><main><h1>Public local browser fixture</h1>
                <p>These are meaningful public content words for browser verification.</p>
                <a href="/report.pdf">PDF</a><img src="/image.png"><a href="/deep/page">Page</a>
                <script>fetch('/api').then(r=>r.json());fetch('/opaque-file');</script></main></body></html>'''
            self.send_response(200)
            self.send_header('Content-Type', self.mime())
            self.end_headers()
            self.wfile.write(body.encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Website)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('CRAWLSCOPE_BROWSER_ENGINE', 'camoufox')
    monkeypatch.setenv('CAMOFOX_URL', 'http://friend-system.invalid:9377')
    base = f'http://127.0.0.1:{server.server_port}'
    async def exercise():
        async with BrowserExplorer(sources={'RENDERED', 'API'}, challenge_wait_seconds=0) as browser:
            assert not browser.error, browser.error
            assert browser.engine == 'camoufox'
            result = await browser.explore(base + '/')
            assert not result.error, result.error
            assert result.http_status == 200
            links = {link.url: link.tag for link in result.links}
            assert base + '/deep/page' in links
            assert base + '/api-discovered-page' in links
            assert base + '/report.pdf' in links
            assert links[base + '/opaque-file'] == 'download'
    try:
        asyncio.run(exercise())
        assert ('HEAD', '/opaque-file') in requests
        assert not any(method == 'GET' and path in {'/opaque-file', '/report.pdf', '/image.png'} for method, path in requests)
    finally:
        server.shutdown()
        server.server_close()
