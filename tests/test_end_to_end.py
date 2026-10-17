import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig


class _FixtureHandler(BaseHTTPRequestHandler):
    base_url = ""

    def log_message(self, *_: object) -> None:
        return

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        assert self.headers['Authorization'] == 'Bearer test-only-key'
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/v1/text':
            text = {'/': 'Home About News Report 2024 2023', '/about': 'About Captured page.',
                    '/news?page=1': 'News 1 Unique article collection number 1 Next',
                    '/news?page=2': 'News 2 Unique article collection number 2 Next'}
            return self._send(200, json.dumps({'results': [{'requested_url': item['url'],
                'matched_url': item['url'], 'lookup_status': 'found', 'content_type': 'text/html',
                'html_text': {'status': 'available', 'truncated': False, 'text': text[item['url'].removeprefix(self.base_url)]}}
                for item in payload['items']]}), 'application/json')
        assert self.path == '/v1/held'
        urls = payload['urls']
        held = {self.base_url + path for path in ('/', '/about', '/news?page=1', '/news?page=2')}
        self._send(200, json.dumps({'results': [{'url': url, 'held': url in held,
            'match': 'exact' if url in held else None, 'matched_url': url if url in held else None} for url in urls]}), 'application/json')

    def do_HEAD(self) -> None:
        self.send_response(200 if self.path == '/docs/report.pdf' else 404)
        self.send_header('Content-Type', 'application/pdf')
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        base = self.base_url
        if self.path == "/robots.txt":
            return self._send(200, f"Sitemap: {base}/sitemap.xml\n", "text/plain")
        if self.path == "/sitemap.xml":
            xml = f"""<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
            <url><loc>{base}/</loc></url><url><loc>{base}/archive/old</loc></url></urlset>"""
            return self._send(200, xml, "application/xml")
        if self.path == "/":
            return self._send(200, """<title>Home</title><h1>Home</h1><a href='/about'>About</a><a href='/news?page=2'>News</a><a href='/docs/report.pdf'>Report</a>
            <form action='/news' method='get'><select name='year'><option value='2024'>2024</option><option value='2023'>2023</option></select></form>""")
        if self.path == "/about":
            return self._send(200, "<title>About</title><h1>About</h1><p>Captured page.</p>")
        if self.path == "/archive/old":
            return self._send(200, "<title>Old archive</title><h1>Old archive</h1><p>Valid historical material.</p>")
        if self.path.startswith("/news"):
            from urllib.parse import parse_qs, urlsplit
            page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
            if page > 4:
                return self._send(200, "<title>No results</title><h1>No results found</h1>")
            return self._send(200, f"<title>News {page}</title><h1>News {page}</h1><article>Unique article collection number {page}</article><a href='/news?page=2'>Next</a>")
        if self.path == "/docs/report.pdf":
            return self._send(200, "%PDF-test", "application/pdf")
        if self.path.startswith("/api/data/search"):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            results = [{"id": "home", "url": f"{base}/"}, {"id": "about", "url": f"{base}/about"},
                       {"id": "news-1", "url": f"{base}/news?page=1"}, {"id": "news-2", "url": f"{base}/news?page=2"}]
            return self._send(200, json.dumps({"rows": [result for result in results if result["url"] == query]}), "application/json")
        if self.path.startswith("/api/data/doc/"):
            return self._send(200, "captured text", "text/plain")
        return self._send(404, "not found")


def test_end_to_end_static_sitemap_corpus_and_reports(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    _FixtureHandler.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = AuditConfig(target_url=_FixtureHandler.base_url + "/", corpus_url=_FixtureHandler.base_url + "/",
                             output_dir=tmp_path / "reports", database_path=tmp_path / "coverage.sqlite", corpus_interval_seconds=0)
        result = asyncio.run(CoverageAuditor(config).run())
        assert result["expected_urls"] == 11
        assert result["found_urls"] == 4
        assert result["missing_urls"] == 7
        assert result["unverified_urls"] == 0
        assert result['text_checked_urls'] == 4
        assert (tmp_path / "reports" / "coverage_report.xlsx").exists()
        assert (tmp_path / "reports" / "coverage_report.html").exists()
        missing = (tmp_path / "reports" / "missing_from_corpus.csv").read_text(encoding="utf-8")
        assert "/archive/old" in missing
        assert "news?page=3" in missing
        with sqlite3.connect(tmp_path / "coverage.sqlite") as connection:
            series = connection.execute("SELECT max_page, status FROM pagination_series").fetchone()
            filter_values = connection.execute("SELECT request_value FROM filter_values ORDER BY request_value").fetchall()
            event_kinds = {row[0] for row in connection.execute("SELECT kind FROM audit_events")}
            assert {"DISCOVERED", "CORPUS_CHECKING", "FOUND", "MISSING", "FILTER_DETECTED", "PAGINATION_MAX"} <= event_kinds
        assert series == (4, "VERIFIED")
        assert filter_values == [("2023",), ("2024",)]
        incremental = AuditConfig(target_url=_FixtureHandler.base_url + "/", corpus_url=_FixtureHandler.base_url + "/",
                                  output_dir=tmp_path / "incremental", database_path=tmp_path / "coverage.sqlite", incremental=True, corpus_interval_seconds=0)
        asyncio.run(CoverageAuditor(incremental).run())
        with sqlite3.connect(tmp_path / "coverage.sqlite") as connection:
            runs = connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]
            rechecks = connection.execute("SELECT COUNT(*) FROM urls WHERE discovery_methods LIKE '%INCREMENTAL_RECHECK%'").fetchone()[0]
        assert runs == 2
        assert rechecks > 0
    finally:
        server.shutdown()
        server.server_close()
