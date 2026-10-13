"""Controlled tests for compatible ZIP adaptations; no live bypass or Corpus load."""
import asyncio
from datetime import datetime, timezone
import gzip
import json
import ssl

import httpx
import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.api_pagination import continuations
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.discovery.resources import json_links
from crawler_coverage_auditor.discovery.sitemap import discover_sitemaps
from crawler_coverage_auditor.discovery.static_links import extract_links
from crawler_coverage_auditor.validation.access import detect_access, requires_rendering
from crawler_coverage_auditor.validation.failure_policy import quota_delay, retry_after, retryable_status, status_reason, transport_reason
from crawler_coverage_auditor.validation.http import FetchResult, PoliteHttpClient


def test_retry_after_and_exhausted_budget_headers():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert retry_after('Mon, 12 Oct 2026 00:00:05 GMT', now=now) == 5
    assert retry_after('0.25') == .25
    for bad in ['infinity', 'nan', 'invalid', 'inf']:
        assert retry_after(bad) is None
    assert quota_delay({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(now.timestamp() + 9)}, now=now) == 9
    assert quota_delay({'RateLimit': '"default";r=0;t=7'}) == 7
    assert quota_delay({'RateLimit-Remaining': '0', 'RateLimit-Reset': '11'}) == 11
    assert quota_delay({'RateLimit-Remaining': '1', 'RateLimit-Reset': '11'}) is None


def test_named_status_and_transport_policies():
    assert all(retryable_status(code) for code in [408, 425, 429, 500, 502, 503, 504, 522, 525, 599])
    assert all(not retryable_status(code) for code in [401, 403, 404, 410, 451, 511])
    assert status_reason(525) == 'ORIGIN_TLS_ERROR'
    assert transport_reason(ssl.SSLCertVerificationError('certificate verify failed')) == 'SSL_ERROR'
    assert transport_reason(httpx.TooManyRedirects('Exceeded maximum allowed redirects')) == 'REDIRECT_LOOP'
    assert transport_reason(httpx.ConnectError('getaddrinfo failed')) == 'DNS_FAILURE'


@pytest.mark.parametrize('code', [408, 425, 522, 525])
def test_http_retries_transient_responses_serially(code):
    requests = []
    async def exercise():
        async with PoliteHttpClient(1, 1, 1, 'Fixture') as client:
            await client._client.aclose()
            def handler(request):
                requests.append(request.url)
                return httpx.Response(code if len(requests) == 1 else 200, headers={'Content-Type': 'text/html'}, content=b'<h1>Public content</h1>')
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            async def wait(seconds):
                return True
            client._wait = wait
            assert (await client.get('https://example.test/')).status_code == 200
    asyncio.run(exercise())
    assert len(requests) == 2


@pytest.mark.parametrize('error,reason', [(httpx.ConnectError('certificate_verify_failed'), 'SSL_ERROR'),
                                        (httpx.TooManyRedirects('Exceeded maximum allowed redirects'), 'REDIRECT_LOOP')])
def test_terminal_transport_failure_is_not_retried(error, reason):
    requests = []
    async def exercise():
        async with PoliteHttpClient(1, 1, 3, 'Fixture') as client:
            await client._client.aclose()
            def handler(request):
                requests.append(request)
                raise error
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            result = await client.get('https://example.test/')
            assert result.failure_reason == reason and result.content == b''
    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize('declared', [True, False])
def test_large_response_is_closed_without_storing_partial_content(declared):
    class Body(httpx.AsyncByteStream):
        read = 0
        closed = False
        async def __aiter__(self):
            for _ in range(4):
                self.read += 1
                yield b'x' * 65536
        async def aclose(self):
            self.closed = True
    body = Body()
    async def exercise():
        async with PoliteHttpClient(1, 1, 3, 'Fixture', max_response_bytes=65536) as client:
            await client._client.aclose()
            headers = {'Content-Type': 'text/html', **({'Content-Length': '1000000'} if declared else {})}
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=body)))
            result = await client.get('https://example.test/')
            assert result.failure_reason == 'TOO_LARGE' and not result.content
    asyncio.run(exercise())
    assert body.closed and body.read == (0 if declared else 2)


def test_successful_response_with_spent_quota_stays_valid_but_delays_next_request():
    async def exercise():
        async with PoliteHttpClient(1, 1, 0, 'Fixture') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
                headers={'Content-Type': 'text/html', 'X-RateLimit-Remaining': '0', 'RateLimit-Reset': '120'}, content=b'<h1>Article</h1>')))
            result = await client.get('https://example.test/')
            assert result.status_code == 200 and result.failure_reason is None and 'Article' in result.text
            assert client._next_allowed['example.test'] - asyncio.get_running_loop().time() > 119
    asyncio.run(exercise())


def test_html_meta_charset_is_honored():
    text = '<meta charset="windows-1251"><h1>Новость</h1>'
    assert 'Новость' in FetchResult('', '', 200, {'Content-Type': 'text/html'}, text.encode('cp1251')).text


@pytest.mark.parametrize('html,expected', [
    ('<title>403 ERROR</title><h1>Request blocked</h1>The request could not be satisfied. Generated by CloudFront.', 'BOT_BLOCKED'),
    ('<h1>Too many requests</h1>Try again later.', 'RATE_LIMITED'),
    ('<h1>This content is not available in your country</h1>', 'GEO_BLOCKED'),
    ('<h1>Subscription required</h1>Sign up to continue.', 'PAYWALL'),
    ('<h1>Verify your age</h1><div id="age-gate"></div>', 'AGE_VERIFICATION'),
    ('{"error":"unauthorized"}', 'LOGIN_REQUIRED'),
])
def test_http_200_wall_is_never_content(html, expected):
    assert detect_access(200, {}, html).status == expected


def test_public_prose_widgets_and_consent_policy_are_not_walls():
    html = '<h1>CloudFront tutorial</h1><article>We explain the request could not be satisfied, too many requests and subscription required messages.</article><div class="g-recaptcha"></div>'
    assert detect_access(200, {}, html) is None
    assert detect_access(200, {}, '{"error":{"code":"unknown"}}') is None
    assert detect_access(200, {}, '<h1>Cookie policy</h1>Choices', 'https://example.test/consent/') is None
    assert detect_access(200, {}, '<h1>Privacy choices</h1>', 'https://example.test/consent/', requested_url='https://example.test/news').status == 'CONSENT_REQUIRED'


def test_js_shell_requires_rendering_but_small_real_page_does_not():
    assert requires_rendering('<div id="root">Loading...</div><script src="/app.js"></script>', 'Loading...')
    assert requires_rendering('<div id="__nuxt"></div><script src="/app.js"></script>', '')
    assert not requires_rendering('<div id="root"><h1>About our business</h1><p>Visit us.</p></div><script src="/app.js"></script>', 'About our business Visit us.')


def test_broken_href_does_not_lose_other_links_or_lazy_resource_urls():
    html = '<base href="https://[invalid/"><a href="https://[invalid/">Broken</a><a href="/valid">Valid</a><img data-src="/lazy.jpg"><picture><source data-srcset="/large.webp 2x, /small.webp 1x"></picture><video poster="/poster.png"></video><img data-src="data:invalid">'
    links = extract_links(html, 'https://example.test/')
    assert {link.url for link in links} == {'https://example.test'+path for path in ['/valid', '/lazy.jpg', '/large.webp', '/small.webp', '/poster.png']}
    assert all(link.tag == 'img' for link in links if link.url.endswith(('.jpg', '.webp', '.png')))


def test_response_backed_api_pagination_preserves_filter_values_and_never_guesses_cursors():
    url = 'https://example.test/api?cursorMark=old&category=a&category=b'
    assert continuations({'nextCursorMark': 'new'}, url) == ['https://example.test/api?cursorMark=new&category=a&category=b']
    assert continuations({'nextCursorMark': 'old', 'has_more': True, 'items': [1]}, url) == []
    assert continuations({'meta': {'total': 25}, 'items': list(range(10))}, 'https://example.test/api?offset=10&limit=10') == ['https://example.test/api?offset=20&limit=10']
    assert continuations({'meta': {'total': 25}, 'items': [1]}, 'https://example.test/api?offset=20&limit=10') == []
    assert continuations({'total_pages': 3, 'items': [1]}, 'https://example.test/api?page=2') == ['https://example.test/api?page=3']
    assert continuations({'total_pages': 3, 'items': [1]}, 'https://example.test/api?page=3') == []
    assert continuations({'nbPages': 2, 'hits': [1]}, 'https://example.test/api?page=1') == []
    assert continuations({'items': [1]}, 'https://example.test/api?page=1') == []
    assert continuations({'total': 100, 'items': [1]}, 'https://example.test/api?page=1') == []
    assert continuations({'hasMore': True, 'items': []}, 'https://example.test/api?page=1') == []
    links = json_links(json.dumps({'nextPageUrl': '?page=2', 'bad': 'https://[invalid/', 'url': '/story'}), 'https://example.test/api?page=1')
    assert {link.url for link in links} == {'https://example.test/api?page=2', 'https://example.test/story'}


def test_oversized_robots_response_never_becomes_allow_all(tmp_path):
    auditor = CoverageAuditor(AuditConfig('https://example.test/', database_path=tmp_path/'audit.sqlite'))
    class Client:
        async def get(self, url, **kwargs):
            return FetchResult(url, url, 200, {}, b'', 'TOO_LARGE', failure_reason='TOO_LARGE')
    auditor._website_client = Client()
    try:
        assert not asyncio.run(auditor._allowed('https://example.test/page'))
    finally:
        auditor.db.close()


def test_gzip_sitemap_decoded_limit_is_reported_not_silently_truncated():
    errors, urls = [], []
    class Client:
        max_response_bytes = 128
        async def get(self, url, **kwargs):
            data = gzip.compress(b'<urlset>' + b'<url><loc>https://example.test/page</loc></url>' * 10 + b'</urlset>')
            return FetchResult(url, url, 200 if url.endswith('/sitemap.xml') else 404, {}, data if url.endswith('/sitemap.xml') else b'')
    asyncio.run(discover_sitemaps('https://example.test/', Client(), on_sitemap=lambda *args: None,
                on_url=lambda *args: urls.append(args), on_error=lambda *args: errors.append(args)))
    assert urls == [] and any('decoded response limit' in item[1] for item in errors)


def test_browser_cleanup_continues_after_owned_resource_failure():
    calls, events = [], []
    class Resource:
        def __init__(self, name):
            self.name = name
        async def close(self):
            calls.append(self.name)
            if self.name == 'context':
                raise RuntimeError('closed connection')
        async def stop(self):
            calls.append(self.name)
    async def exercise():
        browser = BrowserExplorer(on_event=lambda *args: events.append(args))
        browser.context, browser.browser, browser.playwright = [Resource(name) for name in ['context', 'browser', 'driver']]
        await browser.__aexit__()
        async def hung():
            await asyncio.sleep(100)
        await browser._close_bounded(hung, 'owned page', timeout=.01)
    asyncio.run(exercise())
    assert calls == ['context', 'browser', 'driver'] and len(events) == 2


def test_browser_response_observers_cannot_pin_teardown():
    async def exercise():
        async def stuck():
            await asyncio.sleep(100)
        browser = BrowserExplorer()
        task = asyncio.create_task(stuck())
        assert not await browser._drain_tasks([task], timeout=.01)
        assert task.cancelled()
        assert await browser._drain_tasks([task], timeout=.01)
    asyncio.run(exercise())
