import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from crawler_coverage_auditor.services.revision import BackendRevision
from crawler_coverage_auditor.web.api import create_app


def test_forced_revision_check_does_not_wait_for_polling_cache(tmp_path):
    package = tmp_path / "crawler_coverage_auditor"
    package.mkdir()
    source = package / "module.py"
    source.write_text("value=1")
    revision = BackendRevision(tmp_path, clock=lambda: 0)
    assert not revision.update_available()
    source.write_text("value=222")
    assert not revision.update_available()
    assert revision.update_available(force=True)


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/discover", {"target_url": "https://example.test/"}),
        ("/api/runs/1/resume", {}),
        ("/api/runs/1/verify", {}),
    ],
)
def test_stale_backend_rejects_new_work_but_allows_reading_and_pause(
    tmp_path, monkeypatch, path, payload
):
    monkeypatch.setattr(
        "crawler_coverage_auditor.web.api.BackendRevision",
        lambda root: SimpleNamespace(
            loaded="old", update_available=lambda **kwargs: True
        ),
    )
    stop = threading.Event()
    job = SimpleNamespace(
        run_id=1, action="discovery", target="https://example.test/", stop_event=stop
    )
    jobs = SimpleNamespace(active=lambda: job)
    client = TestClient(
        create_app(tmp_path / "audit.sqlite", tmp_path / "reports", jobs=jobs)
    )
    assert client.get("/api/state").status_code == 200
    response = client.post(path, json=payload, headers={"X-CrawlScope-Client": "1"})
    assert response.status_code == 409 and "restart" in response.json()["detail"]
    assert (
        client.post(
            "/api/runs/1/stop", json={}, headers={"X-CrawlScope-Client": "1"}
        ).status_code
        == 202
    )
    assert stop.is_set()
