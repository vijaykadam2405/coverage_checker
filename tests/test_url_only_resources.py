"""No file body reads or Corpus file-text requests; HTML comparisons stay intact."""
import asyncio
import gzip
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.analysis.coverage import coverage_summary
from crawler_coverage_auditor.analysis.dashboard import build_analysis, detail_row
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.validation.http import PoliteHttpClient


class ResourceSite(BaseHTTPRequestHandler):
    paths = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).paths.append(self.path)
        body = b''
        mime = 'text/html'
        status = 200
        if self.path == '/':
            body = b'''<title>HTML content</title><h1>Public archive</h1><p>Beginning middle ending.</p>
            <a href="/report.pdf">PDF</a><a href="/sheet.xlsx">Spreadsheet</a>
            <a href="/notes.txt">Notes</a><a href="/slides.pptx">Slides</a>
            <a href="/archive.zip">Archive</a><a download href="/download">Download</a>
            <img src="/picture"><img srcset="/small.png 1x, /large.webp 2x">
            <object data="/embedded"></object><embed src="/embedded-two">
            <video src="/movie.mp4"></video><audio src="/audio"></audio>
            <a href="https://cdn.example.test/report.pdf">CDN report</a>
            <script>fetch('/opaque-file').catch(()=>{});
            let image=new Image();image.src='/dynamic-image';</script>'''
        elif self.path == '/robots.txt':
            mime, body = 'text/plain', b'User-agent: *\nAllow: /'
        elif self.path == '/opaque-file':
            mime = 'application/pdf'
            body = b'%PDF-THIS-MUST-NOT-BE-PARSED'
        else:
            status = 404
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def resource_site():
    ResourceSite.paths = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), ResourceSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()


def test_known_files_and_extensionless_sources_recorded_without_get(tmp_path, resource_site):
    config = AuditConfig(target_url=resource_site, database_path=tmp_path/'audit.sqlite',
                         output_dir=tmp_path/'reports', discovery_only=True, max_retries=0)
    result = asyncio.run(CoverageAuditor(config).run())
    assert result['status'] == 'AWAITING_CORPUS'
    expected = {'report.pdf', 'sheet.xlsx', 'notes.txt', 'slides.pptx', 'archive.zip',
                'download', 'picture', 'small.png', 'large.webp', 'embedded', 'embedded-two', 'movie.mp4', 'audio'}
    assert not {f'/{path}' for path in expected}.intersection(ResourceSite.paths)
    assert not any('/api/data/' in path for path in ResourceSite.paths)
    with sqlite3.connect(config.database_path) as db:
        rows = dict(db.execute('SELECT normalized_url,validity FROM http_results'))
        assert all(rows[resource_site + path] == 'URL_ONLY' for path in expected)
        assert rows['https://cdn.example.test/report.pdf'] == 'URL_ONLY'
        assert db.execute("SELECT COUNT(*) FROM page_snapshots p JOIN urls u ON u.audit_run_id=p.audit_run_id AND u.normalized_url=p.normalized_url WHERE u.page_type IN ('DOCUMENT','IMAGE','RESOURCE')").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM http_results WHERE validity='URL_ONLY' AND (status_code IS NOT NULL OR text_length IS NOT NULL)").fetchone()[0] == 0


@pytest.mark.parametrize('mime,disposition', [('application/pdf', ''), ('image/png', ''),
    ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', ''),
    ('video/mp4', ''), ('text/plain', ''), ('text/csv', ''), ('text/plain', 'attachment; filename="notes.txt"'),
    ('IMAGE/PNG', ''), ('application/vnd.ms-excel', '')])
def test_extensionless_file_stream_closed_before_body_read(mime, disposition):
    class NeverRead(httpx.AsyncByteStream):
        read = False
        closed = False

        async def __aiter__(self):
            self.read = True
            raise AssertionError('Resource body must never be read')
            yield b''

        async def aclose(self):
            self.closed = True

    stream = NeverRead()
    async def exercise():
        async with PoliteHttpClient(1, 2, 0, 'test') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'Content-Type': mime, 'Content-Disposition': disposition}, stream=stream)))
            response = await client.get('https://example.test/opaque')
            assert response.url_only and response.content == b''
    asyncio.run(exercise())
    assert stream.closed and not stream.read


def test_html_compressed_response_still_reads_correctly():
    async def exercise():
        body = b'<h1>HTML meaningful content</h1>'
        async with PoliteHttpClient(1, 2, 0, 'test') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'Content-Type': 'text/html', 'Content-Encoding': 'gzip'}, content=gzip.compress(body))))
            response = await client.get('https://example.test/')
            assert not response.url_only and response.content == body
            assert 'meaningful content' in response.text
    asyncio.run(exercise())


def test_file_error_body_skipped_without_losing_retry_after():
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('Do not consume error file body')
            yield b''

    async def exercise():
        async with PoliteHttpClient(1, 2, 2, 'test') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(429, headers={'Content-Type': 'text/plain', 'Retry-After': '120'}, stream=NeverRead())))
            result = await client.get('https://example.test/opaque')
            assert result.status_code == 429 and not result.url_only and result.content == b''
            assert client._next_allowed['example.test'] - asyncio.get_running_loop().time() > 119
    asyncio.run(exercise())


def test_saved_inventory_checks_resource_urls_but_reads_only_html_text(tmp_path, resource_site):
    requests = []
    class Corpus(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append(self.path)
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/v1/text':
                urls = [item['url'] for item in payload['items']]
                assert all(url in {resource_site, resource_site+'detail'} for url in urls)
                results = [{'requested_url': url, 'lookup_status': 'found', 'matched_url': url,
                    'content_type': 'text/html', 'html_text': {'status': 'empty', 'truncated': False, 'text': ''}} for url in urls]
            else:
                assert self.path == '/v1/held'
                results = [{'url': url, 'held': not url.endswith('report.pdf'),
                    'match': 'exact', 'matched_url': url} for url in payload['urls']]
            body = json.dumps({'results': results}).encode()
            mime = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Corpus)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = AuditConfig(target_url=resource_site, corpus_url=f'http://127.0.0.1:{server.server_port}/',
        database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports', discovery_only=True,
        max_retries=0, corpus_interval_seconds=0)
    try:
        auditor = CoverageAuditor(config)
        asyncio.run(auditor.run())
        assert not requests
        website_requests = list(ResourceSite.paths)
        result = asyncio.run(CoverageAuditor(config, run_id=auditor.run_id).verify_inventory())
        assert result['text_checked_urls'] > 0
        assert result['all_checked_urls'] == result['corpus_candidate_urls']
        assert ResourceSite.paths == website_requests
        assert set(requests) == {'/v1/held', '/v1/text'}
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM corpus_matches c JOIN urls u ON u.audit_run_id=c.audit_run_id AND u.normalized_url=c.normalized_url WHERE u.page_type IN ('DOCUMENT','IMAGE','RESOURCE') AND c.text_status != 'URL_ONLY'").fetchone()[0] == 0
            assert db.execute("SELECT match_status FROM corpus_matches WHERE normalized_url=?", (resource_site+'picture',)).fetchone()[0] == 'EXACT_MATCH'
            assert db.execute("SELECT match_status FROM corpus_matches WHERE normalized_url=?", (resource_site+'report.pdf',)).fetchone()[0] == 'NOT_FOUND'
    finally:
        server.shutdown()
        server.server_close()


def test_resources_excluded_from_html_text_metrics_including_old_audits():
    rows = [{'url': 'https://example.test/html', 'page_type': 'HTML', 'validity': 'VALID_PAGE', 'match_status': 'EXACT_MATCH', 'text_status': 'FOUND_FULL'},
            {'url': 'https://example.test/old.pdf', 'page_type': 'DOCUMENT', 'validity': 'VALID_PAGE', 'match_status': 'EXACT_MATCH', 'text_status': 'FOUND_PARTIAL_TEXT', 'similarity': .1},
            {'url': 'https://example.test/new.pdf', 'page_type': 'DOCUMENT', 'validity': 'URL_ONLY', 'match_status': 'NOT_FOUND'},
            {'url': 'https://example.test/image', 'page_type': 'IMAGE', 'validity': 'URL_ONLY', 'match_status': 'NOT_FOUND'}]
    summary = coverage_summary(rows)
    assert summary['text_checked_urls'] == 1 and summary['text_coverage_percent'] == 100
    assert summary['expected_urls'] == 3 and summary['corpus_candidate_urls'] == 4
    assert summary['all_checked_urls'] == 4
    assert build_analysis(rows, {})['content_counts'] == {'Full content': 1}
    assert detail_row(rows[1])['Content coverage'] == ['URL only']
    assert detail_row(rows[1])['Text similarity %'] is None
    assert detail_row(rows[2])['Corpus status'] == ['Missing from corpus']
    assert detail_row(rows[3])['Crawl status'] == 'URL collected'


def test_browser_blocks_file_bodies_and_keeps_their_urls(resource_site):
    async def exercise():
        async with BrowserExplorer(actions=1) as browser:
            assert not browser.error
            result = await browser.explore(resource_site)
            assert not result.error, result.error
            links = {link.url: link.tag for link in result.links}
            assert links[resource_site+'opaque-file'] == 'download'
            assert links[resource_site+'dynamic-image'] == 'img'
            assert links[resource_site+'picture'] == 'img'
            assert links[resource_site+'report.pdf'] == 'a'
            assert 'Public archive' in result.html
    asyncio.run(exercise())
    assert '/picture' not in ResourceSite.paths
    assert '/dynamic-image' not in ResourceSite.paths
    assert '/embedded' not in ResourceSite.paths
    assert '/embedded-two' not in ResourceSite.paths
    assert '/movie.mp4' not in ResourceSite.paths
