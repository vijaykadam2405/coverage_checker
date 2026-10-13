"""One site per instance was costing ~5x on re-crawls.

An engine has 32 tabs and a 256-worker httpx pool, but per-HOST concurrency is politeness-capped.
So one site per instance = 12 throttled hosts fleet-wide with the tabs idle, and each instance then
spends most of its life in that single site's low-parallelism tail before idling until the next
tick. Measured live: active=3 against 32 tabs, 0.062 pages/s.

These tests pin the two things that must both hold: more than one job can go to an instance, and
the assign-once shard map is still what decides WHICH instance.
"""
from __future__ import annotations

import pytest

from mallory_orchestrator import scheduler


class _SJ:
    def __init__(self, i, dom):
        self.id, self.job = i, {"seed_urls": ["https://%s/" % dom]}


class _DB:
    def get(self, _model, i):
        return _SJ(i, "d%s.com" % i)


@pytest.fixture
def harness(monkeypatch):
    sent: dict[int, list[int]] = {}

    def _serial(_s, sjs, _now, _send, _n):
        return len(sjs)

    monkeypatch.setattr(scheduler, "_dispatch_serial", _serial)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(scheduler.crawler_client, "pool", lambda: ["b"] * 4)
    monkeypatch.setattr(scheduler, "free_instances", lambda **k: [0, 1, 2, 3])
    scheduler._INFLIGHT.clear()

    class _Pool:
        def submit(self, fn, inst, sids):
            sent[inst] = list(sids)
            fn(inst, sids)
    monkeypatch.setattr(scheduler, "_POOL", _Pool())
    return sent


def _shard(monkeypatch, mapping):
    import importlib
    shard = importlib.import_module("mallory_orchestrator.shard")
    monkeypatch.setattr(shard, "instance_for",
                        lambda db, seed, n: mapping.get(seed.split("//")[1].rstrip("/"), 0),
                        raising=False)


def test_one_instance_can_take_several_sites(harness, monkeypatch):
    """THE fix. All four sites shard to instance 0; at JOBS_PER_INSTANCE=1 only one ever went."""
    monkeypatch.setattr(scheduler, "JOBS_PER_INSTANCE", 4)
    _shard(monkeypatch, {"a.com": 0, "b.com": 0, "c.com": 0, "d.com": 0})
    due = [_SJ(1, "a.com"), _SJ(2, "b.com"), _SJ(3, "c.com"), _SJ(4, "d.com")]
    n = scheduler._dispatch_fleet(_DB(), due, "NOW", None)
    assert n == 4, "all four due sites should go out in one tick"
    assert harness[0] == [1, 2, 3, 4]


def test_the_cap_is_respected(harness, monkeypatch):
    """It is a throughput knob, not 'send everything' — the engine still has finite tabs."""
    monkeypatch.setattr(scheduler, "JOBS_PER_INSTANCE", 2)
    _shard(monkeypatch, {"a.com": 0, "b.com": 0, "c.com": 0})
    due = [_SJ(1, "a.com"), _SJ(2, "b.com"), _SJ(3, "c.com")]
    assert scheduler._dispatch_fleet(_DB(), due, "NOW", None) == 2
    assert harness[0] == [1, 2]


def test_default_of_one_preserves_the_old_behaviour(harness, monkeypatch):
    """Unset env must change nothing — the data centre opts in explicitly."""
    monkeypatch.setattr(scheduler, "JOBS_PER_INSTANCE", 1)
    _shard(monkeypatch, {"a.com": 0, "b.com": 0})
    assert scheduler._dispatch_fleet(_DB(), [_SJ(1, "a.com"), _SJ(2, "b.com")], "NOW", None) == 1


def test_sites_still_go_to_their_OWNING_instance(harness, monkeypatch):
    """A site's durable frontier lives in ONE instance's data dir. Send it elsewhere and the crawl
    silently restarts from the seed while the real frontier sits unreachable next door."""
    monkeypatch.setattr(scheduler, "JOBS_PER_INSTANCE", 5)
    _shard(monkeypatch, {"a.com": 2, "b.com": 2, "c.com": 3})
    due = [_SJ(1, "a.com"), _SJ(2, "b.com"), _SJ(3, "c.com")]
    scheduler._dispatch_fleet(_DB(), due, "NOW", None)
    assert harness[2] == [1, 2] and harness[3] == [3]
    assert 0 not in harness, "nothing may be sent to an instance that owns none of the work"


def test_a_job_already_in_flight_is_not_sent_twice(harness, monkeypatch):
    """due_jobs selects on next_run, which only advances on SUCCESS — so without _INFLIGHT the next
    tick re-picks a still-running job and hands it to a second instance."""
    monkeypatch.setattr(scheduler, "JOBS_PER_INSTANCE", 5)
    _shard(monkeypatch, {"a.com": 0, "b.com": 0})
    scheduler._INFLIGHT.add(1)
    try:
        assert scheduler._dispatch_fleet(_DB(), [_SJ(1, "a.com"), _SJ(2, "b.com")], "NOW", None) == 1
        assert harness[0] == [2]
    finally:
        scheduler._INFLIGHT.clear()
