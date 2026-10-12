from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.evidence import embedded_links, feed_links, header_links
from crawler_coverage_auditor.validation.normalize import same_scope


def test_published_sources_expose_unlinked_content():
    base = 'https://example.test/'
    assert embedded_links('<script type="application/json">{"items":[{"url":"/hidden/deep"}]}</script>', base)[0].url == base + 'hidden/deep'
    assert feed_links('<rss><channel><item><link>/archive/2001/item</link></item></channel></rss>', base)[0].url == base + 'archive/2001/item'
    assert header_links('</api/items?cursor=abc>; rel="next", </api/items?cursor=end>; rel="last"', base)[0].url == base + 'api/items?cursor=abc'
    assert same_scope('https://news.example.test/a', {'example.test', 'www.example.test'}, True)
    assert not same_scope('https://example.test.evil.test/a', {'example.test'}, True)


def test_discovery_isolated_then_paced_verification_and_resume(tmp_path):
    website_requests = []
    corpus_requests = []
    queries = Counter()
    state = {'fail': True}
    class Website(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            website_requests.append(self.path)
            bodies = {
                '/': '<h1>Website</h1><a href="/child">Child</a><script type="application/ld+json">{"url":"/hidden"}</script><link rel="alternate" href="/feed.xml">',
                '/child': '<h1>Child content</h1><a href="/deep/1">Go deeper</a>',
                '/hidden': '<h1>Unlinked published content</h1>',
                '/feed.xml': '<rss><channel><item><link>/old/2001/article</link></item></channel></rss>',
                '/old/2001/article': '<h1>Archive article</h1>',
            }
            if self.path.startswith('/deep/'):
                depth = int(self.path.rsplit('/', 1)[1])
                bodies[self.path] = f'<h1>Depth {depth}</h1>' + (f'<a href="/deep/{depth+1}">Continue</a>' if depth < 12 else '')
            body = bodies.get(self.path, 'not found')
            self.send_response(200 if self.path in bodies else 404)
            self.send_header('Content-Type', 'application/rss+xml' if self.path == '/feed.xml' else 'text/html')
            self.end_headers()
            self.wfile.write(body.encode())
    class Corpus(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            corpus_requests.append((time.monotonic(), self.path))
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/v1/text':
                items = payload['items']
                assert all(item['url'].endswith('/') for item in items)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'results': [{'requested_url': item['url'],
                    'matched_url': item['url'], 'lookup_status': 'found', 'content_type': 'text/html',
                    'html_text': {'status': 'available', 'truncated': False, 'text': 'Website Child'}} for item in items]}).encode())
                return
            assert self.path == '/v1/held'
            query = payload['urls'][0]
            queries[query] += 1
            bad = state['fail'] and query.endswith('/child')
            self.send_response(503 if bad else 200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'results': [{'url': query, 'held': query.endswith('/'), 'match': 'exact', 'matched_url': query}]}).encode())
    website = ThreadingHTTPServer(('127.0.0.1', 0), Website)
    corpus = ThreadingHTTPServer(('127.0.0.1', 0), Corpus)
    for server in [website, corpus]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{website.server_port}/'
    config = AuditConfig(target_url=base, corpus_url=f'http://127.0.0.1:{corpus.server_port}/',
        database_path=tmp_path / 'audit.sqlite', output_dir=tmp_path / 'reports', discovery_only=True,
        max_retries=0, corpus_interval_seconds=0.04, corpus_batch_size=1)
    try:
        discovery = CoverageAuditor(config)
        run_id = discovery.run_id
        result = asyncio.run(discovery.run())
        assert result['status'] == 'AWAITING_CORPUS'
        assert corpus_requests == []
        assert result['found_urls'] == result['missing_urls'] == 0
        assert result['expected_urls'] >= 17
        requests_before_check = len(website_requests)
        with sqlite3.connect(config.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM corpus_matches').fetchone()[0] == 0
            assert db.execute('SELECT MAX(navigation_depth) FROM urls').fetchone()[0] == 13
        checked = asyncio.run(CoverageAuditor(config, run_id=run_id).verify_inventory())
        assert checked['status'] == 'CORPUS_PAUSED'
        assert checked['found_urls'] == 1
        assert checked['missing_urls'] == 0
        assert checked['unverified_urls'] > 1
        assert len(website_requests) == requests_before_check
        state['fail'] = False
        resumed = asyncio.run(CoverageAuditor(config, run_id=run_id).verify_inventory())
        assert resumed['status'] == 'LIMITED'  # Discovery/browser and historical outage boundaries remain disclosed.
        assert resumed['unverified_urls'] == 0
        assert resumed['missing_urls'] == resumed['expected_urls'] - 1
        assert queries[base] == 1  # Confirmed presence is not searched again on resume.
        assert len(website_requests) == requests_before_check
        gaps = [later[0] - earlier[0] for earlier, later in zip(corpus_requests, corpus_requests[1:])]
        # A new explicitly started phase can begin immediately; requests within each client are paced.
        assert sum(gap >= 0.03 for gap in gaps) >= len(gaps) - 1
    finally:
        for server in [website, corpus]:
            server.shutdown()
            server.server_close()
