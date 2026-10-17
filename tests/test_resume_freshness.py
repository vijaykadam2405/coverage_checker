"""Resume recovers unfinished work without refreshing an entire old inventory."""

import asyncio
import json
import sqlite3

import httpx
from test_coverage_api import inventory, mock_api, response
from test_serial_scan import serial_fixture

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL


def test_resume_advances_pending_website_without_rechecking_expired_completed_results(
    tmp_path, monkeypatch
):
    config, events, _ = serial_fixture(tmp_path, monkeypatch, render=True)
    first = CoverageAuditor(config)
    run_id = first.run_id
    assert asyncio.run(first.run())["status"] == "COMPLETED"
    fresh = config.target_url + "fresh"
    db = AuditDatabase(config.database_path)
    db.connection.execute(
        "UPDATE corpus_matches SET checked_at='2000-01-01T00:00:00+00:00'"
    )
    db.connection.execute(
        "UPDATE audit_runs SET discovery_completed_at=NULL WHERE id=?", (run_id,)
    )
    db.add_url(run_id, DiscoveredURL(fresh, fresh, "STATIC", 1))
    db.connection.commit()
    db.close()
    config.resume = True
    events.clear()

    assert asyncio.run(CoverageAuditor(config, run_id=run_id).run())["status"] == "COMPLETED"
    assert events == [("website", fresh), ("render", fresh), ("presence", fresh), ("text", fresh)]
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM corpus_matches WHERE checked_at LIKE '2000-%'"
        ).fetchone()[0] == 3


def test_resume_retries_failed_text_even_when_its_presence_result_is_old(
    tmp_path, monkeypatch
):
    config, events, fail = serial_fixture(tmp_path, monkeypatch, blocked=True)
    first = CoverageAuditor(config)
    run_id = first.run_id
    assert asyncio.run(first.run())["status"] == "CORPUS_PAUSED"
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "UPDATE corpus_matches SET checked_at='2000-01-01T00:00:00+00:00'"
        )
    config.resume = True
    fail[0] = False
    events.clear()
    assert asyncio.run(CoverageAuditor(config, run_id=run_id).run())["status"] == "LIMITED"
    assert events[0] == ("text", config.target_url)
    assert ("presence", config.target_url) not in events
    assert ("website", config.target_url + "child") in events


def test_explicit_saved_inventory_refresh_still_rechecks_expired_results(tmp_path, monkeypatch):
    _, auditor = inventory(tmp_path, count=1)
    url = "https://example.test/0"
    provider = "coverage_api:" + auditor.config.corpus_url.rstrip("/")
    auditor.db.add_corpus_match(
        auditor.run_id, url, "NOT_FOUND", None, None, None, None,
        "TEXT_NOT_AVAILABLE", provider=provider,
    )
    auditor.db.connection.execute(
        "UPDATE corpus_matches SET checked_at='2000-01-01T00:00:00+00:00'"
    )
    auditor.db.connection.commit()
    calls = []

    def handler(request):
        calls.extend(json.loads(request.content)["urls"])
        return httpx.Response(200, json={"results": [response(url, False)]})

    mock_api(monkeypatch, handler)
    try:
        assert asyncio.run(auditor._verify_all()) == "COMPLETED"
        assert calls == [url]
        assert auditor.db.connection.execute(
            "SELECT checked_at FROM corpus_matches"
        ).fetchone()[0] > "2000-01-01"
    finally:
        auditor.db.close()
