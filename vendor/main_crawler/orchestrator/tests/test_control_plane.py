"""The control plane as the operator meets it: Stop stays stopped, the mode switch is exclusive
and leaves nothing behind, and every timer is an opt-in toggle that Stop overrides.

Three product decisions, each pinned here from the API down to the loop that would have broken it:

  1. SEPARATION = one mode switch. NORMAL (the daily lane + the schedule) and CAMPAIGN (ad-hoc
     batches) are mutually exclusive; switching stops the other mode's work in flight, parks its
     frontiers, and re-pins the daily domains — nothing lingers.
  2. STOP = a hard stop that STAYS stopped. Every lane closes and every timer is disabled until an
     explicit Start; no teardown, no refill, no autopilot, no maintenance-off, no dispatch route
     can lift it.
  3. AUTO-DISPATCH = opt-in toggles, and Stop always wins.

Every "does not" here has a negative control — the same call succeeds once the state allows it —
so a gate that is merely a broken dispatcher cannot pass.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from mallory_orchestrator import (api, autopilot, daily, orchestrate, policy, refill,  # noqa: E402
                                  scheduler)
from mallory_orchestrator.models import Base, DailyUrl, DomainShard, ScheduleLog  # noqa: E402

NOW = dt.datetime(2026, 9, 20, 12, 0, 0)
N = 4


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def SL(monkeypatch, tmp_path):
    """A FILE database wired into every module's SessionLocal (see test_daily_lane for why not
    :memory:). Yields the sessionmaker, so a test can open a SECOND session — which is how "survives
    a restart" and "seen from another thread" are asserted rather than assumed."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "t.db"),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, expire_on_commit=False)
    import mallory_orchestrator.db as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "SessionLocal", SL)
    monkeypatch.setattr(daily, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "MAX_BUSY_INSTANCES", 0)
    scheduler._TOGGLE_SEEN["scheduled"] = None
    orchestrate._MANUAL.update(running=False, total=0, done=0, halted=None, error=None, sites=[],
                               lane="manual")
    autopilot._history.clear()
    return SL


@pytest.fixture()
def db(SL):
    with SL() as s:
        policy.start(s)
        yield s


class _Fleet:
    """N crawler instances, all idle, with teardown FINISHED: running=False and stop_pending=False,
    which is the state in which a halted instance is byte-identical to one that ran out of work.
    Each holds one normal-mode frontier and one campaign frontier. Records every stop and resume."""

    def __init__(self, n=N):
        self.bases = ["http://c%d:8099" % i for i in range(n)]
        self.running = False
        self.jobs = [{"job_id": "daily_news0_example", "pending": 10, "has_config": True},
                     {"job_id": "full_acme_com", "pending": 900, "has_config": True}]
        self.stops: list[str] = []
        self.resumed: list[tuple[str, list[str]]] = []
        self.dispatched: list[str] = []

    def pool(self):
        return list(self.bases)

    def batch_status(self, base=None):
        return {"running": self.running, "stop_pending": False}

    def resumable(self, base=None, limit=1000):
        return {"jobs": [dict(j) for j in self.jobs]}

    def resume(self, ids, background=True, forward_to_ingest=True, base=None):
        self.resumed.append((base, list(ids)))
        return {"resumed": list(ids), "started": True}

    def stop_crawl(self, base=None):
        self.stops.append(base)
        return {"stopping": True}

    def get_metrics(self, base=None):
        return {"lanes": {"frontier": 0, "render_q": 0}, "batch": {"running": self.running, "total": 0}}

    def dispatch(self, job, timeout=None, base=None):
        self.dispatched.append(job["job_id"])
        return {"summary": {"fetched": 1, "kept": 1, "stop_reason": "completed"}}

    def dispatch_batch(self, payload, timeout=None, base=None):
        ids = [j["job_id"] for j in payload["jobs"]]
        self.dispatched.extend(ids)
        return {"jobs": len(ids), "results": [
            {"job_id": i, "summary": {"fetched": 1, "kept": 1, "stop_reason": "completed"}} for i in ids]}

    def fanout(self, fn, *a, **kw):
        return [{"instance": i, "base": b, "ok": True, "data": fn(*a, **kw, base=b)}
                for i, b in enumerate(self.bases)]


@pytest.fixture()
def fleet(monkeypatch):
    f = _Fleet()
    for mod in (api, refill, daily, orchestrate, scheduler):
        monkeypatch.setattr(mod, "crawler_client", f)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "0,1")
    monkeypatch.setenv("CRAWLER_DAILY_EXCLUSIVE", "0")
    return f


class _Thread:
    """Records what a route would have started, and starts nothing."""
    started: list[tuple] = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append((self.target.__name__, self.args))


@pytest.fixture()
def client(db, fleet, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: db
    _Thread.started.clear()
    monkeypatch.setattr(api, "Thread", _Thread)
    monkeypatch.setattr(api, "_ingest_get", lambda *a, **k: [])
    return TestClient(app)


def _daily_site(db, dom="news0.example", inst=0):
    db.add(DailyUrl(url="https://%s/" % dom, domain=dom, enabled=True, next_run=NOW, created_at=NOW))
    db.add(DomainShard(domain=dom, instance=inst))
    db.commit()


def _events(db):
    return [r.event for r in db.query(ScheduleLog).all()]


def _quiet_autopilot(monkeypatch, calls):
    """Stub every autopilot action but leave run_once's own control flow real."""
    monkeypatch.setattr(api, "crawler_fleet_assign", lambda db=None: calls.append("assign") or
                        {"ok": True, "summary": "assigned"})
    monkeypatch.setattr(api, "crawler_fleet_prune", lambda apply=False: calls.append("traps") or
                        {"ok": True, "summary": "", "junk": 0})
    monkeypatch.setattr(api, "crawler_fleet_health", lambda: {"verdict": "ok", "detail": ""})
    monkeypatch.setattr(api, "seeds_recheck", lambda **k: {"ok": True, "summary": ""})
    monkeypatch.setattr(api, "selfcheck_run", lambda db=None: {"ok": True, "summary": ""})
    monkeypatch.setattr(api, "data_quality", lambda: {"reachable": True})


# ══ (i) STOP halts and STAYS stopped ══════════════════════════════════════════

def test_stop_records_the_state_before_the_broadcast_and_halts_every_instance(client, db, fleet):
    r = client.post("/api/control/stop")
    assert r.status_code == 200 and r.json()["run"] == "stopped", r.text
    assert r.json()["stop"] == {"acked": N, "total": N, "stopping": True, "failed": []}
    assert len(fleet.stops) == N, "a stop that one container did not hear is not a stop"
    assert policy.stopped(db) and "stop" in _events(db)
    st = client.get("/api/control/state").json()
    assert st["run"] == "stopped" and st["lanes"]["open"] == [] and st["halted"] is True
    assert st["master"] is False, "the master switch and the run state are ONE thing"


def test_stop_survives_a_restart(client, SL):
    """Durable: a fresh session — which is what a recreated container has — reads it back."""
    client.post("/api/control/stop")
    with SL() as fresh:
        assert policy.stopped(fresh)
        assert not any(policy.lane_open(fresh, lane) for lane in policy.LANES)


def test_after_teardown_nothing_revives_a_stopped_fleet(db, fleet, monkeypatch):
    """THE BUG THIS PRODUCT EXISTS FOR. Teardown has finished (running=False, stop_pending=False),
    every instance holds a saved frontier, and EVERY timer toggle is ON. Stopped means every one
    of them does nothing — and the moment Start is pressed the same calls do their job."""
    for name in policy.AUTOS:
        policy.set_auto(db, name, True)
    policy.stop(db)
    calls: list[str] = []
    _quiet_autopilot(monkeypatch, calls)
    monkeypatch.setattr(scheduler, "run_due", lambda db_, **k: calls.append("run_due") or 0)
    _daily_site(db)

    assert refill.tick() is None, "the refill timer ran while stopped"
    out = refill.refill_once(force=True)
    assert out["resumed"] == 0 and out.get("halted") and fleet.resumed == []
    assert autopilot.tick() is None, "the autopilot timer ran while stopped"
    assert "stopped" in autopilot.run_once()["skipped"]
    scheduler._tick_once()
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False,
                      dispatch=lambda job, base: calls.append("daily") or {"summary": {}},
                      preempt_status=lambda base: {"running": True, "current_job": "cat_x"},
                      preempt_stop=lambda base: calls.append("preempt"))
    orchestrate.run_manual_batch([{"job_id": "full_x_com", "seed_urls": ["https://x.com/"]}], 0)
    assert n == 0 and calls == [], calls
    assert fleet.dispatched == [] and fleet.resumed == []
    assert policy.stopped(db), "something on a timer lifted the stop"

    # Negative control: Start, and the identical calls put the fleet back to work.
    policy.start(db)
    assert refill.tick() is not None and fleet.resumed, "refill did nothing after Start"
    autopilot.run_once()
    assert "assign" in calls
    scheduler._tick_once()
    assert "run_due" in calls


def test_no_dispatch_route_lifts_a_stop(client, db, fleet):
    """Every route a placer script could call. Each is refused or does nothing, none writes the
    run state, and the fleet is still stopped at the end."""
    policy.set_auto(db, "refill", True)
    client.post("/api/control/stop")
    fleet.stops.clear()

    r = client.post("/api/crawler/resume", json={"job_ids": ["daily_news0_example"]})
    assert r.status_code == 409 and "stopped" in r.json()["detail"], r.text
    r = client.post("/api/crawler/batch", json={"jobs": [{"job_id": "full_x_com", "seed_urls": ["https://x.com/"]}]})
    assert r.json()["started"] is False and "stopped" in r.json()["error"], r.text
    r = client.post("/api/crawler/fleet/assign")
    assert r.json()["ok"] is False and r.json()["halted"] is True, r.text
    r = client.post("/api/crawler/fleet/maintenance?on=false")
    assert r.json()["resumed"] == 0, r.text
    r = client.post("/api/crawler/fleet/refill")
    assert r.json()["resumed"] == 0 and r.json().get("halted"), r.text
    r = client.post("/api/autopilot/run")
    assert "stopped" in r.json()["skipped"], r.text
    assert client.post("/api/control/dispatch", json={"lanes": None}).status_code == 410
    r = client.post("/api/control/mode", json={"mode": "campaign"})
    assert r.json()["run"] == "stopped", "a mode switch must not start the pipeline"

    assert fleet.resumed == [] and fleet.dispatched == [] and _Thread.started == []
    assert policy.stopped(db)
    assert client.get("/api/control/state").json()["run"] == "stopped"


def test_only_start_lifts_a_stop_and_the_master_switch_is_the_same_control(client, db, fleet):
    client.post("/api/control/stop")
    r = client.post("/api/control/start")
    assert r.json()["run"] == "running" and r.json()["changed"] is True
    assert policy.running(db) and policy.open_lanes(db) == {"daily", "scheduled", "resume"}
    assert "start" in _events(db)
    # The master switch maps onto the same two operations — one stop, not two.
    r = client.post("/api/control/master", json={"enabled": False})
    assert r.json()["run"] == "stopped" and policy.stopped(db) and len(fleet.stops) == 2 * N
    r = client.post("/api/control/master", json={"enabled": True})
    assert r.json()["run"] == "running" and policy.running(db)
    # And the old Stop button's route is the same hard stop.
    r = client.post("/api/crawler/stop")
    assert r.json()["run"] == "stopped" and policy.stopped(db)


def test_stop_is_idempotent_and_says_so(client, db):
    assert client.post("/api/control/stop").json()["changed"] is True
    assert client.post("/api/control/stop").json()["changed"] is False
    assert client.post("/api/control/start").json()["changed"] is True
    assert client.post("/api/control/start").json()["changed"] is False


# ══ (ii) the MODE switch: exclusive, and clean on switch / stop ═══════════════

def test_exactly_one_mode_is_active_and_the_lanes_follow_it(client, db):
    st = client.get("/api/control/state").json()
    assert st["mode"] == "normal" and st["lanes"]["open"] == ["daily", "resume", "scheduled"]
    r = client.post("/api/control/mode", json={"mode": "campaign"})
    assert r.status_code == 200 and r.json()["changed"] is True
    st = client.get("/api/control/state").json()
    assert st["mode"] == "campaign" and st["lanes"]["open"] == ["manual", "resume"]
    assert st["lanes"]["closed"] == ["daily", "scheduled"]
    assert client.post("/api/control/mode", json={"mode": "both"}).status_code == 400
    assert client.post("/api/control/mode", json={"mode": "campaign"}).json()["changed"] is False


def test_switching_mode_stops_in_flight_work_and_leaves_the_run_state_alone(client, db, fleet):
    fleet.running = True
    r = client.post("/api/control/mode", json={"mode": "campaign"})
    assert r.json()["stop"]["acked"] == N and len(fleet.stops) == N, "in-flight normal work was not stopped"
    assert r.json()["run"] == "running" and policy.running(db)
    fleet.stops.clear()
    r = client.post("/api/control/mode", json={"mode": "normal"})
    assert len(fleet.stops) == N, "in-flight campaign work was not stopped"
    assert policy.running(db)


def test_returning_to_normal_pins_daily_domains_back_onto_the_lane(client, db, fleet):
    """No orphaned pins. A rebalance during the campaign moved a daily domain onto instance 3,
    which the lane (0,1) never dispatches to — so the site would be due and undispatchable."""
    _daily_site(db, inst=3)
    client.post("/api/control/mode", json={"mode": "campaign"})
    r = client.post("/api/control/mode", json={"mode": "normal"})
    assert r.json()["pinned"]["moved"] == 1, r.json()
    db.expire_all()
    assert db.get(DomainShard, "news0.example").instance in (0, 1)
    assert "daily_pinned" in _events(db)


def test_no_revived_backlog_after_the_campaign(client, db, fleet):
    """The owner's sentence: "if I stop the competitor campaign and start my normal crawl, it
    should just work." Campaign frontiers stay parked; the daily lane runs; nothing else."""
    _daily_site(db)
    policy.set_auto(db, "refill", True)
    client.post("/api/control/mode", json={"mode": "campaign"})
    client.post("/api/control/stop")
    client.post("/api/control/mode", json={"mode": "normal"})
    client.post("/api/control/start")
    # The refill timer, with its toggle ON, resumes the daily frontier and parks the campaign's.
    # Instances 0,1 are RESERVED for the daily lane (CRAWLER_DAILY_INSTANCES="0,1"), so refill holds
    # those for the lane itself (run_due, below) and refills the other two — resuming their daily
    # frontier and parking the campaign one. Held + refilled == the whole fleet.
    out = refill.tick()
    reserved = 2
    assert out["resumed"] == N - reserved and out["parked"] == N - reserved, out
    assert out.get("held_for_daily") == reserved, out
    assert all(ids == ["daily_news0_example"] for _, ids in fleet.resumed), fleet.resumed
    # The daily lane dispatches its due site to the owning lane instance.
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: fleet.dispatch(job, base=base))
    assert n == 1 and fleet.dispatched == ["daily_news0_example"]
    # An explicit resume of the campaign's frontier is refused in normal mode, by name.
    r = client.post("/api/crawler/resume", json={"job_ids": ["full_acme_com"]})
    assert r.status_code == 409 and "full_acme_com" in r.json()["detail"]


def test_campaign_mode_runs_the_batch_and_holds_the_daily_lane(client, db, fleet):
    _daily_site(db)
    client.post("/api/control/mode", json={"mode": "campaign"})
    r = client.post("/api/crawler/batch", json={"jobs": [{"job_id": "full_x_com", "seed_urls": ["https://x.com/"]}]})
    assert r.json()["started"] is True and _Thread.started[-1][0] == "run_manual_batch"
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False,
                      dispatch=lambda job, base: fleet.dispatch(job, base=base))
    assert n == 0 and fleet.dispatched == [] and "daily_skipped_lane_closed" in _events(db)
    # Refill in campaign mode resumes the campaign's frontiers and parks the daily ones.
    out = refill.refill_once(force=True)
    assert all(ids == ["full_acme_com"] for _, ids in fleet.resumed) and out["parked"] == N
    # The lane hold that reserves instances for daily sites is off: the campaign has the fleet.
    assert not daily.holds_fleet(db, NOW)


# ══ (iii) the toggles: opt-in, independent, and Stop overrides them ═══════════

def test_every_toggle_is_off_by_default_and_reported(client):
    st = client.get("/api/control/auto").json()
    assert set(st["auto"]) == set(policy.AUTOS) == {"scheduled", "refill", "autopilot",
                                                    "health_dispatch", "repin"}
    assert not any(v["on"] or v["effective"] for v in st["auto"].values())


def test_a_toggle_is_stored_and_effective_only_while_running(client, db):
    r = client.post("/api/control/auto", json={"name": "refill", "enabled": True})
    assert r.json()["auto"]["refill"] == {"on": True, "effective": True, "label": policy.AUTOS["refill"]}
    client.post("/api/control/stop")
    a = client.get("/api/control/auto").json()["auto"]["refill"]
    assert a["on"] is True and a["effective"] is False, "Stop must hold the timer, not flip its switch"
    client.post("/api/control/start")
    assert client.get("/api/control/auto").json()["auto"]["refill"]["effective"] is True
    assert client.post("/api/control/auto", json={"name": "warp", "enabled": True}).status_code == 400


def test_the_scheduler_tick_needs_its_toggle(db, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "run_due", lambda db_, **k: calls.append("run_due") or 0)
    scheduler._tick_once()
    assert calls == [], "the catalogue schedule dispatched with its toggle off"
    policy.set_auto(db, "scheduled", True)
    scheduler._tick_once()
    assert calls == ["run_due"]
    policy.stop(db)
    scheduler._tick_once()
    assert calls == ["run_due"], "Stop did not override the scheduled toggle"


def test_the_refill_timer_needs_its_toggle_but_the_button_does_not(db, fleet):
    assert refill.tick() is None
    assert refill.refill_once()["resumed"] == N, "a button press is the operator asking"
    fleet.resumed.clear()
    policy.set_auto(db, "refill", True)
    assert refill.tick()["resumed"] == N
    policy.stop(db)
    assert refill.tick() is None, "Stop did not override the refill toggle"


def test_the_autopilot_and_its_assign_step_are_separate_toggles(db, fleet, monkeypatch):
    calls: list[str] = []
    _quiet_autopilot(monkeypatch, calls)
    assert autopilot.tick() is None
    policy.set_auto(db, "autopilot", True)
    out = autopilot.tick()
    assert out is not None and "traps" in calls and "assign" not in calls, calls
    assert any(a.get("skipped") == "health_dispatch toggle off" for a in out["actions"]), out
    policy.set_auto(db, "health_dispatch", True)
    autopilot.tick()
    assert "assign" in calls
    policy.stop(db)
    calls.clear()
    assert autopilot.tick() is None and "stopped" in autopilot.run_once()["skipped"]
    assert calls == []


def test_the_daily_repin_needs_its_toggle_and_a_drifted_site_is_reported(db, fleet):
    _daily_site(db, inst=3)                       # drifted off the lane (0,1)
    sent = []
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: sent.append(base) or {"summary": {}})
    db.expire_all()
    assert n == 0 and sent == [] and db.get(DomainShard, "news0.example").instance == 3
    assert "daily_unpinned" in _events(db), "an undispatchable due site must be named, not silent"
    policy.set_auto(db, "repin", True)
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: sent.append(base) or {"summary": {}})
    db.expire_all()
    assert n == 1 and db.get(DomainShard, "news0.example").instance in (0, 1)


def test_maintenance_off_resumes_only_with_the_refill_toggle(client, db, fleet):
    r = client.post("/api/crawler/fleet/maintenance?on=false")
    assert r.json()["resumed"] == 0 and "toggle is off" in r.json()["summary"] and fleet.resumed == []
    policy.set_auto(db, "refill", True)
    r = client.post("/api/crawler/fleet/maintenance?on=false")
    assert r.json()["resumed"] == N and fleet.resumed


def test_state_explains_a_fleet_that_will_not_start(client, db, monkeypatch):
    """Every hold is visible next to the mode: stopped, maintenance, each toggle's effect — and a
    loop the environment never started, so a toggle over a dead loop is never shown as live."""
    scheduler.set_maintenance(db, True)
    st = client.get("/api/control/state").json()
    assert st["run"] == "running" and st["lanes"]["held"] == "maintenance mode is on"
    assert st["lanes"]["open"] == [] and st["maintenance"] is True
    assert st["manual_batch"]["running"] is False and st["manual_batch"]["lane"] == "manual"
    assert st["loops"] == {"refill": True, "autopilot": True, "scheduled": True,
                           "health_dispatch": True, "repin": True}
    monkeypatch.setenv("CRAWLER_AUTOPILOT", "0")
    monkeypatch.setenv("CRAWLER_AUTO_REFILL", "0")
    lp = client.get("/api/control/state").json()["loops"]
    assert lp["autopilot"] is False and lp["health_dispatch"] is False and lp["refill"] is False
