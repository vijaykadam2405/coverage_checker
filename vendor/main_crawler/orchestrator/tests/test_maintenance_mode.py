"""Maintenance mode: can an operator make the fleet hold still without destroying anything?

There were two switches and neither said this. The master switch STOPS the pipeline and drops the
durable frontier. The operator halt means "stay stopped until I say otherwise". Neither expresses
"I am about to move 13,388 queued URLs between instances, hold still for ten minutes" — and without
that, every maintenance action loses a race it cannot even see:

    16:41  stop signalled to all 12
    16:41  prune -> 409 Conflict on 11 of 12   (auto-refill had already resumed them)

Both the frontier prune and the frontier move REFUSE to touch a crawling instance, correctly — they
would be racing a live engine. So the refusals were right and the fleet was unfixable at the same
time. The workaround was `docker stop mallory-crawler-orchestrator-1`, which is not something an
operator should have to know.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mallory_orchestrator import autopilot, scheduler


class _Row:
    def __init__(self, key, value):
        self.key, self.value = key, value


class _DB:
    """Enough Setting-store to exercise the flag, with no database."""

    def __init__(self):
        self.rows: dict[str, _Row] = {}

    def get(self, _model, key):
        return self.rows.get(key)

    def add(self, row):
        self.rows[row.key] = row

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr(scheduler, "log_event", lambda *a, **k: None)
    return _DB()


def test_it_is_off_until_someone_turns_it_on(db):
    """A flag that defaults on would silently freeze the fleet after any fresh deploy."""
    assert scheduler.maintenance(db) is False


def test_it_survives_being_read_back(db):
    scheduler.set_maintenance(db, True)
    assert scheduler.maintenance(db) is True
    scheduler.set_maintenance(db, False)
    assert scheduler.maintenance(db) is False


def test_it_is_a_different_flag_from_the_operator_halt(db):
    """They mean different things and must be independently settable: a halt outlives maintenance,
    and coming out of maintenance must not cancel a halt the operator set on purpose."""
    scheduler.set_halted(db, True)
    scheduler.set_maintenance(db, True)
    scheduler.set_maintenance(db, False)
    assert scheduler.halted(db) is True, "leaving maintenance cleared the operator's halt"


def test_maintenance_beats_refill_even_when_forced():
    """`force` is the operator saying "put the fleet back to work"; maintenance is the same operator
    saying "hold still". The later instruction wins, or the button fights the window."""
    src = Path(__import__("mallory_orchestrator.refill", fromlist=["x"]).__file__).read_text(
        encoding="utf-8")
    body = src.split("def refill_once")[1]
    assert "scheduler.maintenance(db)" in body, "refill ignores maintenance mode"
    # …and it must be checked BEFORE any instance is looked at, not per-instance after the fan-out.
    assert body.index("scheduler.maintenance(db)") < body.index("crawler_client.pool()")


def test_the_autopilot_stands_down_in_maintenance(monkeypatch):
    """It resumes idle instances and prunes frontiers — both of which are exactly what an operator
    moving frontiers needs NOT to happen."""
    import sys
    from types import SimpleNamespace

    calls: list[str] = []
    sched = SimpleNamespace(halted=lambda db: False, maintenance=lambda db: True,
                            auto_on=lambda db, name: True)
    monkeypatch.setitem(sys.modules, "mallory_orchestrator.scheduler", sched)
    import mallory_orchestrator
    monkeypatch.setattr(mallory_orchestrator, "scheduler", sched, raising=False)
    api = SimpleNamespace(crawler_fleet_assign=lambda db=None: calls.append("assign"),
                          crawler_fleet_prune=lambda apply=False: calls.append("traps"),
                          crawler_fleet_health=lambda: calls.append("health"),
                          data_quality=lambda: calls.append("quality"))
    monkeypatch.setitem(sys.modules, "mallory_orchestrator.api", api)
    monkeypatch.setattr(mallory_orchestrator, "api", api, raising=False)
    db_mod = SimpleNamespace(SessionLocal=lambda: _DB())
    monkeypatch.setitem(sys.modules, "mallory_orchestrator.db", db_mod)
    monkeypatch.setattr(mallory_orchestrator, "db", db_mod, raising=False)

    autopilot._history.clear()
    out = autopilot.run_once()
    assert calls == [], "the autopilot acted during maintenance"
    assert "maintenance" in out["skipped"]


def test_it_never_drops_the_frontier():
    """The distinction that justifies a third flag at all. The master switch drops the durable
    frontier; maintenance exists precisely to preserve it while it is being moved."""
    from mallory_orchestrator import api
    src = Path(api.__file__).read_text(encoding="utf-8")
    body = src.split("def crawler_fleet_maintenance")[1].split("\n@router")[0]
    for forbidden in ("clear_resumable", "_wipe", "flush", "set_master", "frontier_clear"):
        assert forbidden not in body, f"maintenance calls {forbidden!r} — it must only PAUSE"


def test_turning_it_off_puts_the_fleet_back_to_work():
    """A maintenance mode you have to remember to undo by hand is one that gets left on."""
    from mallory_orchestrator import api
    src = Path(api.__file__).read_text(encoding="utf-8")
    body = src.split("def crawler_fleet_maintenance")[1].split("\n@router")[0]
    assert "refill.refill_once(force=True)" in body


def test_it_reports_instances_that_would_not_stop():
    """A wedged instance is a fact the operator needs — it is the difference between "wait" and
    "restart that container". Silently returning success would send them straight into another 409."""
    from mallory_orchestrator import api
    src = Path(api.__file__).read_text(encoding="utf-8")
    body = src.split("def crawler_fleet_maintenance")[1].split("\n@router")[0]
    assert "still_running" in body
    assert "durable" in body, "nothing tells the operator a restart is safe"


def test_the_endpoint_is_registered():
    from mallory_orchestrator import api
    assert "/api/crawler/fleet/maintenance" in {r.path for r in api.router.routes}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
