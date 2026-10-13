"""Every crawl-starting POST the orchestrator sends carries the arming secret — and only those.

The crawler refuses un-armed starts once it has a token (crawler/tests/test_arm_lockout.py). This
is the other half: crawler_client is the ONE sender, its three start functions put the header on
the wire, the secret goes nowhere it is not needed, and no other module talks to a crawl route —
so the scheduler, the daily lane, refill, the autopilot's assign, a pasted batch and the campaign
launcher are all armed by construction.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from mallory_orchestrator import crawler_client  # noqa: E402

BASE = "http://c1:8099"


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True}


class _Client:
    """Stands in for httpx.Client: records each request's method, url and headers; sends nothing."""
    sent: list[tuple[str, str, dict]] = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, **kw):
        _Client.sent.append(("POST", url, dict(headers or {})))
        return _Resp()

    def get(self, url, params=None, headers=None, **kw):
        _Client.sent.append(("GET", url, dict(headers or {})))
        return _Resp()


@pytest.fixture()
def sent(monkeypatch):
    monkeypatch.setattr(crawler_client.httpx, "Client", _Client)
    _Client.sent.clear()
    return _Client.sent


def _starts():
    crawler_client.dispatch({"job_id": "j"}, base=BASE)
    crawler_client.dispatch_batch({"jobs": []}, base=BASE)
    crawler_client.resume(["j"], base=BASE)


def test_every_crawl_start_is_armed_when_the_token_is_set(sent, monkeypatch):
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    _starts()
    assert [u for _, u, _ in sent] == [BASE + "/v1/crawl", BASE + "/v1/crawl/batch",
                                      BASE + "/v1/crawl/resume"]
    assert all(h.get("X-Mallory-Arm") == "s3cret" for _, _, h in sent), sent


def test_no_token_means_no_header(sent, monkeypatch):
    """Against an instance with no token the header is noise; against one with a token, a missing
    header is a 403 — which is the ORDER the rollout must follow (orchestrator first)."""
    monkeypatch.delenv("CRAWLER_ARM_TOKEN", raising=False)
    _starts()
    assert len(sent) == 3 and all("X-Mallory-Arm" not in h for _, _, h in sent), sent
    sent.clear()
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "   ")
    _starts()
    assert all("X-Mallory-Arm" not in h for _, _, h in sent), sent


def test_the_token_is_read_live_not_at_import(sent, monkeypatch):
    """A Settings snapshot would freeze the value at import — the trap config.py documents."""
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "first")
    crawler_client.dispatch_batch({"jobs": []}, base=BASE)
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "second")
    crawler_client.dispatch_batch({"jobs": []}, base=BASE)
    assert [h["X-Mallory-Arm"] for _, _, h in sent] == ["first", "second"]


def test_the_secret_goes_only_where_a_crawl_starts(sent, monkeypatch):
    monkeypatch.setenv("CRAWLER_ARM_TOKEN", "s3cret")
    crawler_client.stop_crawl(base=BASE)
    crawler_client.batch_status(base=BASE)
    crawler_client.drop_jobs([], base=BASE)
    crawler_client.get_metrics(base=BASE)
    assert len(sent) == 4 and all("X-Mallory-Arm" not in h for _, _, h in sent), sent


def test_no_module_but_crawler_client_talks_to_a_crawl_route():
    """The arming lives in three functions. A dispatcher that built its own POST to /v1/crawl*
    would be un-armed and refused — and would look like 'the crawler is down'. Pinned on the
    source, the way the crawler pins its own claim wiring."""
    pkg = Path(crawler_client.__file__).parent
    offenders = []
    for p in pkg.rglob("*.py"):
        if p.name == "crawler_client.py":
            continue
        if re.search(r"""["']/v1/crawl(?!/stop|/drop)""", p.read_text(encoding="utf-8")):
            offenders.append(str(p.relative_to(pkg)))
    assert offenders == [], offenders
