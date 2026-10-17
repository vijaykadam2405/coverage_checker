"""External files are not target pages; headers, not suffixes, classify files."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from crawler_coverage_auditor.analysis.coverage import coverage_summary
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig, automatic_scan_settings
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.http import PoliteHttpClient
from scripts.remove_external_data import remove_external


def make_auditor(tmp_path):
    return CoverageAuditor(
        AuditConfig(
            target_url="https://example.test/",
            database_path=tmp_path / "audit.sqlite",
            output_dir=tmp_path / "reports",
        )
    )


def test_external_inventory_blocked_for_every_source_and_storage(tmp_path):
    audit = make_auditor(tmp_path)
    try:
        for method in (
            "STATIC",
            "SITEMAP",
            "JS_DISCOVERY",
            "API_DISCOVERY",
            "EXTERNAL_INDEX",
        ):
            for url in (
                "https://external.test/file.pdf",
                "https://external.test/image.png",
                "https://external.test/page",
            ):
                assert not audit._add_discovery(
                    url, method, 1, audit.config.target_url, ""
                )
                assert not audit.db.add_url(
                    audit.run_id, DiscoveredURL(url, url, method, 1)
                )
        assert audit._add_discovery(
            "https://example.test/file.pdf", "STATIC", 1, audit.config.target_url, ""
        )
        assert not automatic_scan_settings({"external_resources": True})[
            "external_resources"
        ]
    finally:
        audit.db.close()


def test_external_redirect_blocked_before_request_but_metadata_sources_work():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "example.test":
            return httpx.Response(
                302, headers={"location": "https://external.test/stories/"}
            )
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json={}
        )

    async def check():
        async with PoliteHttpClient(
            1,
            2,
            0,
            "test",
            target_scope=lambda url: httpx.URL(url).host == "example.test",
        ) as client:
            client._client._transport = httpx.MockTransport(respond)
            response = await client.head("https://example.test/old.pdf")
            assert response.failure_reason == "OUT_OF_SCOPE"
            assert response.final_url == "https://external.test/stories/"
            assert calls == ["https://example.test/old.pdf"]
            assert (
                await client.get("https://external.test/index", discovery_metadata=True)
            ).status_code == 200
            # The ContextVar is reset after the metadata request.
            assert (
                await client.get("https://external.test/page")
            ).failure_reason == "OUT_OF_SCOPE"

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/old.pdf", "/old-article"])
def test_redirected_resource_or_page_excluded_from_corpus_coverage(tmp_path, path):
    audit = make_auditor(tmp_path)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "https://external.test/stories/"}
        )

    async def check():
        async with PoliteHttpClient(
            1, 2, 0, "test", target_scope=audit._in_scope
        ) as client:
            client._client._transport = httpx.MockTransport(respond)
            url = "https://example.test" + path
            audit._add_discovery(url, "STATIC", 1, audit.config.target_url, "")
            await audit._process_url(
                client, dict(audit.db.pending_urls(audit.run_id)[0])
            )
            saved = audit.db.connection.execute(
                "SELECT validity,final_url FROM http_results"
            ).fetchone()
            assert tuple(saved) == ("OUT_OF_SCOPE", "https://external.test/stories/")
            assert (
                audit.db.connection.execute(
                    "SELECT capture_enabled FROM urls"
                ).fetchone()[0]
                == 0
            )
            assert (
                coverage_summary(
                    audit.db.report_rows(audit.run_id, include_traversal=True)
                )["expected_urls"]
                == 0
            )
            assert not audit.db.connection.execute(
                "SELECT 1 FROM page_snapshots"
            ).fetchone()

    try:
        asyncio.run(check())
        assert calls == ["https://example.test" + path]
    finally:
        audit.db.close()


@pytest.mark.parametrize(
    "status,mime,expected",
    [
        (200, "application/pdf", "URL_ONLY"),
        (200, "image/png", "URL_ONLY"),
        (405, "text/html", "RESOURCE_UNVERIFIED"),
        (404, "text/html", "RESOURCE_UNVERIFIED"),
        (200, "", "RESOURCE_UNVERIFIED"),
        (200, "application/x-unknown", "RESOURCE_UNVERIFIED"),
    ],
)
def test_header_only_resource_validation(tmp_path, status, mime, expected):
    audit = make_auditor(tmp_path)
    calls = []

    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("File body must never be read")
            yield b""

    def respond(request):
        calls.append(request.method)
        return httpx.Response(
            status, headers={"content-type": mime}, stream=NeverRead()
        )

    async def check():
        async with PoliteHttpClient(1, 2, 0, "test") as client:
            client._client._transport = httpx.MockTransport(respond)
            url = "https://example.test/report.pdf"
            audit._add_discovery(url, "STATIC", 1, audit.config.target_url, "")
            row = dict(audit.db.pending_urls(audit.run_id)[0])
            await audit._process_url(client, row)
            saved = audit.db.connection.execute("SELECT * FROM http_results").fetchone()
            assert saved["validity"] == expected
            assert saved["text_length"] is None
            assert not audit.db.connection.execute(
                "SELECT 1 FROM page_snapshots"
            ).fetchone()
            if mime == "image/png":
                assert (
                    audit.db.connection.execute(
                        "SELECT page_type FROM urls"
                    ).fetchone()[0]
                    == "IMAGE"
                )

    try:
        asyncio.run(check())
        assert calls == ["HEAD"]
    finally:
        audit.db.close()


def test_old_pdf_redirecting_to_internal_html_reclassified(tmp_path):
    audit = make_auditor(tmp_path)
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/old.pdf":
            return httpx.Response(301, headers={"location": "/article"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Article</title><p>"
            + "Meaningful news content and details. " * 30
            + "</p>",
        )

    async def check():
        async with PoliteHttpClient(1, 2, 0, "test") as client:
            client._client._transport = httpx.MockTransport(respond)
            audit._add_discovery(
                "https://example.test/old.pdf", "STATIC", 1, audit.config.target_url, ""
            )
            await audit._process_url(
                client, dict(audit.db.pending_urls(audit.run_id)[0])
            )
            row = audit.db.connection.execute("SELECT page_type FROM urls").fetchone()
            assert row[0] == "HTML"
            saved = audit.db.connection.execute(
                "SELECT validity,final_url,content_type FROM http_results"
            ).fetchone()
            assert tuple(saved) == (
                "VALID_PAGE",
                "https://example.test/article",
                "text/html",
            )
            assert not audit.db.connection.execute("SELECT 1 FROM documents").fetchone()
            assert audit.db.connection.execute(
                "SELECT 1 FROM page_snapshots"
            ).fetchone()

    try:
        asyncio.run(check())
        assert ("HEAD", "/article") in calls and ("GET", "/article") in calls
    finally:
        audit.db.close()


def test_removal_is_run_scoped_backs_up_and_preserves_subdomains(tmp_path):
    path = tmp_path / "audit.sqlite"
    db = AuditDatabase(path)
    first = db.create_run("https://example.test/", "", {"include_subdomains": True})
    second = db.create_run("https://external.test/", "", {})
    file = "https://external.test/report.pdf"
    internal = "https://sub.example.test/page"
    # Legacy rows must bypass the new admission gate.
    for run, url in ((first, file), (first, internal), (second, file)):
        db.connection.execute(
            "INSERT INTO urls(audit_run_id,url,normalized_url,min_depth,discovery_methods) VALUES(?,?,?,?,?)",
            (run, url, url, 1, '["STATIC"]'),
        )
        db.connection.execute(
            "INSERT INTO documents(audit_run_id,normalized_url) VALUES(?,?)", (run, url)
        )
    db.connection.execute("UPDATE audit_runs SET status='CANCELLED'")
    db.connection.commit()
    db.close()
    result = remove_external(path, tmp_path / "backup")
    assert result["removed_urls"] == 1 and result["per_run"] == {first: 1}
    with sqlite3.connect(path) as saved:
        assert saved.execute(
            "SELECT audit_run_id,normalized_url FROM urls ORDER BY id"
        ).fetchall() == [(first, internal), (second, file)]
        assert saved.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert all(
            not json.loads(row[0])["external_resources"]
            for row in saved.execute("SELECT config_json FROM audit_runs")
        )
        assert not saved.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(tmp_path / "backup" / "coverage.sqlite") as original:
        assert original.execute("SELECT COUNT(*) FROM urls").fetchone()[0] == 3


def test_removal_refuses_running_scan(tmp_path):
    path = tmp_path / "audit.sqlite"
    db = AuditDatabase(path)
    db.create_run("https://example.test/", "", {})
    db.close()
    with pytest.raises(ValueError, match="Pause active scans"):
        remove_external(path, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
