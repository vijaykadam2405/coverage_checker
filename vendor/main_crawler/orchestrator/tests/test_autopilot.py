"""The health autopilot: does it stay inside the actions that are safe to repeat unattended?

Every action it takes already existed as a button, and buttons only fire when somebody looks. The
faults they fix are quiet by design — an instance idle on a stale stop flag, work stranded on one
container — so automating them is worth real care about what it must NEVER do.

The one that matters most is the halt. "Something restarted my crawl behind my back" has already
shipped twice in this codebase (the multiprocess stop-file, then the refill timer). An automation
that can undo an operator's stop is worse than no automation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mallory_orchestrator import autopilot


@pytest.fixture(autouse=True)
def _clean():
    autopilot._history.clear()
    autopilot._state.update({"last_at": None, "next_at": None, "running": False, "ticks": 0})
    yield
    autopilot._history.clear()


class _DB:
    def close(self):
        pass


@pytest.fixture()
def wired(monkeypatch):
    """Stub the three actions and the halt flag; record what actually got called."""
    calls: list[str] = []

    def _go(halted=False, assign=None, health=None, quality=None, traps=None, seeds=None):
        import sys
        from types import SimpleNamespace

        # auto_on: the health_dispatch toggle (test_control_plane.py) — ON here, because these
        # cases are about the halt and the action list, not about the toggle.
        sched = SimpleNamespace(halted=lambda db: halted, maintenance=lambda db: False,
                                auto_on=lambda db, name: True)
        monkeypatch.setitem(sys.modules, "mallory_orchestrator.scheduler", sched)
        import mallory_orchestrator
        monkeypatch.setattr(mallory_orchestrator, "scheduler", sched, raising=False)

        def _assign(db=None):
            calls.append("assign")
            if isinstance(assign, Exception):
                raise assign
            return assign or {"ok": True, "summary": "nothing to assign", "resumed": 0, "moved": 0}

        def _health():
            calls.append("health")
            if isinstance(health, Exception):
                raise health
            return health or {"verdict": "ok", "detail": "spread"}

        def _quality():
            calls.append("quality")
            if isinstance(quality, Exception):
                raise quality
            return quality or {"reachable": True, "duplicate_content": 0, "empty": {"no_text": 0}}

        def _traps(apply=False):
            calls.append("traps")
            if isinstance(traps, Exception):
                raise traps
            return traps or {"ok": True, "junk": 0, "summary": "would remove 0 queued URL(s)"}

        def _seeds(limit=25, apply=False, apply_moves=False, db=None):
            calls.append("seeds")
            if isinstance(seeds, Exception):
                raise seeds
            return seeds or {"ok": True, "corrected": [], "applied": [], "proposed": [],
                             "checked": 0,
                             "summary": "re-pointed 0 of 0 re-probed seed(s)"}

        api = SimpleNamespace(crawler_fleet_assign=_assign, crawler_fleet_health=_health,
                              data_quality=_quality, crawler_fleet_prune=_traps,
                              seeds_recheck=_seeds)
        monkeypatch.setitem(sys.modules, "mallory_orchestrator.api", api)
        monkeypatch.setattr(mallory_orchestrator, "api", api, raising=False)

        db_mod = SimpleNamespace(SessionLocal=lambda: _DB())
        monkeypatch.setitem(sys.modules, "mallory_orchestrator.db", db_mod)
        monkeypatch.setattr(mallory_orchestrator, "db", db_mod, raising=False)
        return calls

    return _go


def test_a_halted_crawl_is_left_completely_alone(wired):
    """THE one that matters. Undoing an operator's stop has shipped twice here already."""
    calls = wired(halted=True)
    out = autopilot.run_once()
    assert calls == [], "the autopilot acted while the operator had the crawl halted"
    assert "halted" in out["skipped"]


def test_a_normal_pass_runs_every_action_in_order(wired):
    calls = wired()
    autopilot.run_once()
    assert calls == ["assign", "traps", "health", "seeds", "quality"], calls


def test_the_changing_action_runs_BEFORE_the_slow_read_only_ones(wired):
    """quality scans the whole corpus. If a pass is interrupted, the action that fixes something
    should already have happened."""
    calls = wired()
    autopilot.run_once()
    assert calls.index("assign") < calls.index("quality")


def test_one_failing_action_does_not_stop_the_others(wired):
    calls = wired(assign=RuntimeError("crawler unreachable"))
    autopilot.run_once()
    assert calls == ["assign", "traps", "health", "seeds", "quality"]
    assert any(h["action"] == "assign" and not h["ok"] for h in autopilot._history)


def test_it_never_raises(wired):
    """It runs on a timer; an exception escaping would silently end the automation."""
    wired(assign=RuntimeError("x"), health=RuntimeError("y"), quality=RuntimeError("z"),
          traps=RuntimeError("w"), seeds=RuntimeError("v"))
    autopilot.run_once()          # must not raise
    assert autopilot._state["ticks"] == 1


def test_the_trap_sweep_actually_writes(wired):
    """A dry run every fifteen minutes for ever would report the fault and never fix it — which is
    the state this action exists to end. It must be called with apply."""
    seen = {}

    def _capture(apply=False):
        seen["apply"] = apply
        return {"ok": True, "junk": 12, "summary": "removed 12"}

    wired()
    import mallory_orchestrator
    mallory_orchestrator.api.crawler_fleet_prune = _capture
    autopilot.run_once()
    assert seen.get("apply") is True, "the sweep ran dry and changed nothing"
    row = [h for h in autopilot._history if h["action"] == "traps"][-1]
    assert row["changed"] is True


def test_a_sweep_that_found_nothing_is_not_marked_as_a_change(wired):
    """The normal case. If every quiet pass claims it acted, the history stops being readable."""
    wired(traps={"ok": True, "junk": 0, "summary": "removed 0 queued URL(s) of 8,412"})
    autopilot.run_once()
    assert [h for h in autopilot._history if h["action"] == "traps"][-1]["changed"] is False


def test_a_pass_that_changed_something_is_marked_as_such(wired):
    """A no-op pass is the NORMAL case and must look different from one that acted, or the history
    is unreadable."""
    wired(assign={"ok": True, "summary": "resumed 3", "resumed": 3, "moved": 0})
    autopilot.run_once()
    row = [h for h in autopilot._history if h["action"] == "assign"][0]
    assert row["changed"] is True
    wired(assign={"ok": True, "summary": "nothing to assign", "resumed": 0, "moved": 0})
    autopilot.run_once()
    assert [h for h in autopilot._history if h["action"] == "assign"][-1]["changed"] is False


def test_history_is_bounded(wired):
    wired()
    for _ in range(30):
        autopilot.run_once()
    assert len(autopilot._history) <= autopilot.HISTORY_MAX


def test_the_interval_cannot_be_set_absurdly_low(monkeypatch):
    """assign fans out to twelve instances and quality scans the corpus. A one-minute autopilot
    would be a load generator wearing a health-check's name."""
    monkeypatch.setenv("CRAWLER_AUTOPILOT_MINUTES", "1")
    assert autopilot.interval_s() >= 300
    monkeypatch.setenv("CRAWLER_AUTOPILOT_MINUTES", "30")
    assert autopilot.interval_s() == 1800


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CRAWLER_AUTOPILOT", "0")
    assert autopilot.enabled() is False


def test_it_never_stops_flushes_or_touches_the_corpus():
    """The blast radius, asserted against the source rather than trusted.

    It moved once, deliberately: the trap sweep DELETES queued frontier URLs. That is a different
    kind of act from the rest of this list — a queue entry costs nothing if it was real, because the
    page that linked to it offers it again, and the crawler's own filter refuses to enqueue these
    today anyway. Stored pages are still untouchable, which is what `data_dedupe` is doing on this
    list: it deletes corpus rows, so it stays a button a human presses.
    """
    src = Path(autopilot.__file__).read_text(encoding="utf-8")
    body = src.split("def run_once")[1]
    for forbidden in ("crawler_stop", "set_halted", "flush", "_wipe", "data_dedupe",
                      "clear_resumable", "rebalance("):
        assert forbidden not in body, f"autopilot calls {forbidden!r} — outside its blast radius"


def test_the_corpus_dedupe_is_not_on_the_timer():
    """Asked for explicitly: the frontier sweep runs unattended, deleting stored pages does not."""
    from mallory_orchestrator import api
    assert hasattr(api, "data_dedupe"), "the dedupe button is gone"
    assert "data_dedupe" not in Path(autopilot.__file__).read_text(encoding="utf-8")


def test_it_still_RUNS_after_its_panel_was_removed():
    """The panel was removed from the console (asked for: the page had grown too long to scan).
    What must not follow is the automation quietly going with it — "an automation nobody can see is
    indistinguishable from one that died three days ago" was the original point, and deleting the
    display makes that MORE true, not less. So the loop, the timer and the endpoint are what get
    pinned now, and /api/autopilot stays reachable for anyone who wants to look.
    """
    import asyncio
    from mallory_orchestrator import api, autopilot, main
    assert callable(autopilot.loop) and callable(autopilot.run_once)
    assert autopilot.enabled(), "autopilot switched off, not merely hidden"
    assert autopilot.interval_s() >= 300
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "autopilot.loop" in src, "the timer is no longer started at boot"
    assert "/api/autopilot" in {r.path for r in api.router.routes}, "the endpoint went with the panel"


def test_the_removed_panels_left_no_dead_javascript():
    """A handler bound to an element that no longer exists is a silent console error on every
    poll — and this dashboard has already lost a whole script block to one bad line."""
    from mallory_orchestrator import api
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    for gone in ('id="autopilot"', 'id="alarms"', 'id="btnApNow"', 'id="perSiteDone"', "covCell"):
        assert gone not in html, "%s still referenced after its panel was removed" % gone


def test_the_endpoints_are_registered():
    from mallory_orchestrator import api
    paths = {r.path for r in api.router.routes}
    assert "/api/autopilot" in paths
    assert "/api/autopilot/run" in paths


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
