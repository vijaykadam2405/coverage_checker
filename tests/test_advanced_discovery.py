"""Offline regression fixtures for additive discovery and preserved audit rules."""

import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from test_corpus_text_api import text_result
from test_coverage_api import mock_api
from test_coverage_api import response as held_result

from crawler_coverage_auditor.analysis.reconciliation import discovery_reconciliation
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import (
    DEFAULT_DISCOVERY_SOURCES,
    LEGACY_DISCOVERY_SOURCES,
    AuditConfig,
    upgraded_discovery_sources,
)
from crawler_coverage_auditor.discovery.advanced import (
    manifest_links,
    metadata_links,
    public_candidate,
    source_tasks,
)
from crawler_coverage_auditor.discovery.filters import (
    dependent_controls,
    detect_filters,
    get_filter_urls,
)
from crawler_coverage_auditor.discovery.javascript import JavaScriptMiner
from crawler_coverage_auditor.discovery.sitemap import discover_sitemaps
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.validation.http import FetchResult, PoliteHttpClient


def auditor(tmp_path, **settings):
    return CoverageAuditor(
        AuditConfig(
            target_url="https://example.test/",
            database_path=tmp_path / "audit.sqlite",
            output_dir=tmp_path / "reports",
            respect_robots=False,
            discovery_sources=set(DEFAULT_DISCOVERY_SOURCES),
            **settings,
        )
    )


@pytest.mark.parametrize(
    "failure,status",
    [
        ("DNS_FAILURE", 0),
        ("FETCH_ERROR", 0),
        ("TIMEOUT", 0),
        ("SSL_ERROR", 0),
        ("RATE_LIMITED", 429),
        ("HTTP_ERROR", 503),
    ],
)
def test_failed_retry_keeps_success_and_empty_retry_cannot_erase_text(
    tmp_path, failure, status
):
    a = auditor(tmp_path)
    try:
        url = a.config.target_url
        a._add_discovery(url, "STATIC", 0, None, "")
        a.db.add_http_result(
            a.run_id,
            url,
            status_code=200,
            validity="VALID_PAGE",
            text_length=15,
            title="Saved page",
        )
        a.db.add_page_snapshot(a.run_id, url, "Saved page text")
        a.db.add_corpus_match(a.run_id, url, "NOT_FOUND", None, None, None, None)
        a.db.set_url_state(a.run_id, url, "DONE")
        a.db.add_http_result(
            a.run_id,
            url,
            status_code=status,
            validity=failure,
            error="temporary failure",
            text_length=0,
        )
        a.db.add_page_snapshot(a.run_id, url, "")
        row = a.db.report_rows(a.run_id)[0]
        assert row["validity"] == "VALID_PAGE" and row["title"] == "Saved page"
        assert row["last_attempt_validity"] == failure
        assert a.db.page_snapshot(a.run_id, url) == "Saved page text"
        m = queries.summary(a.config.database_path, a.run_id)["metrics"]
        assert (
            m["checked"] == m["saved_corpus_checks"] == m["retained_after_retry"] == 1
        )
    finally:
        a.db.close()


def test_genuine_deletion_changes_current_eligibility_but_keeps_historical_text(
    tmp_path,
):
    a = auditor(tmp_path)
    try:
        url = a.config.target_url
        a._add_discovery(url, "STATIC", 0, None, "")
        a.db.add_http_result(a.run_id, url, status_code=200, validity="VALID_PAGE")
        a.db.add_page_snapshot(a.run_id, url, "Good text")
        a.db.add_http_result(a.run_id, url, status_code=404, validity="HTTP_ERROR")
        assert a.db.report_rows(a.run_id)[0]["validity"] == "HTTP_ERROR"
        assert a.db.page_snapshot(a.run_id, url) == "Good text"
        a.db.add_page_snapshot(a.run_id, url, "New good text")
        assert (
            a.db.connection.execute(
                "SELECT cleaned_text FROM page_snapshot_history"
            ).fetchone()[0]
            == "Good text"
        )
    finally:
        a.db.close()


def test_metadata_templates_framework_data_and_comments_are_candidates_not_proof():
    html = """<area href="/map"><div data-route="/route"></div><div data-target="#panel"></div>
    <script id="__NUXT_DATA__" type="application/json">{"link":"/old-article"}</script>
    <script type="text/template"><a href="/template">Item</a></script>
    <!-- <a href="/comment">Old link</a> --><meta http-equiv="refresh" content="0;url=/moved">
    <meta property="og:image" content="/image.png"> https://example.test/plain-text-link
    <a href="/logout">Do not infer an action</a>"""
    found = {link.url for link in metadata_links(html, "https://example.test/")}
    assert {
        f"https://example.test/{path}"
        for path in (
            "map",
            "route",
            "old-article",
            "template",
            "comment",
            "moved",
            "image.png",
            "plain-text-link",
        )
    } <= found
    assert "https://example.test/#panel" not in found


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test/",
        "/logout",
        "/.git/config",
        "/backup.sql",
        "/api?api_key=secret",
        "/x?access_token=secret",
    ],
)
def test_new_candidate_extractors_do_not_schedule_credentials_or_account_actions(url):
    assert public_candidate(url, "https://example.test/") is None


def test_published_sources_not_blind_endpoint_generation():
    html = '<script src="/app.js"></script><link rel="manifest" href="/manifest.json"><link rel="https://api.w.org/" href="/wp-json/">'
    assert set(source_tasks(html, "https://example.test/")) == {
        ("SCRIPT", "https://example.test/app.js"),
        ("MANIFEST", "https://example.test/manifest.json"),
        ("WORDPRESS_ROOT", "https://example.test/wp-json/"),
    }
    assert source_tasks("<h1>Static article</h1>", "https://example.test/") == []
    assert [
        l.url
        for l in manifest_links(
            {
                "start_url": "/home",
                "scope": "/not-a-page",
                "shortcuts": [{"url": "/archive"}],
            },
            "https://example.test/manifest.json",
        )
    ] == ["https://example.test/home", "https://example.test/archive"]


def test_acorn_does_not_execute_input_and_patterns_are_separate():
    async def run():
        miner = JavaScriptMiner()
        try:
            value = await miner.parse(
                'throw new Error("must never execute"); const routes=["/old-page","/news/:slug","/product/[id]"];'
            )
            assert not value.get("error")
            assert value["urls"] == ["/old-page"]
            assert set(value["patterns"]) == {"/news/:slug", "/product/[id]"}
            process = miner.process
            assert "/second" in (await miner.parse('const path="/second";'))["urls"]
            assert miner.process is process
        finally:
            await miner.close()

    asyncio.run(run())


def test_task_resume_and_module_versions_do_not_claim_old_work_done(tmp_path):
    a = auditor(tmp_path)
    try:
        tasks = a.advanced.tasks
        assert tasks.enqueue(
            "SCRIPT", "https://example.test/a.js", a.config.target_url, 0
        )
        assert not tasks.enqueue(
            "SCRIPT", "https://example.test/a.js", a.config.target_url, 0
        )
        task = tasks.pending()
        tasks.save(task, "RUNNING", cursor={"position": 3})
        tasks.restore()
        assert json.loads(tasks.pending()["cursor_json"]) == {"position": 3}
        tasks.save(task, "DONE")
        tasks.VERSION = 2
        assert tasks.enqueue(
            "SCRIPT", "https://example.test/a.js", a.config.target_url, 0
        )
        assert tasks.pending()["version"] == 2
    finally:
        a.db.close()


def test_multi_value_dependencies_are_preserved_in_get_urls():
    control = detect_filters(
        '<form><select name="year"><option value="2021">2021</option></select></form>',
        "https://example.test/news",
    )[0]
    url = get_filter_urls(
        control, "https://example.test/news?category[]=a&category[]=b&page=8"
    )[0]
    assert (
        "category%5B%5D=a" in url and "category%5B%5D=b" in url and "page=8" not in url
    )


def test_filter_dependencies_require_observed_option_changes():
    baseline = [
        {
            "name": "category",
            "type": "select",
            "method": "javascript",
            "unsafe": False,
            "values": [{"value": "a", "label": "A"}],
        }
    ]
    assert (
        dependent_controls(baseline, baseline, {"name": "year", "value": "2021"}) == []
    )
    changed = [dict(baseline[0], values=[{"value": "old", "label": "Old"}])]
    dependency = dependent_controls(
        baseline, changed, {"name": "year", "type": "select", "value": "2021"}
    )[0]
    assert dependency["setup"][0]["value"] == "2021"


def test_gzip_large_sitemap_and_resource_extensions_are_enumerated():
    # Valid sitemap larger than the former 8 MB HTML response budget.
    body = b'<urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"><url><loc>https://example.test/page</loc><image:image><image:loc>https://example.test/a.png</image:loc></image:image></url>'
    body += b" " * (9 * 1024 * 1024) + b"</urlset>"
    urls = []

    class Client:
        async def get(self, url, **kwargs):
            return FetchResult(url, url, 404, {}, b"")

        @asynccontextmanager
        async def metadata_stream(self, url):
            yield (
                FetchResult(
                    url, url, 200 if url.endswith("/sitemap.xml") else 404, {}, b""
                ),
                BytesIO(gzip.compress(body) if url.endswith("/sitemap.xml") else b""),
            )

    asyncio.run(
        discover_sitemaps(
            "https://example.test/",
            Client(),
            on_sitemap=lambda *args: None,
            on_url=lambda url, *args: urls.append(url),
        )
    )
    assert urls == ["https://example.test/page", "https://example.test/a.png"]


def test_text_and_feed_sitemaps_are_supported_and_entities_rejected():
    urls = []
    errors = []

    class Client:
        async def get(self, url, **kwargs):
            bodies = {
                "robots.txt": b"Sitemap: /urls.txt\nSitemap: /feed.xml\nSitemap: /bad.xml",
                "urls.txt": b"https://example.test/text\n",
                "feed.xml": b"<rss><channel><item><link>https://example.test/feed-item</link></item></channel></rss>",
                "bad.xml": b'<!DOCTYPE x [<!ENTITY x "BAD">]><urlset><url><loc>&x;</loc></url></urlset>',
            }
            key = url.rsplit("/", 1)[-1]
            body = bodies.get(key, b"")
            return FetchResult(url, url, 200 if body else 404, {}, body)

    asyncio.run(
        discover_sitemaps(
            "https://example.test/",
            Client(),
            on_sitemap=lambda *args: None,
            on_url=lambda url, *args: urls.append(url),
            on_error=lambda *args: errors.append(args),
        )
    )
    assert set(urls) == {"https://example.test/text", "https://example.test/feed-item"}
    assert any("DTD/entities" in item[1] for item in errors)


def test_metadata_stream_does_not_download_pdf_body(monkeypatch):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("PDF body must not be read")
            yield b""

    async def run():
        client = PoliteHttpClient(1, 5, 0, "test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, headers={"content-type": "application/pdf"}, stream=Body()
                )
            )
        )
        async with client:
            async with client.metadata_stream("https://example.test/sitemap.xml") as (
                response,
                stream,
            ):
                assert response.url_only and stream.read() == b""

    asyncio.run(run())


def test_document_extension_cannot_be_downloaded_by_spoofing_json_mime():
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Known document URL must remain body-free")
            yield b""

    async def run():
        client = PoliteHttpClient(1, 5, 0, "test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=Body()
                )
            )
        )
        async with client:
            response = await client.get(
                "https://example.test/manual.pdf", discovery_metadata=True
            )
            assert response.url_only

    asyncio.run(run())


def test_existing_explicit_discovery_selection_is_preserved():
    assert upgraded_discovery_sources({"discovery_sources": ["STATIC"]}) == {"STATIC"}
    assert upgraded_discovery_sources(
        {"discovery_sources": list(LEGACY_DISCOVERY_SOURCES)}
    ) == set(DEFAULT_DISCOVERY_SOURCES)


def test_public_collection_counts_do_not_claim_website_completeness():
    tasks = [
        {
            "kind": "WORDPRESS_LIST",
            "url": f"https://example.test/wp-json/wp/v2/posts?page={page}",
            "state": "DONE",
            "evidence_json": json.dumps(
                {
                    "item_ids": [page],
                    "page": page,
                    "reported_total": 2,
                    "reported_pages": 2,
                }
            ),
        }
        for page in (1, 2)
    ]
    result = discovery_reconciliation(tasks)
    assert result["collections"][0]["Status"] == "Published counts reconciled"
    tasks[1]["evidence_json"] = json.dumps(
        {"item_ids": [1], "page": 2, "reported_total": 2, "reported_pages": 2}
    )
    assert (
        discovery_reconciliation(tasks)["collections"][0]["Status"]
        == "Unresolved or changing collection"
    )


def test_new_discovery_sources_feed_existing_serial_corpus_pipeline(
    tmp_path, monkeypatch
):
    events = []
    html = "<html><body><main><h1>Beginning middle ending</h1></main></body></html>"
    root = html.replace(
        "</body>",
        '<script src="/app.js"></script><link rel="manifest" href="/manifest.json"><link rel="https://api.w.org/" href="/wp-json/"></body>',
    )

    class Website:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def head(self, url, **kwargs):
            events.append(('website', url))
            return FetchResult(url, url, 200, {'content-type': 'application/pdf'}, b'', url_only=True)

        async def get(self, url, **kwargs):
            events.append(("website", url))
            mime = "text/html"
            if url.endswith("/robots.txt") or "/sitemap" in url:
                return FetchResult(url, url, 404, {}, b"")
            if url.endswith("/"):
                body = (
                    root
                    if url == "https://example.test/"
                    else json.dumps({"routes": {"/wp/v2/posts": {"methods": ["GET"]}}})
                )
                mime = (
                    "text/html"
                    if url == "https://example.test/"
                    else "application/json"
                )
            elif url.endswith("/app.js"):
                body = 'const routes=["/js-only","/download.pdf","/news/:slug"];'
                mime = "application/javascript"
            elif url.endswith("/manifest.json"):
                body = json.dumps({"start_url": "/manifest-only"})
                mime = "application/json"
            elif "/wp/v2/posts?" in url:
                body = json.dumps([{"id": 1, "link": "https://example.test/api-only"}])
                mime = "application/json"
                return FetchResult(
                    url,
                    url,
                    200,
                    {"content-type": mime, "x-wp-total": "1", "x-wp-totalpages": "1"},
                    body.encode(),
                )
            else:
                body = html
            assert not url.endswith(".pdf")
            return FetchResult(url, url, 200, {"content-type": mime}, body.encode())

    monkeypatch.setattr("crawler_coverage_auditor.auditor.PoliteHttpClient", Website)

    def handler(request):
        payload = json.loads(request.content)
        urls = payload.get("urls") or [item["url"] for item in payload["items"]]
        assert len(urls) == 1
        url = urls[0]
        kind = "presence" if request.url.path == "/v1/held" else "text"
        events.append((kind, url))
        assert kind != "text" or not url.endswith(".pdf")
        return httpx.Response(
            200,
            json={
                "results": [
                    held_result(url)
                    if kind == "presence"
                    else text_result(url, "Beginning middle ending")
                ]
            },
        )

    mock_api(monkeypatch, handler)
    a = auditor(tmp_path, corpus_url="https://api.test/", capture_types={"HTML", "PDF"})
    run_id = a.run_id
    assert (
        asyncio.run(a.run())["status"] == "LIMITED"
    )  # Browser disabled in this HTTP-only fixture.
    summary = queries.summary(a.config.database_path, run_id)
    urls = {
        row["url"] for row in queries.url_page(a.config.database_path, run_id)["items"]
    }
    assert {
        "https://example.test/js-only",
        "https://example.test/api-only",
        "https://example.test/manifest-only",
        "https://example.test/download.pdf",
    } <= urls
    assert not any(":slug" in url for url in urls)
    for url in urls:
        if not url.endswith(".pdf"):
            assert (
                events.index(("website", url))
                < events.index(("presence", url))
                < events.index(("text", url))
            )
    assert sum(op == "website" and url.endswith("/app.js") for op, url in events) == 1
    assert summary["metrics"]["unverified"] == 0
    assert not any(item["state"] == "FAILED" for item in summary["discovery_tasks"])
