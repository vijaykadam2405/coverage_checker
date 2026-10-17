import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from test_advanced_discovery import auditor
from test_serial_scan import serial_fixture

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import (
    DEFAULT_DISCOVERY_SOURCES,
    LEGACY_DISCOVERY_SOURCES,
)
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.validation.http import FetchResult


def test_wordpress_continuation_is_response_backed_and_no_api_body_text_is_saved(
    tmp_path,
):
    a = auditor(tmp_path)
    try:
        task = {
            "kind": "WORDPRESS_LIST",
            "parent_url": a.config.target_url,
            "url": "https://example.test/wp-json/wp/v2/posts?page=1&per_page=100",
        }
        response = FetchResult(
            task["url"],
            task["url"],
            200,
            {"x-wp-total": "2", "x-wp-totalpages": "2"},
            json.dumps([{"id": 1, "link": "https://example.test/article"}]).encode(),
        )
        result = asyncio.run(a.advanced._parse(task, response))
        assert result["links"] == ["https://example.test/article"]
        assert parse_qs(urlsplit(result["tasks"][0][1]).query)["page"] == ["2"]
        assert result["evidence"]["reported_total"] == 2
        assert (
            a.db.connection.execute("SELECT count(*) FROM page_snapshots").fetchone()[0]
            == 0
        )
    finally:
        a.db.close()


def test_resourcesync_uses_advertised_lists_and_never_follows_dumps_or_deleted_files(
    tmp_path,
):
    a = auditor(tmp_path)
    try:
        task = {
            "kind": "RESOURCESYNC",
            "url": "https://example.test/capability.xml",
            "parent_url": a.config.target_url,
        }

        def parse(body):
            return asyncio.run(
                a.advanced._parse(
                    task, FetchResult(task["url"], task["url"], 200, {}, body.encode())
                )
            )

        result = parse(
            '<urlset xmlns:rs="http://www.openarchives.org/rs/terms/"><rs:md capability="capabilitylist"/><url><loc>https://example.test/list.xml</loc><rs:md capability="resourcelist"/></url><url><loc>https://example.test/dump.zip</loc><rs:md capability="resourcedump"/></url></urlset>'
        )
        assert result["tasks"] == [("RESOURCESYNC", "https://example.test/list.xml")]
        result = parse(
            '<urlset xmlns:rs="http://www.openarchives.org/rs/terms/"><rs:md capability="changelist" from="2026-01-01" until="2026-01-02"/><url><loc>https://example.test/new.pdf</loc><rs:md change="created"/></url><url><loc>https://example.test/removed.pdf</loc><rs:md change="deleted"/></url></urlset>'
        )
        assert result["links"] == ["https://example.test/new.pdf"]
        assert result["evidence"]["from"] == "2026-01-01"
        with pytest.raises(ValueError):
            parse("<!DOCTYPE x><urlset/>")
        with pytest.raises(ValueError):
            parse("<broken>")
    finally:
        a.db.close()


@pytest.mark.parametrize(
    "columns,row",
    [
        (["original"], ["https://example.test/old"]),
        (["original", "timestamp"], ["https://example.test/old", "20200101000000"]),
    ],
)
def test_wayback_continuation_is_not_mistaken_for_content_url(tmp_path, columns, row):
    a = auditor(tmp_path)
    try:
        url = "https://web.archive.org/cdx/search/cdx?url=example.test%2F*&showResumeKey=true"
        task = {"kind": "WAYBACK", "url": url, "parent_url": a.config.target_url}
        result = asyncio.run(
            a.advanced._parse(
                task,
                FetchResult(
                    url,
                    url,
                    200,
                    {},
                    json.dumps([columns, row, [], ["resume-next"]]).encode(),
                ),
            )
        )
        assert result["links"] == ["https://example.test/old"]
        assert parse_qs(urlsplit(result["tasks"][0][1]).query)["resumeKey"] == [
            "resume-next"
        ]
    finally:
        a.db.close()


def test_commoncrawl_pages_continue_beyond_first_page_without_internal_parameter_egress(
    tmp_path,
):
    a = auditor(tmp_path)
    urls = []

    class Client:
        async def get(self, url, **kwargs):
            urls.append(url)
            assert "crawlscope_pages" not in url
            payload = (
                {"pages": 3}
                if "showNumPages=true" in url
                else {"url": "https://example.test/from-index"}
            )
            return FetchResult(
                url,
                url,
                200,
                {"content-type": "application/json"},
                json.dumps(payload).encode(),
            )

    try:
        a.config.discovery_sources.add("EXTERNAL_INDEX")
        a.advanced.enqueue(
            "COMMONCRAWL",
            "https://index.commoncrawl.org/CC-MAIN-2026-39-index?url=example.test%2F*&output=json&showNumPages=true&pageSize=1",
            a.config.target_url,
            0,
        )

        async def drain():
            while await a.advanced.step(Client()):
                pass

        asyncio.run(drain())
        assert len(urls) == 4
        assert {
            parse_qs(urlsplit(url).query).get("page", ["count"])[0] for url in urls
        } == {"count", "0", "1", "2"}
        assert len(a.db.report_rows(a.run_id)) == 1
    finally:
        a.db.close()


def test_expired_corpus_results_refresh_serially_without_refetching_completed_website(
    tmp_path, monkeypatch
):
    config, events, _ = serial_fixture(tmp_path, monkeypatch)
    first = CoverageAuditor(config)
    run_id = first.run_id
    asyncio.run(first.run())
    website = [event for event in events if event[0] == "website"]
    with sqlite3.connect(config.database_path) as c:
        c.execute("UPDATE corpus_matches SET checked_at='2000-01-01T00:00:00+00:00'")
    config.resume = True
    asyncio.run(CoverageAuditor(config, run_id=run_id).run())
    assert [event for event in events if event[0] == "website"] == website
    assert events.count(("presence", config.target_url)) == 2
    assert events.count(("text", config.target_url)) == 2


def test_old_inventory_upgrade_retains_urls_matches_and_requeues_only_saved_sources(
    tmp_path,
):
    a = auditor(tmp_path)
    try:
        url = a.config.target_url + "old"
        a._add_discovery(url, "STATIC", 1, None, "")
        a.db.add_http_result(a.run_id, url, status_code=200, validity="VALID_PAGE")
        a.db.add_page_snapshot(a.run_id, url, "Saved meaningful text")
        a.db.add_corpus_match(a.run_id, url, "NOT_FOUND", None, None, None, None)
        a.db.set_url_state(a.run_id, url, "DONE")
        a.progress.mark(url, "COMPLETED")
        a._add_discovery(a.config.target_url + "pending", "STATIC", 2, None, "")
        assert a.advanced.prepare_upgrade() == 1
        assert a.db.connection.execute("SELECT count(*) FROM urls").fetchone()[0] == 2
        assert a.db.page_snapshot(a.run_id, url) == "Saved meaningful text"
        assert (
            a.db.report_rows(a.run_id, only_url=url)[0]["match_status"] == "NOT_FOUND"
        )
        assert not a.progress.url(url)["website_completed"]
    finally:
        a.db.close()


def test_old_complete_default_inventory_routes_to_discovery_upgrade_not_corpus_only(
    tmp_path, monkeypatch
):
    path = tmp_path / "audit.sqlite"
    db = AuditDatabase(path)
    run = db.create_run(
        "https://example.test/",
        "https://api.test/",
        {"discovery_sources": list(LEGACY_DISCOVERY_SOURCES)},
    )
    db.inventory_ready(run)
    db.finish_run(run, "COMPLETED")
    db.close()
    jobs = AuditJobs()
    monkeypatch.setattr(jobs, "_launch", lambda config, *args: config)
    config = jobs.resume(run, path, tmp_path / "reports")
    assert config.resume and config.discovery_sources == (set(DEFAULT_DISCOVERY_SOURCES) | {'EXTERNAL_INDEX'})


def test_literal_module_imports_are_resolved_against_script_not_page(tmp_path):
    a = auditor(tmp_path)

    async def run():
        try:
            task = {
                "kind": "SCRIPT",
                "url": "https://example.test/assets/main.js",
                "parent_url": a.config.target_url,
            }
            body = b'import "./archive-chunk.js"; const routes=["/old-article"];'
            result = await a.advanced._parse(
                task,
                FetchResult(
                    task["url"],
                    task["url"],
                    200,
                    {"content-type": "application/javascript"},
                    body,
                ),
            )
            assert result["tasks"] == [
                ("SCRIPT", "https://example.test/assets/archive-chunk.js")
            ]
            assert result["links"] == ["https://example.test/old-article"]
        finally:
            await a.advanced.close()

    try:
        asyncio.run(run())
    finally:
        a.db.close()


def test_failed_metadata_source_retries_on_resume_and_old_error_becomes_history(
    tmp_path,
):
    a = auditor(tmp_path)
    failing = [True]

    class Client:
        async def get(self, url, **kwargs):
            return FetchResult(
                url,
                url,
                503 if failing[0] else 200,
                {"content-type": "application/json"},
                b'{"start_url":"/recovered"}',
            )

    async def run():
        a.advanced.enqueue(
            "MANIFEST", "https://example.test/manifest.json", a.config.target_url, 0
        )
        await a.advanced.step(Client())
        assert a.advanced.tasks.pending() is None and a._limited
        failing[0] = False
        a.advanced.tasks.restore(retry_failed=True)
        await a.advanced.step(Client())
        assert not a._limited
        assert (
            a.db.connection.execute("SELECT stage FROM audit_limits").fetchone()[0]
            == "Historical discovery retry"
        )
        assert a.db.report_rows(a.run_id)[0]["url"] == "https://example.test/recovered"

    try:
        asyncio.run(run())
    finally:
        a.db.close()


class Dependencies(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        html = """<html><body><main><h1>Archive</h1><select name="year"><option value="2026">2026</option><option value="2021">2021</option></select>
        <select name="category"><option value="">Choose year</option></select><div id="results"></div></main>
        <script>document.querySelector('[name=year]').onchange=function(){let value=this.value==='2021'?'old':'new';document.querySelector('[name=category]').innerHTML='<option value="'+value+'">'+value+'</option>';};
        document.querySelector('[name=category]').onchange=function(){document.querySelector('#results').innerHTML='<a href="/archive/'+document.querySelector('[name=year]').value+'/'+this.value+'">Archive article</a>';};</script></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


def test_browser_explores_values_only_revealed_by_parent_filter():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Dependencies)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    async def run():
        async with BrowserExplorer(
            sources={"RENDERED", "FILTER_BROWSER", "STATIC"}
        ) as browser:
            assert not browser.error
            return await browser.explore(base + "/")

    try:
        result = asyncio.run(run())
        assert not result.error, result.error
        assert base + "/archive/2021/old" in {link.url for link in result.links}
    finally:
        server.shutdown()
        server.server_close()
