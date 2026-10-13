"""The fleet resume must not count an instance that refused the work.

`crawler_resume` did `resumed.extend(d["resumed"])` for every instance, and the crawler listed the
job ids in `resumed` even when it had refused to start them. So a fleet resume reported
"671 resumed, 0 errors" while one instance never restarted and then sat idle holding real pending
URLs. Four cycles in a row.
"""
from __future__ import annotations

import pytest

from mallory_orchestrator import api


class _Client:
    """Stands in for crawler_client. `refuse` = instance indexes that will not take the work."""

    def __init__(self, owners: dict[str, int], refuse=(), raise_on=(), refuse_old=()):
        self.owners = owners
        self.refuse = set(refuse)
        self.refuse_old = set(refuse_old)
        self.raise_on = set(raise_on)
        self.sent: dict[int, list[str]] = {}

    def pool(self):
        return [f"http://i{n}:8099" for n in range(12)]

    def resumable(self, base=None):
        return {}

    def fanout(self, fn, *a, **kw):
        rows = []
        for inst in sorted(set(self.owners.values())):
            jobs = [{"job_id": j} for j, i in self.owners.items() if i == inst]
            rows.append({"instance": inst, "ok": True, "data": {"jobs": jobs}})
        return rows

    def resume(self, job_ids, background=True, forward_to_ingest=True, base=None):
        inst = int(base.split("//i")[1].split(":")[0])
        self.sent[inst] = list(job_ids)
        if inst in self.raise_on:
            raise RuntimeError("409 Conflict: resume refused")
        if inst in self.refuse:
            # The crawler still lists the ids — that is exactly what made this invisible.
            return {"resumed": list(job_ids), "started": False,
                    "error": "a batch is already running"}
        if inst in self.refuse_old:
            # An UNPATCHED crawler: 200, ids listed, refusal only visible inside `batch`.
            return {"resumed": list(job_ids),
                    "batch": {"started": False, "error": "a batch is already running"}}
        return {"resumed": list(job_ids), "started": True}


class _Sched:
    def set_halted(self, db, v):
        self.halted = v

    def lane_open(self, db, lane):
        # The dispatch policy is read first (test_dispatch_policy.py); open here, because these
        # cases are about the accounting of what came back.
        return True

    def why_closed(self, db, lane):
        return None

    def owned_by_mode(self, db, ids):
        # Mode scoping (test_control_plane.py): every id is the current mode's here.
        return list(ids)


@pytest.fixture()
def wired(monkeypatch):
    def _go(client):
        monkeypatch.setattr(api, "crawler_client", client)
        monkeypatch.setattr(api, "scheduler", _Sched())
        return client
    return _go


def test_an_instance_that_refuses_is_reported_not_counted(wired):
    c = wired(_Client({"a": 0, "b": 0, "c": 5}, refuse={5}))
    out = api.crawler_resume({"job_ids": ["a", "b", "c"]}, db=None)
    assert sorted(out["resumed"]) == ["a", "b"], out["resumed"]
    assert out["refused"] == [5], out
    assert out["errors"] and out["errors"][0]["instance"] == 5
    assert 5 not in out["instances"], "a refusing instance must not be listed as having taken work"


def test_a_raising_instance_is_reported_too(wired):
    """httpx raise_for_status turns the crawler's 409 into an exception, so both shapes must land in
    `errors` — one from a mixed-version 200 body, one from the status code."""
    c = wired(_Client({"a": 0, "c": 5}, raise_on={5}))
    out = api.crawler_resume({"job_ids": ["a", "c"]}, db=None)
    assert out["resumed"] == ["a"]
    assert out["refused"] == [5]
    assert "refused" in out["errors"][0]["error"] or "409" in out["errors"][0]["error"]


def test_the_happy_path_is_unchanged(wired):
    c = wired(_Client({"a": 0, "b": 1, "c": 2}))
    out = api.crawler_resume({"job_ids": ["a", "b", "c"]}, db=None)
    assert sorted(out["resumed"]) == ["a", "b", "c"]
    assert out["refused"] == []
    assert out["errors"] == []
    assert out["instances"] == [0, 1, 2]


def test_every_instance_refusing_is_a_hard_failure(wired):
    """Silently returning "0 resumed" would read as "nothing to do" on a fleet that is fully stuck."""
    c = wired(_Client({"a": 0, "c": 5}, refuse={0, 5}))
    with pytest.raises(Exception) as e:
        api.crawler_resume({"job_ids": ["a", "c"]}, db=None)
    assert "every instance" in str(getattr(e.value, "detail", e.value))


def test_a_job_no_instance_owns_is_still_reported_separately(wired):
    """Unowned is a different problem from refused — it means no instance holds that frontier."""
    c = wired(_Client({"a": 0}))
    out = api.crawler_resume({"job_ids": ["a", "ghost"]}, db=None)
    assert out["resumed"] == ["a"]
    assert out["missing_config"] == ["ghost"]
    assert out["refused"] == []


def test_each_job_goes_only_to_its_owner(wired):
    c = wired(_Client({"a": 0, "b": 7, "c": 7}))
    api.crawler_resume({"job_ids": ["a", "b", "c"]}, db=None)
    assert c.sent[0] == ["a"]
    assert sorted(c.sent[7]) == ["b", "c"]


def test_an_UNPATCHED_crawler_refusal_is_also_caught(wired):
    """Mid-rollout: an instance still on the old image returns 200 with the ids listed and the
    refusal only inside `batch.started`. Reading the top level alone keeps missing it exactly where
    it still matters."""
    c = wired(_Client({"a": 0, "c": 5}, refuse_old={5}))
    out = api.crawler_resume({"job_ids": ["a", "c"]}, db=None)
    assert out["resumed"] == ["a"], out["resumed"]
    assert out["refused"] == [5], out
    assert "already running" in out["errors"][0]["error"]
