"""Real SQLite commits reach local API views without a restart or Corpus call."""

from fastapi.testclient import TestClient
from test_react_api import Jobs

from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.web.api import create_app


def test_database_writes_reach_summary_analysis_inventory_and_activity(tmp_path):
    path = tmp_path / "audit.sqlite"
    db = AuditDatabase(path)
    target = "https://example.test/"
    run_id = db.create_run(target, "https://api.example.test/", {})
    jobs = Jobs()
    client = TestClient(create_app(path, tmp_path / "reports", jobs=jobs))

    def assert_views(discovered, eligible, found, missing):
        snapshots = []
        for diagnostics in ("false", "true"):
            response = client.get(
                f"/api/runs/{run_id}/summary?diagnostics={diagnostics}"
            )
            assert response.status_code == 200
            value = response.json()
            snapshots.append(value)
            for key, expected in {
                "discovered": discovered, "eligible": eligible,
                "found": found, "missing": missing,
            }.items():
                assert value["metrics"][key] == expected
        assert snapshots[0]["health"] is None
        assert snapshots[1]["health"] is not None
        assert snapshots[0]["metrics"] == snapshots[1]["metrics"]
        analysis = client.get(f"/api/runs/{run_id}/analysis")
        assert analysis.status_code == 200
        summary = analysis.json()["summary"]
        assert summary["discovered_urls"] == discovered
        assert summary["expected_urls"] == eligible
        assert summary["found_urls"] == found
        assert summary["missing_urls"] == missing
        assert client.get(f"/api/runs/{run_id}/urls").json()["total"] == discovered
        assert client.get(f"/api/runs/{run_id}/live-urls").status_code == 200
        assert jobs.calls == []

    try:
        db.add_url(run_id, DiscoveredURL(target, target, "STATIC", 0))
        assert_views(1, 0, 0, 0)
        db.add_http_result(run_id, target, status_code=200, validity="VALID_PAGE")
        db.add_corpus_match(
            run_id, target, "EXACT_MATCH", target, None, 20, 1.0, "FOUND_FULL"
        )
        assert_views(1, 1, 1, 0)
        child = target + "child"
        db.add_url(run_id, DiscoveredURL(child, child, "STATIC", 1))
        db.add_http_result(run_id, child, status_code=200, validity="VALID_PAGE")
        db.add_corpus_match(
            run_id, child, "NOT_FOUND", None, None, None, None, "TEXT_NOT_AVAILABLE"
        )
        assert_views(2, 2, 1, 1)
        pdf = target + "report.pdf"
        db.add_url(run_id, DiscoveredURL(pdf, pdf, "STATIC", 1, page_type="DOCUMENT"))
        db.add_http_result(run_id, pdf, validity="URL_ONLY")
        db.add_corpus_match(
            run_id, pdf, "NOT_FOUND", None, None, None, None, "URL_ONLY"
        )
        assert_views(3, 3, 1, 2)
        # Revisiting an existing URL merges evidence without inflating totals.
        db.add_url(run_id, DiscoveredURL(child, child, "SITEMAP", 1))
        assert_views(3, 3, 1, 2)
        db.event(run_id, "Discovering", "DISCOVERED", "New URL saved", child, 1)
        latest = client.get(f"/api/runs/{run_id}/activity?limit=1").json()["items"][0]
        assert latest["url"] == child and latest["kind"] == "DISCOVERED"
        assert db.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
