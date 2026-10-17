from fastapi.testclient import TestClient

from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.web.api import create_app


def inventory(tmp_path):
    path = tmp_path / "test.sqlite"
    db = AuditDatabase(path)
    run = db.create_run("https://example.test/", "", {})
    db.add_url(
        run, DiscoveredURL("https://example.test/", "https://example.test/", "SEED", 0)
    )
    return path, db, run


def test_read_cache_reuses_unchanged_summary_and_invalidates_wal(tmp_path, monkeypatch):
    path, db, run = inventory(tmp_path)
    original = queries._connect_readonly
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(queries, "_connect_readonly", counted)
    try:
        before = queries.summary(path, run)
        before["metrics"]["discovered"] = -1
        again = queries.summary(path, run)
        assert again["metrics"]["discovered"] == 1 and len(calls) == 1
        db.add_url(
            run,
            DiscoveredURL(
                "https://example.test/child", "https://example.test/child", "STATIC", 1
            ),
        )
        assert queries.summary(path, run)["metrics"]["discovered"] == 2
        assert len(calls) == 2
    finally:
        db.close()


def test_diagnostics_opt_out_is_explicit_and_default_contract_is_preserved(
    tmp_path, monkeypatch
):
    path, db, run = inventory(tmp_path)
    db.close()
    calls = []
    original = queries.extraction_health

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(queries, "extraction_health", counted)
    client = TestClient(create_app(path, tmp_path / "reports"))
    fast = client.get(f"/api/runs/{run}/summary?diagnostics=false")
    assert fast.status_code == 200 and fast.json()["health"] is None and not calls
    compatible = client.get(f"/api/runs/{run}/summary")
    assert (
        compatible.status_code == 200 and compatible.json()["health"]["valid_html"] == 0
    )
    assert len(calls) == 1
