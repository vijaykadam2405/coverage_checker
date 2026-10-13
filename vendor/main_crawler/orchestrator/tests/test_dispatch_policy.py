"""ONE gate for everything that can start or resume a crawl — and it is DERIVED, never set.

MEASURED 2026-09-19 on the live fleet: it took an operator an hour to stop every crawl and run only
two lanes on twelve instances. No switch was broken — each dispatch path simply read a different
subset of them:

    POST /api/crawler/stop     halted=true, 12/12 acked. The daily lane and the manual-batch loop
                               never read `halted`, and /crawler/batch + /crawler/resume CLEARED it,
                               so the next placer script re-armed the whole fleet.
    category switches          hold catalogue BUCKETS; a firstcrawl_/full_/kssl_ job has no bucket.
    CRAWLER_AUTO_REFILL=0      stops the refill TIMER at boot; the Assign button, the autopilot and
                               maintenance-off still call refill_once(force=True).
    toggle-all off             pauses 1,275 scheduled jobs; refill resumes their saved frontiers.

The first fix was a stored `dispatch_lanes` row every dispatcher read. This is its successor: the
lanes are computed from the RUN STATE (Start/Stop) and the MODE (normal/campaign) in policy.py, so
a stop or a mode switch cannot leave a lanes row behind to fight it. These tests pin the gate three
ways: each dispatcher no-ops when its lane is closed — by a stop AND by the other mode — does its
ordinary work when the lane is open (so a gate that is merely a broken dispatcher cannot pass), and
no dispatch route touches the stop.

The API surface of the control plane itself is in test_control_plane.py.
"""
from __future__ import annotations

import datetime as dt
import inspect
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from mallory_orchestrator import api, daily, orchestrate, policy, refill, scheduler  # noqa: E402
from mallory_orchestrator.models import (Base, DailyUrl, DomainShard, ScheduledJob,  # noqa: E402
                                         ScheduleLog)

NOW = dt.datetime(2026, 9, 19, 12, 0, 0)
POOL = ",".join("http://c%d:8099" % i for i in range(4))


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """A FILE database (see test_daily_lane for why not :memory:) wired into EVERY module's
    SessionLocal — refill and orchestrate import it lazily from .db, scheduler and daily at load.
    Running, normal mode, no toggle set: the state a deployment is in after Start."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "t.db"),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, expire_on_commit=False)
    import mallory_orchestrator.db as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "SessionLocal", SL)
    monkeypatch.setattr(daily, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "MAX_BUSY_INSTANCES", 0)
    orchestrate._MANUAL.update(running=False, total=0, done=0, halted=None, error=None, sites=[],
                               lane="manual")
    with SL() as s:
        policy.start(s)
        yield s


def _due_job(db, jid="j1"):
    sj = ScheduledJob(name=jid, job_type="news", frequency="once", retry_count=0, retries_left=0,
                      job={"job_id": jid, "job_type": "news", "seed_urls": ["https://%s.example/" % jid]},
                      start_at=None, next_run=NOW - dt.timedelta(seconds=1), last_status="pending",
                      enabled=True, created_at=NOW)
    db.add(sj)
    db.commit()
    db.refresh(sj)
    return sj


def _events(db):
    return [r.event for r in db.query(ScheduleLog).all()]


# ── the gate itself ───────────────────────────────────────────────────────────

def test_a_fresh_deployment_is_stopped_in_normal_mode(monkeypatch, tmp_path):
    """No rows at all: the state a deployment boots into. Stopped (the master used to default to
    off for the same reason: nothing auto-crawls until the operator arms it), normal mode, every
    toggle off, every lane closed."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "fresh.db"))
    Base.metadata.create_all(eng)
    with sessionmaker(bind=eng)() as s:
        assert policy.stopped(s) and policy.mode(s) == "normal"
        assert not any(policy.lane_open(s, lane) for lane in policy.LANES)
        assert not any(policy.auto_setting(s, n) for n in policy.AUTOS)


def test_normal_and_campaign_open_disjoint_lanes(db):
    assert policy.open_lanes(db) == {"daily", "scheduled", "resume"}
    policy.set_mode(db, "campaign")
    assert policy.open_lanes(db) == {"manual", "resume"}
    assert "mode" in _events(db)


def test_stop_closes_every_lane_in_either_mode_and_only_start_reopens(db):
    for m in ("normal", "campaign"):
        policy.set_mode(db, m)
        policy.stop(db)
        assert not any(policy.lane_open(db, lane) for lane in policy.LANES), m
        assert policy.why_closed(db, "daily").startswith("stopped")
        policy.start(db)
        assert policy.open_lanes(db) == set(policy.MODE_LANES[m])


def test_maintenance_closes_every_lane_without_changing_the_run_state(db):
    scheduler.set_maintenance(db, True)
    assert policy.running(db) and not policy.open_lanes(db)
    assert policy.why_closed(db, "daily") == "maintenance mode is on"
    scheduler.set_maintenance(db, False)
    assert policy.open_lanes(db)


def test_a_lane_nobody_declared_is_closed(db):
    assert not policy.lane_open(db, "something_new")


def test_a_typo_in_the_mode_is_refused_not_applied(db):
    with pytest.raises(ValueError):
        policy.set_mode(db, "campain")
    assert policy.mode(db) == "normal"


def test_the_old_dispatch_lanes_row_is_ignored(db):
    """The DC ran a stored `dispatch_lanes` row ("daily") for a day. It must not survive as a
    third thing holding the fleet."""
    from mallory_orchestrator.models import Setting
    db.add(Setting(key="dispatch_lanes", value=""))       # the old "frozen"
    db.commit()
    assert policy.open_lanes(db) == {"daily", "scheduled", "resume"}


# ── scheduler.run_due (the 20s tick) ──────────────────────────────────────────

def _spy():
    sent = []
    return sent, (lambda job: sent.append(job["job_id"]) or {"summary": {}})


def test_scheduler_holds_when_stopped_or_in_campaign_mode(db):
    _due_job(db)
    sent, dispatch = _spy()
    policy.stop(db)
    assert scheduler.run_due(db, now=NOW, dispatch=dispatch, busy_check=lambda: False) == 0
    policy.start(db)
    policy.set_mode(db, "campaign")
    assert scheduler.run_due(db, now=NOW, dispatch=dispatch, busy_check=lambda: False) == 0
    assert sent == []
    assert "skipped_lane_closed" in _events(db), "a held tick must say why in the Logs tab"


def test_scheduler_dispatches_when_open(db):
    """Negative control: a gate that is really a broken dispatcher passes the test above."""
    _due_job(db)
    sent, dispatch = _spy()
    n = scheduler.run_due(db, now=NOW, dispatch=dispatch, busy_check=lambda: False)
    assert n == 1 and sent == ["j1"]


def test_scheduler_stops_mid_tick_when_stop_is_pressed(db):
    """dispatch() blocks for a whole crawl, so the state this tick was admitted on is stale by the
    second job — the same reason the master switch is re-read per job."""
    _due_job(db, "j1")
    _due_job(db, "j2")
    sent = []

    def dispatch(job):
        sent.append(job["job_id"])
        with scheduler.SessionLocal() as other:      # the operator presses Stop mid-tick
            policy.stop(other)
        return {"summary": {}}

    n = scheduler.run_due(db, now=NOW, dispatch=dispatch, busy_check=lambda: False)
    assert n == 1 and len(sent) == 1, sent


# ── orchestrate.run_batch (POST /api/run — read NONE of the other switches) ──

def test_the_run_button_is_scheduled_work(db, monkeypatch):
    sent = []
    monkeypatch.setattr(orchestrate.crawler_client, "dispatch",
                        lambda job, **k: sent.append(job["job_id"]) or {"summary": {}})
    job = {"job_id": "j1", "job_type": "news", "seed_urls": ["https://j1.example/"]}
    policy.set_mode(db, "campaign")
    out = orchestrate.run_batch(db, [job])
    assert out["dispatched"] == 0 and sent == [] and "closed" in out["held"]
    policy.set_mode(db, "normal")
    out = orchestrate.run_batch(db, [job])
    assert out["dispatched"] == 1 and sent == ["j1"]


# ── orchestrate.run_manual_batch (/crawler/batch, Assign's first-crawls, gap-fill) ──

def _routed(sent):
    def routed(db_, chunk, *, timeout):
        sent.append([j["job_id"] for j in chunk])
        return {"jobs": len(chunk), "results": [
            {"job_id": j["job_id"], "summary": {"fetched": 1, "kept": 1, "stop_reason": "completed"}}
            for j in chunk]}
    return routed


def test_a_pasted_batch_is_campaign_work(db, monkeypatch):
    sent = []
    monkeypatch.setattr(orchestrate, "_dispatch_routed", _routed(sent))
    jobs = [{"job_id": "full_x_com", "job_type": "profile", "seed_urls": ["https://x.com/"]}]
    orchestrate.run_manual_batch(jobs, 0)                 # normal mode: manual lane closed
    st = orchestrate.manual_status()
    assert sent == [] and st["done"] == 0, st
    assert "manual" in (st["halted"] or ""), "the batch must say it was held, not look finished"
    policy.set_mode(db, "campaign")
    orchestrate.run_manual_batch(jobs, 0)
    assert sent == [["full_x_com"]] and orchestrate.manual_status()["done"] == 1


def test_assigns_first_crawls_are_scheduled_work_not_campaign_work(db, monkeypatch):
    """Same loop, different lane: the Assign button's first-crawls of never-crawled catalogue
    sites run in NORMAL mode and are refused in a campaign — the opposite of a pasted batch."""
    sent = []
    monkeypatch.setattr(orchestrate, "_dispatch_routed", _routed(sent))
    jobs = [{"job_id": "firstcrawl_x_com", "job_type": "profile", "seed_urls": ["https://x.com/"]}]
    orchestrate.run_manual_batch(jobs, 0, lane="scheduled")
    assert sent == [["firstcrawl_x_com"]]
    policy.set_mode(db, "campaign")
    orchestrate.run_manual_batch(jobs, 0, lane="scheduled")
    assert sent == [["firstcrawl_x_com"]] and "scheduled" in (orchestrate.manual_status()["halted"] or "")


def test_a_manual_batch_stops_between_chunks_when_stop_is_pressed(db, monkeypatch):
    """THE TRAP the other flags have: SessionLocal is expire_on_commit=False and this loop holds
    ONE session for the life of the batch (hours), so a flag read through the identity map never
    sees the operator's write. _halt_reason expires before every chunk; this pins that the gate is
    read after that expire, and that a batch mid-way through its list is held."""
    policy.set_mode(db, "campaign")
    sent = []

    def routed(db_, chunk, *, timeout):
        sent.append([j["job_id"] for j in chunk])
        with scheduler.SessionLocal() as other:      # the operator presses Stop mid-batch
            policy.stop(other)
        return {"jobs": len(chunk), "results": [
            {"job_id": j["job_id"], "summary": {"fetched": 1, "kept": 1, "stop_reason": "completed"}}
            for j in chunk]}

    monkeypatch.setattr(orchestrate, "_dispatch_routed", routed)
    jobs = [{"job_id": "full_%s_com" % x, "job_type": "profile", "seed_urls": ["https://%s.com/" % x]}
            for x in ("a", "b")]
    orchestrate.run_manual_batch(jobs, 1)            # one site per chunk
    st = orchestrate.manual_status()
    assert sent == [["full_a_com"]], sent
    assert st["done"] == 1 and "stopped" in (st["halted"] or ""), st


# ── refill.refill_once (timer, button, Assign, autopilot, maintenance-off) ──

class _Fleet:
    """Two idle instances each holding a resumable frontier behind a STALE stop flag — the exact
    case `force` exists to override, so this proves force does not override the gate."""

    def __init__(self, n=2, jobs=None):
        self.bases = ["http://c%d:8099" % i for i in range(n)]
        self.jobs = jobs or [{"job_id": "recrawl_x_com", "pending": 500, "has_config": True}]
        self.resumed = []

    def pool(self):
        return list(self.bases)

    def batch_status(self, base=None):
        return {"running": False, "stop_pending": True}

    def resumable(self, base=None):
        return {"jobs": [dict(j) for j in self.jobs]}

    def resume(self, ids, background=True, forward_to_ingest=True, base=None):
        self.resumed.append((base, list(ids)))
        return {"resumed": list(ids)}


def test_refill_holds_when_stopped_even_when_forced(db, monkeypatch):
    fleet = _Fleet()
    monkeypatch.setattr(refill, "crawler_client", fleet)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "")     # no lane hold in play
    policy.stop(db)
    out = refill.refill_once(force=True)
    assert out["resumed"] == 0 and fleet.resumed == [], out
    assert out.get("halted"), "a held watcher must say so, not read as idle"
    policy.start(db)
    out = refill.refill_once(force=True)
    assert out["resumed"] == 2 and len(fleet.resumed) == 2, out


def test_refill_parks_the_other_modes_frontiers(db, monkeypatch):
    """THE REVIVED BACKLOG. An instance holds a daily site's frontier next to a campaign's. Normal
    mode resumes the daily one and PARKS the campaign's; campaign mode does the reverse. Parked is
    counted — an instance left idle on purpose must not read as one the watcher missed."""
    fleet = _Fleet(n=1, jobs=[{"job_id": "daily_news0_example", "pending": 10, "has_config": True},
                              {"job_id": "full_acme_com", "pending": 900, "has_config": True}])
    monkeypatch.setattr(refill, "crawler_client", fleet)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "")
    out = refill.refill_once(force=True)
    assert fleet.resumed == [("http://c0:8099", ["daily_news0_example"])], fleet.resumed
    assert out["parked"] == 1, out
    fleet.resumed.clear()
    policy.set_mode(db, "campaign")
    out = refill.refill_once(force=True)
    assert fleet.resumed == [("http://c0:8099", ["full_acme_com"])], fleet.resumed
    assert out["parked"] == 1, out


def test_an_enrolled_job_is_normal_work_whatever_its_id(db):
    """Ownership is not only the prefix: a job enrolled in scheduled_jobs under any name is the
    system's own work. Everything else was submitted by hand."""
    _due_job(db, "acme_special")
    assert policy.job_owner(db, "acme_special") == "normal"
    assert policy.job_owner(db, "daily_x") == "normal"
    assert policy.job_owner(db, "firstcrawl_x_com") == "normal"
    assert policy.job_owner(db, "full_acme_com") == "campaign"
    assert policy.job_owner(db, "kssl_x") == "campaign"


# ── daily.run_due (the lane NORMAL mode exists for) ──────────────────────────

class _Shim:
    crawler_api = "http://c0:8099"
    crawler_api_pool = POOL


def _lane(monkeypatch):
    monkeypatch.setenv("CRAWLER_API_POOL", POOL)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "0,1")
    monkeypatch.setenv("CRAWLER_DAILY_EXCLUSIVE", "0")
    from mallory_orchestrator import crawler_client
    monkeypatch.setattr(crawler_client, "_s", _Shim())


def _daily_site(db, dom="news0.example", inst=0):
    db.add(DailyUrl(url="https://%s/" % dom, domain=dom, enabled=True, next_run=NOW, created_at=NOW))
    db.add(DomainShard(domain=dom, instance=inst))
    db.commit()


def test_the_daily_lane_holds_in_campaign_mode_and_does_not_preempt(db, monkeypatch):
    """Closed means it does nothing at all — including preempt_for_due, which STOPS other work."""
    _lane(monkeypatch)
    _daily_site(db)
    sent, killed = [], []
    policy.set_mode(db, "campaign")
    n = daily.run_due(db, now=NOW, dispatch=lambda job, base: sent.append(job) or {"summary": {}},
                      busy_check=lambda i, b: False,
                      preempt_status=lambda base: {"running": True, "current_job": "cat_gov_x"},
                      preempt_stop=lambda base: killed.append(base))
    assert n == 0 and sent == [] and killed == []
    assert "daily_skipped_lane_closed" in _events(db)
    assert not daily.holds_fleet(db, NOW), "a closed lane must not hold instances for itself"


def test_the_daily_lane_dispatches_in_normal_mode(db, monkeypatch):
    _lane(monkeypatch)
    _daily_site(db)
    sent = []
    assert daily.holds_fleet(db, NOW)
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: sent.append((job["job_id"], base))
                      or {"summary": {"fetched": 2, "kept": 1}})
    assert n == 1 and sent == [("daily_news0_example", "http://c0:8099")]


def test_a_stopped_daily_crawl_retries_within_the_hour(db, monkeypatch):
    """A fleet-wide stop is step one of every mode switch. Before this, a daily site cut short by
    that stop was recorded as a good run and lost its day."""
    _lane(monkeypatch)
    _daily_site(db)
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: {"summary": {"fetched": 3, "kept": 0,
                                                              "stop_reason": "stopped"}})
    assert n == 0
    db.expire_all()
    row = db.get(DailyUrl, "https://news0.example/")
    assert row.last_status == "stopped"
    assert row.next_run == NOW + dt.timedelta(seconds=daily.RETRY_AFTER_S), row.next_run


def test_a_finished_daily_crawl_still_advances_a_day(db, monkeypatch):
    """Negative control for the retry: only a CUT-SHORT run comes back early."""
    _lane(monkeypatch)
    _daily_site(db)
    n = daily.run_due(db, now=NOW, busy_check=lambda i, b: False, preempt=False,
                      dispatch=lambda job, base: {"summary": {"fetched": 3, "kept": 1,
                                                              "stop_reason": "completed"}})
    assert n == 1
    db.expire_all()
    row = db.get(DailyUrl, "https://news0.example/")
    assert row.last_status == "ok" and row.next_run == NOW + dt.timedelta(days=1)


# ── the API: the routes that used to clear the halt ──────────────────────────

class _NoThread:
    def __init__(self, *a, **k):
        raise AssertionError("a dispatch thread was started while the lane was closed")


@pytest.fixture()
def client(db, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: db

    class Fleet:
        stops = 0

        def stop_crawl(self, base=None):
            return {"stopping": True}

        def fanout(self, fn, *a, **kw):
            if getattr(fn, "__name__", "") == "stop_crawl":
                self.stops += 1
                return [{"instance": 0, "ok": True, "data": fn()}]
            return []

        def pool(self):
            return ["http://c0:8099"]

    fleet = Fleet()
    monkeypatch.setattr(api, "crawler_client", fleet)
    monkeypatch.setattr(api, "Thread", _NoThread)
    return TestClient(app), fleet


def test_resume_is_refused_while_stopped_and_the_stop_is_left_alone(client, db):
    """/crawler/resume used to clear the operator halt as its FIRST line. Refused means untouched."""
    c, _ = client
    policy.stop(db)
    r = c.post("/api/crawler/resume", json={"job_ids": ["recrawl_x_com"]})
    assert r.status_code == 409 and "stopped" in r.json()["detail"], r.text
    assert policy.stopped(db), "a refused resume lifted the operator's stop"


def test_a_manual_batch_is_refused_before_any_thread_starts(client, db):
    c, _ = client
    r = c.post("/api/crawler/batch",
               json={"jobs": [{"job_id": "full_x_com", "seed_urls": ["https://x.com/"]}]})
    assert r.status_code == 200 and r.json()["started"] is False, r.text
    assert "normal mode" in r.json()["error"]
    policy.stop(db)
    policy.set_mode(db, "campaign")
    r = c.post("/api/crawler/batch",
               json={"jobs": [{"job_id": "full_x_com", "seed_urls": ["https://x.com/"]}]})
    assert r.json()["started"] is False and "stopped" in r.json()["error"]
    assert policy.stopped(db), "a refused batch lifted the operator's stop"


def test_run_delta_is_scheduled_work(client, db):
    c, _ = client
    sj = _due_job(db)
    policy.set_mode(db, "campaign")
    assert c.post("/api/schedule/%d/run-delta" % sj.id).status_code == 409


def test_the_retired_lanes_endpoint_says_where_the_controls_went(client):
    c, _ = client
    r = c.post("/api/control/dispatch", json={"lanes": ["daily"]})
    assert r.status_code == 410 and "/api/control/mode" in r.json()["detail"]
    assert c.get("/api/control/dispatch").json()["lanes"] == ["daily", "resume", "scheduled"]


# ── the invariant itself ──────────────────────────────────────────────────────

def test_every_dispatcher_consults_the_gate():
    """Pinned by text, because a dispatcher added without this check is exactly how the fourth
    copy of the 'stop did not stop' bug would ship. Every function below can put work on an
    instance; every one must read lane_open() before it does."""
    # The gap-fill route is found by what it does rather than by name, so a rename cannot drop it
    # from this list silently.
    gapfill = [fn for fn in {r.endpoint for r in api.router.routes}
               if "GAPFILL_MAX_SEEDS" in inspect.getsource(fn)]
    assert len(gapfill) == 1, gapfill
    dispatchers = [scheduler.run_due, scheduler._dispatch_serial, daily.run_due,
                   refill.refill_once, orchestrate.run_batch, orchestrate._halt_reason,
                   api.crawler_resume, api.crawler_batch, api.run_delta,
                   api.crawler_fleet_assign, gapfill[0]]
    for fn in dispatchers:
        assert "lane_open(" in inspect.getsource(fn), \
            "%s.%s does not consult the dispatch policy" % (fn.__module__, fn.__name__)


def test_no_dispatch_route_writes_the_run_state():
    """The three routes that CLEARED the halt are why a placer script could re-arm the fleet.
    Only the control routes (stop/start/master) may write it. Pinned by text."""
    writers = ("policy.start(", "policy.stop(", "set_halted(", "set_master(")
    for fn in (api.crawler_resume, api.crawler_batch, api.crawler_fleet_assign,
               api.crawler_fleet_maintenance, api.run_delta, api.data_coverage_fill):
        src = inspect.getsource(fn)
        assert not any(w in src for w in writers), "%s writes the run state" % fn.__name__
