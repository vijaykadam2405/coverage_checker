"""The crawl-STARTING routes refuse anything the orchestrator did not arm.

The orchestrator's control plane decides what may crawl; a POST straight to an instance's
/v1/crawl/batch walks past it. So /v1/crawl, /v1/crawl/batch and /v1/crawl/resume require the
shared secret (X-Mallory-Arm == CRAWLER_ARM_TOKEN) once the instance has one, and:

  (a) no token on the instance  -> open, byte-for-byte as before (the deploy cannot brick the fleet)
  (b) token + the right header  -> accepted
  (c) token + missing/wrong     -> 403, and NOTHING started: no engine, no claim, no stop-file clear
  (d) everything that is not a crawl start stays open — including Stop

Every crawl is stubbed at `_dispatch`, so nothing here opens a browser or the network.
"""
import pytest
from fastapi.testclient import TestClient

import crawler_api.app as app_module
from crawler_api.app import app

JOB = {"job_id": "arm_test", "job_type": "profile", "seed_urls": ["https://example.com/"],
       "keywords": [], "max_pages": 1, "max_depth": 0}
RESULT = [{"job_id": "arm_test", "summary": {"fetched": 0, "kept": 0, "sent": 0, "accepted": 0}}]
OK = {"X-Mallory-Arm": "s3cret"}


@pytest.fixture()
def crawls(monkeypatch):
    """Records every crawl the app would have started and every stop-file clear, and does neither."""
    started: list[list[str]] = []
    armed: list[list[str]] = []
    monkeypatch.setattr(app_module, "_dispatch",
                        lambda jobs, *, forward: started.append([j.job_id for j in jobs]) or RESULT)
    monkeypatch.setattr(app_module, "_arm", lambda ids=None: armed.append(list(ids or [])))
    return {"started": started, "armed": armed}


@pytest.fixture()
def client():
    return TestClient(app)


def _starts(client, headers=None):
    """One request to each crawl-starting route, in order."""
    return [client.post("/v1/crawl/batch", json={"jobs": [JOB]}, headers=headers),
            client.post("/v1/crawl", json=JOB, headers=headers),
            client.post("/v1/crawl/resume", json={"job_ids": ["arm_test"]}, headers=headers)]


# ── (a) fail-safe ─────────────────────────────────────────────────────────────

def test_no_token_means_open_exactly_as_before(client, crawls, monkeypatch):
    monkeypatch.delenv("CRAWLER_ARM_TOKEN", raising=False)
    assert client.get("/health").json()["arm_lock"] is False
    batch, single, resume = _starts(client)
    assert batch.status_code == 200 and batch.json()["jobs"] == 1, batch.text
    assert single.status_code == 200 and single.json()["job_id"] == "arm_test", single.text
    # No saved frontier for this id, so an OPEN resume fails on its own terms — not with a 403.
    assert resume.status_code == 400 and "no resumable job" in resume.json()["detail"], resume.text
    assert crawls["started"] == [["arm_test"], ["arm_test"]]
    # An empty or blank token is "unset", not a token that an empty header could match.
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "   ")
    assert client.get("/health").json()["arm_lock"] is False
    assert client.post("/v1/crawl/batch", json={"jobs": [JOB]}).status_code == 200


# ── (b) armed ─────────────────────────────────────────────────────────────────

def test_the_right_token_is_accepted(client, crawls, monkeypatch):
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    assert client.get("/health").json()["arm_lock"] is True
    batch, single, resume = _starts(client, headers=OK)
    assert batch.status_code == 200 and batch.json()["jobs"] == 1, batch.text
    assert single.status_code == 200 and single.json()["job_id"] == "arm_test", single.text
    assert resume.status_code == 400, "an armed resume reached the route body"
    assert crawls["started"] == [["arm_test"], ["arm_test"]]
    assert crawls["armed"] == [["arm_test"], ["arm_test"]], "an armed start must clear the stop file"
    # The header name is case-insensitive, as HTTP headers are; the VALUE is not.
    assert client.post("/v1/crawl/batch", json={"jobs": [JOB]},
                       headers={"x-mallory-arm": "s3cret"}).status_code == 200


# ── (c) refused, and nothing started ──────────────────────────────────────────

@pytest.mark.parametrize("headers", [{}, {"X-Mallory-Arm": ""}, {"X-Mallory-Arm": "wrong"},
                                     {"X-Mallory-Arm": "s3cre"}, {"X-Mallory-Arm": "S3CRET"},
                                     {"Authorization": "Bearer s3cret"}])
def test_missing_or_wrong_token_is_refused_and_starts_nothing(client, crawls, monkeypatch, headers):
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    for r in _starts(client, headers=headers):
        assert r.status_code == 403, (r.request.url, r.text)
        assert "X-Mallory-Arm" in r.json()["detail"] and "s3cret" not in r.text
    assert crawls["started"] == [], "a refused request started a crawl"
    assert crawls["armed"] == [], "a refused request cleared the stop file"
    st = client.get("/v1/batch/status").json()
    assert st["running"] is False and st["claim"]["held"] is False, "a refused request took the claim"


def test_a_refusal_does_not_wedge_the_instance(client, crawls, monkeypatch):
    """The lock sits BEFORE the claim, so a refused request leaves nothing to release — the very
    next armed request goes straight through."""
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    assert client.post("/v1/crawl/batch", json={"jobs": [JOB]}).status_code == 403
    assert client.post("/v1/crawl/batch", json={"jobs": [JOB]}, headers=OK).status_code == 200
    assert crawls["started"] == [["arm_test"]]


# ── (d) everything else stays open ────────────────────────────────────────────

def test_everything_that_is_not_a_crawl_start_stays_open(client, crawls, monkeypatch):
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    for path in ("/health", "/v1/batch/status", "/v1/schema", "/v1/crawl/drop"):
        assert client.get(path).status_code == 200, path
    # Stop and drop take work AWAY. A Stop that needed a secret would be a Stop that could fail.
    try:
        r = client.post("/v1/crawl/stop")
        assert r.status_code == 200 and "stopping" in r.json(), r.text
        r = client.post("/v1/crawl/drop", json={"job_ids": []})
        assert r.status_code == 200 and r.json()["count"] == 0, r.text
    finally:
        app_module.clear_stop()      # the stop is a FILE that outlives this test; `_arm` is stubbed
    assert crawls["started"] == []
