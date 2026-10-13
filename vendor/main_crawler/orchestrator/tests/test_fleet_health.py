"""Does the fleet health check name the RIGHT fault?

Three failures look identical in every aggregate number on the dashboard — total pages/s falls and
nothing else moves — but each has a different remedy:

  * idle instances holding nothing   -> rebalance (move_frontier), work cannot move on its own
  * idle instances holding a queue   -> resume that instance (a stale stop flag)
  * one instance hogging the backlog -> rebalance

Calling the second one "rebalance" wastes a stop/resume cycle on a problem a single POST fixes, so
the verdicts have to be distinguishable.
"""
from __future__ import annotations

import pytest

from mallory_orchestrator import api


def _fleet(specs):
    """specs: list of (crawling, frontier, render_q). Index = instance number."""
    return [{"instance": i, "ok": True, "data": {
        "batch": {"running": c, "total": 3 if c else 0},
        "lanes": {"frontier": f, "render_q": q},
    }} for i, (c, f, q) in enumerate(specs)]


@pytest.fixture()
def wired(monkeypatch):
    def _go(rows):
        class C:
            def fanout(self, fn, *a, **kw): return rows
            def get_metrics(self, *a, **kw): return {}
        monkeypatch.setattr(api, "crawler_client", C())
    return _go


def test_a_healthy_spread_is_ok(wired):
    wired(_fleet([(True, 1000, 100)] * 6))
    out = api.crawler_fleet_health()
    assert out["verdict"] == "ok", out
    assert out["instances"]["idle"] == 0
    assert out["remedy"] is None


def test_idle_instances_with_work_elsewhere_calls_for_a_rebalance(wired):
    """The measured case: six idle at zero while instance 0 holds the backlog."""
    wired(_fleet([(True, 111131, 0)] + [(True, 500, 0)] * 5 + [(False, 0, 0)] * 6))
    out = api.crawler_fleet_health()
    assert out["verdict"] == "idle_instances", out
    assert out["instances"]["idle"] == 6
    assert out["idle_empty"] == [6, 7, 8, 9, 10, 11]
    assert "move_frontier" in out["remedy"]


def test_an_idle_instance_that_HOLDS_work_is_a_different_fault(wired):
    """A stale stop flag. Rebalancing would not fix it and costs a whole stop/resume cycle."""
    wired(_fleet([(True, 1000, 0), (False, 5188, 0)]))
    out = api.crawler_fleet_health()
    assert out["verdict"] == "idle_with_work", out
    assert out["idle_with_work"] == [1]
    assert "resume them directly" in out["detail"]
    assert out["remedy"] is None, "this one is not fixed by a rebalance"


def test_one_instance_hogging_the_backlog_is_flagged_even_when_none_are_idle(wired):
    """Everyone is busy, so no idle alarm fires — but the tail is still stuck behind one instance."""
    wired(_fleet([(True, 90000, 0)] + [(True, 1000, 0)] * 5))
    out = api.crawler_fleet_health()
    assert out["verdict"] == "unbalanced", out
    assert out["busiest"]["instance"] == 0
    assert "move_frontier" in out["remedy"]


def test_a_stopped_fleet_is_not_reported_as_a_distribution_fault(wired):
    """Nobody crawling is a deliberate state most of the time; calling it 'unbalanced' would train
    the operator to ignore the check."""
    wired(_fleet([(False, 5000, 0)] * 4))
    out = api.crawler_fleet_health()
    assert out["verdict"] == "not_crawling", out


def test_no_instances_answering_is_its_own_verdict(wired):
    wired([{"instance": 0, "ok": False, "error": "no answer"}])
    out = api.crawler_fleet_health()
    assert out["reachable"] is False
    assert out["verdict"] == "no_instances"


def test_render_q_counts_as_queued_work(wired):
    """A frontier of 0 with 30,000 renders queued is NOT an idle instance — counting only `frontier`
    would call a render-bound instance empty and hand its work away."""
    wired(_fleet([(True, 0, 29704), (False, 0, 0)]))
    out = api.crawler_fleet_health()
    assert out["pending_total"] == 29704
    assert out["per_instance"][0]["pending"] == 29704
    assert out["idle_empty"] == [1]


def test_the_alarm_is_wired_into_the_dashboard():
    """The check is worthless if nothing surfaces it — the recurring shape in this codebase."""
    from pathlib import Path
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "crawlers idle" in html, "no alarm names idle crawlers"
    assert "m.instances.idle" in html, "the alarm never reads the idle count"
