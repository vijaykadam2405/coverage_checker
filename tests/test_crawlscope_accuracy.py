from __future__ import annotations

import asyncio
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crawler_coverage_auditor.analysis.coverage import coverage_summary
from crawler_coverage_auditor.analysis.states import public_states
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.pagination import (
    numeric_page_parameter,
    replace_page_parameter,
)
from crawler_coverage_auditor.discovery.resources import json_links, trap_reason
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL


def test_pending_and_failed_corpus_checks_are_not_confirmed_misses():
    rows = [{"validity": "VALID_PAGE", "page_type": "HTML", "match_status": value} for value in ["EXACT_MATCH", "NOT_FOUND", "REVIEW_REQUIRED", None]]
    summary = coverage_summary(rows)
    assert summary["found_urls"] == 1
    assert summary["missing_urls"] == 1
    assert summary["unverified_urls"] == 2
    assert summary["url_coverage_percent"] == 50
    assert summary['coverage_lower_percent'] == 25
    assert summary['verification_completion_percent'] == 50
    assert summary["coverage_upper_percent"] == 75


def test_images_are_separate_from_content_coverage():
    rows = [{"validity": "VALID_PAGE", "page_type": "HTML", "match_status": "EXACT_MATCH"},
            {"validity": "VALID_PAGE", "page_type": "IMAGE", "match_status": "NOT_FOUND"}]
    assert coverage_summary(rows)["url_coverage_percent"] == 100


def test_missing_text_differs_from_unavailable_live_text():
    base = {'page_type': 'HTML', 'validity': 'VALID_PAGE'}
    assert public_states(base | {'text_length': 0})['text_label'] == 'No readable live text'
    assert public_states(base | {'text_length': None})['text_label'] == 'Not assessed'
    assert public_states(base | {'text_length': 150})['text_label'] == 'Live text captured'


def test_path_pagination_and_api_supplied_links():
    url = "https://example.test/news/page/487/?year=2021"
    assert numeric_page_parameter(url) == ("__path__", 487)
    assert replace_page_parameter(url, "__path__", 512) == "https://example.test/news/page/512/?year=2021"
    assert [link.url for link in json_links('{"items":[{"url":"/old/article"}],"next":"/api/items?cursor=abc"}', "https://example.test/api")] == ["https://example.test/old/article", "https://example.test/api/items?cursor=abc"]
    assert trap_reason("https://example.test/a/b/c/d/e/f/g/h/i/j/k/l") is None
    assert trap_reason("https://example.test/news/news/news")


def test_navigation_depth_uses_link_graph_even_when_sitemap_discovered_first(tmp_path):
    database = AuditDatabase(tmp_path / "depth.sqlite")
    run_id = database.create_run("https://example.test/", "https://corpus.test/", {})
    seed = "https://example.test/"
    database.add_url(run_id, DiscoveredURL(seed, seed, "SEED", 0))
    previous = seed
    for depth in range(1, 16):
        url = f"https://example.test/nested-{depth}"
        database.add_url(run_id, DiscoveredURL(url, url, "SITEMAP", 1, "https://example.test/sitemap.xml"))
        database.add_url(run_id, DiscoveredURL(url, url, "STATIC", depth, previous))
        previous = url
    database.reconcile_navigation_depth(run_id)
    result = database.connection.execute("SELECT min_depth,navigation_depth FROM urls WHERE normalized_url=?", (previous,)).fetchone()
    assert tuple(result) == (1, 15)
    database.close()


class BoundaryHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        assert self.path == '/v1/held'
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(503)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/data/search"):
            self.send_response(200 if "coverage-audit-probe" in self.path else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"rows":[]}')
        elif self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Archive</h1><a href='/news?page=2'>News</a>")
        elif self.path.startswith("/news"):
            from urllib.parse import parse_qs, urlsplit
            page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
            self.send_response(429 if page >= 4 else 200)
            self.end_headers()
            self.wfile.write(f"<h1>Page {page}</h1><article>Unique article {page}</article><a href='/news?page=2'>Next</a>".encode())
        else:
            self.send_response(404)
            self.end_headers()


def test_corpus_failure_and_rate_limit_leave_explicit_uncertainty(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), BoundaryHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        config = AuditConfig(target_url=base, corpus_url=base, database_path=tmp_path / "audit.sqlite", output_dir=tmp_path / "reports", max_retries=0)
        result = asyncio.run(CoverageAuditor(config).run())
        assert result["missing_urls"] == 0
        assert result["unverified_urls"] == 1  # Next page is queued, not validated after Corpus fails.
        assert result["status"] == "CORPUS_PAUSED"
        with sqlite3.connect(config.database_path) as connection:
            assert connection.execute("SELECT status,max_page FROM pagination_series").fetchone() is None
            assert connection.execute('SELECT COUNT(*) FROM pagination_tasks').fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE kind='CORPUS_CHECKING'").fetchone()[0] == 1
            reasons = " ".join(row[0] for row in connection.execute("SELECT reason FROM audit_limits"))
            assert "503" in reasons  # Corpus failure stops exploration before website probes.
        html = (tmp_path / "reports" / "coverage_report.html").read_text(encoding="utf-8")
        assert "503" in html and "last_successful_url" in html
    finally:
        server.shutdown()
        server.server_close()
