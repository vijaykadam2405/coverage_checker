"""The daily lane, 3 instances, news-first-then-backlog.

These drive the real decision functions with an injected dispatch, so they assert the CONTROL FLOW
(which site went to which instance, and what the catalogue dispatcher did while news was due) and
not merely that rows were written.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from mallory_orchestrator import config, daily, scheduler, shard  # noqa: E402
from mallory_orchestrator.models import Base, DailyUrl, DomainShard  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

NOW = dt.datetime(2026, 9, 4, 12, 0, 0)
POOL = ",".join("http://c%d:8099" % i for i in range(12))


@pytest.fixture()
def db(monkeypatch, tmp_path):
    # A FILE, not ":memory:". run_due dispatches from a thread pool and each worker opens its own
    # Session, which is right for Postgres in production. In-memory SQLite cannot model that: one
    # database per connection hides the tables from the workers, and forcing a shared connection
    # with StaticPool then breaks under concurrent use ("no more rows available"). A file gives
    # each worker a real independent connection to the same database, which is what production has.
    eng = create_engine("sqlite:///%s" % (tmp_path / "t.db"),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(daily, "SessionLocal", SL)
    with SL() as s:
        # master_enabled() fails CLOSED - no Setting row means off. That is the right production
        # default and it has to be armed explicitly here, or every dispatch test silently asserts
        # nothing.
        scheduler.set_master(s, True)
        s.commit()
        yield s


class _Shim:
    """Settings stand-in for crawler_client.

    crawler_client binds `_s = get_settings()` at import, and Settings is a dataclass whose field
    defaults are evaluated at CLASS creation - so no amount of monkeypatching the environment
    changes what pool() sees. Replacing the object is the only honest way to point it at a
    12-instance fleet from a test.
    """

    crawler_api = "http://c0:8099"
    crawler_api_pool = POOL


def _env(monkeypatch, instances="0,1,2", exclusive="0"):
    monkeypatch.setenv("CRAWLER_API_POOL", POOL)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", instances)
    monkeypatch.setenv("CRAWLER_DAILY_EXCLUSIVE", exclusive)
    from mallory_orchestrator import crawler_client
    monkeypatch.setattr(crawler_client, "_s", _Shim())


def _sites(db: Session, n: int, due_at=NOW, prefix="n", lane=(0, 1, 2)):
    """n due sites, each OWNED by a lane instance (round-robin, so ownership is deterministic).

    The shard row is not decoration. The lane dispatches a site to the instance that owns it, so a
    DailyUrl with no shard row is not dispatchable at all — which is the point: it is what stops a
    site being sent to a container that holds neither its frontier nor its crawl history.
    """
    for i in range(n):
        dom = "%s%d.example" % (prefix, i)
        db.add(DailyUrl(url="https://%s/" % dom, domain=dom,
                        enabled=True, next_run=due_at, created_at=NOW))
        if lane:
            db.add(DomainShard(domain=dom, instance=lane[i % len(lane)]))
    db.commit()


# ── config parsing ────────────────────────────────────────────────────────────────────────

def test_instance_list_parses_plural_and_falls_back_to_singular(monkeypatch):
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "2, 0 ,1,1")
    assert config.daily_instance_list(config.get_settings()) == [0, 1, 2]  # sorted, deduped
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "")
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCE", "6")
    assert config.daily_instance_list(config.get_settings()) == [6]
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCE", "-1")
    assert config.daily_instance_list(config.get_settings()) == []


def test_a_typo_in_the_list_does_not_stop_the_orchestrator(monkeypatch):
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "0,x,2")
    assert config.daily_instance_list(config.get_settings()) == [0, 2]


def test_instances_past_the_pool_are_dropped_not_clamped(monkeypatch):
    _env(monkeypatch, instances="0,1,99")
    res = daily.reservation()
    assert res["instances"] == [0, 1]          # 99 must NOT become 9 or 11
    assert res["missing"] == [99]
    assert "99" in res["reason"]


# ── the shard carve-out ───────────────────────────────────────────────────────────────────

def test_priority_mode_leaves_the_whole_fleet_assignable(monkeypatch):
    _env(monkeypatch, exclusive="0")
    assert shard.allowed_instances(12) == list(range(12))


def test_exclusive_mode_carves_the_lane_out(monkeypatch):
    _env(monkeypatch, exclusive="1")
    assert shard.allowed_instances(12) == list(range(3, 12))


def test_exclusive_carve_out_never_leaves_nothing(monkeypatch):
    _env(monkeypatch, instances="0", exclusive="1")
    assert shard.allowed_instances(1) == [0]   # sharing beats routing into a void


# ── dispatch ──────────────────────────────────────────────────────────────────────────────

def test_due_sites_go_out_across_all_three_instances_concurrently(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 3)
    seen = []
    sent = daily.run_due(db, now=NOW,
                         dispatch=lambda job, base: seen.append((job["job_id"], base)) or {},
                         busy_check=lambda i, b: False)
    assert sent == 3
    assert sorted(b for _, b in seen) == ["http://c0:8099", "http://c1:8099", "http://c2:8099"]


def test_a_busy_instance_is_skipped_not_waited_on(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 3)
    seen = []
    # instance 1 is mid-crawl on catalogue work; the lane uses the other two this tick.
    sent = daily.run_due(db, now=NOW,
                         dispatch=lambda job, base: seen.append(base) or {},
                         busy_check=lambda i, b: i == 1)
    assert sent == 2
    assert sorted(seen) == ["http://c0:8099", "http://c2:8099"]


def test_all_instances_busy_dispatches_nothing_and_keeps_next_run(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 2)
    before = [r.next_run for r in db.query(DailyUrl).all()]
    sent = daily.run_due(db, now=NOW, dispatch=lambda job, base: {},
                         busy_check=lambda i, b: True)
    assert sent == 0
    db.expire_all()
    # A skipped day must never be a lost day: the rows stay due.
    assert [r.next_run for r in db.query(DailyUrl).all()] == before


def test_a_failed_site_retries_within_the_hour_and_does_not_block_the_others(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 3)

    def flaky(job, base):
        if base.endswith("c1:8099"):
            raise RuntimeError("connection refused")
        return {"summary": {"fetched": 10, "kept": 4, "accepted": 4}}

    sent = daily.run_due(db, now=NOW, dispatch=flaky, busy_check=lambda i, b: False)
    assert sent == 2
    db.expire_all()
    rows = {r.last_status for r in db.query(DailyUrl).all()}
    assert rows == {"ok", "error"}
    bad = db.query(DailyUrl).filter(DailyUrl.last_status == "error").one()
    assert bad.next_run == NOW + dt.timedelta(seconds=daily.RETRY_AFTER_S)


def test_success_keeps_the_site_s_hour_of_the_day(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 1, due_at=NOW - dt.timedelta(minutes=90))
    daily.run_due(db, now=NOW,
                  dispatch=lambda job, base: {"summary": {"fetched": 9, "kept": 3, "accepted": 3}},
                  busy_check=lambda i, b: False)
    db.expire_all()
    row = db.query(DailyUrl).one()
    # advanced FROM the scheduled time, not from now, so the slot does not drift later each day
    assert row.next_run == NOW - dt.timedelta(minutes=90) + dt.timedelta(days=1)
    assert row.runs == 1


def test_overdue_counts_what_priority_mode_actually_cost(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 2, due_at=NOW - dt.timedelta(hours=30), prefix="late")
    _sites(db, 1, due_at=NOW - dt.timedelta(hours=2), prefix="recent")
    assert len(daily.due(db, NOW)) == 3
    assert len(daily.overdue(db, NOW)) == 2


def test_has_due_is_the_dispatcher_signal(monkeypatch, db):
    _env(monkeypatch)
    assert daily.has_due(db, NOW) is False
    _sites(db, 1, due_at=NOW + dt.timedelta(hours=3), prefix="future")
    assert daily.has_due(db, NOW) is False        # scheduled, not yet due
    _sites(db, 1, due_at=NOW - dt.timedelta(minutes=1), prefix="past")
    assert daily.has_due(db, NOW) is True


# ── news first, then the backlog ──────────────────────────────────────────────────────────

def _routed(db, monkeypatch, chunk, *, due):
    """Run the real _dispatch_routed with HTTP stubbed, and report which instances it sent to."""
    from mallory_orchestrator import crawler_client, orchestrate
    monkeypatch.setattr(daily, "has_due", lambda _db, now=None: due)
    calls = []
    monkeypatch.setattr(crawler_client, "dispatch_batch",
                        lambda payload, timeout=None, base=None:
                        calls.append(base) or {"jobs": len(payload["jobs"]), "results": []})
    out = orchestrate._dispatch_routed(db, chunk, timeout=5)
    return calls, out


def test_catalogue_work_is_held_off_the_lane_while_news_is_due(monkeypatch, db):
    _env(monkeypatch)
    # two domains pinned to lane instance 1, one to non-lane instance 7
    db.add_all([DomainShard(domain="a.example", instance=1),
                DomainShard(domain="b.example", instance=7)])
    db.commit()
    chunk = [{"seed_urls": ["https://a.example/"]}, {"seed_urls": ["https://b.example/"]}]
    calls, out = _routed(db, monkeypatch, chunk, due=True)
    assert calls == ["http://c7:8099"]              # instance 1 held for the lane
    assert out["held_for_daily"] == [1]


def test_the_lane_s_instances_rejoin_the_backlog_once_the_list_is_clear(monkeypatch, db):
    _env(monkeypatch)
    db.add_all([DomainShard(domain="a.example", instance=1),
                DomainShard(domain="b.example", instance=7)])
    db.commit()
    chunk = [{"seed_urls": ["https://a.example/"]}, {"seed_urls": ["https://b.example/"]}]
    calls, out = _routed(db, monkeypatch, chunk, due=False)
    assert sorted(calls) == ["http://c1:8099", "http://c7:8099"]
    assert out.get("held_for_daily", []) == []


def test_the_catalogue_never_stops_entirely_for_the_lane(monkeypatch, db):
    """If every bucket in the chunk belongs to the lane, holding them all would make the crawler
    silently do nothing. It must dispatch anyway."""
    _env(monkeypatch)
    db.add_all([DomainShard(domain="a.example", instance=0),
                DomainShard(domain="b.example", instance=2)])
    db.commit()
    chunk = [{"seed_urls": ["https://a.example/"]}, {"seed_urls": ["https://b.example/"]}]
    calls, out = _routed(db, monkeypatch, chunk, due=True)
    assert sorted(calls) == ["http://c0:8099", "http://c2:8099"]
    assert out.get("held_for_daily", []) == []


def test_exclusive_mode_does_not_hold_anything(monkeypatch, db):
    """Nothing to hold: shard.allowed_instances already keeps catalogue domains off the lane."""
    _env(monkeypatch, exclusive="1")
    db.add_all([DomainShard(domain="a.example", instance=1),
                DomainShard(domain="b.example", instance=7)])
    db.commit()
    chunk = [{"seed_urls": ["https://a.example/"]}, {"seed_urls": ["https://b.example/"]}]
    calls, out = _routed(db, monkeypatch, chunk, due=True)
    assert sorted(calls) == ["http://c1:8099", "http://c7:8099"]
    assert out.get("held_for_daily", []) == []


def test_a_broken_lane_can_never_stop_the_catalogue(monkeypatch, db):
    _env(monkeypatch)
    db.add(DomainShard(domain="b.example", instance=7))
    db.commit()

    def boom(_db, now=None):
        raise RuntimeError("daily table missing")

    from mallory_orchestrator import crawler_client, orchestrate
    monkeypatch.setattr(daily, "has_due", boom)
    calls = []
    monkeypatch.setattr(crawler_client, "dispatch_batch",
                        lambda payload, timeout=None, base=None:
                        calls.append(base) or {"jobs": 1, "results": []})
    orchestrate._dispatch_routed(db, [{"seed_urls": ["https://b.example/"]}], timeout=5)
    assert calls == ["http://c7:8099"]


# ── routing: a site goes to the instance that OWNS it ─────────────────────────────────────
#
# The regression these lock down was found in production, not here. On the lane's first live
# dispatch, infodefensa.com — shard owner instance 7 — was sent to instance 1 because 1 happened to
# be free. Instance 1 held no crawl history for it, so `incremental` had nothing to answer 304 with,
# the crawl became a cold walk from the seed, and it blew the 3600s dispatch timeout while still
# running. Only 10 of the 37 daily domains were owned by a lane instance at all, and because the
# free instance is picked per tick, a site never settled anywhere long enough to build history.

def test_a_site_goes_to_the_instance_that_owns_it(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 1, lane=(2,))                      # the one site is owned by instance 2
    seen = []
    sent = daily.run_due(db, now=NOW,
                         dispatch=lambda job, base: seen.append(base) or {},
                         busy_check=lambda i, b: False)   # 0 and 1 are free and must NOT be used
    assert sent == 1
    assert seen == ["http://c2:8099"]


def test_a_site_whose_owner_is_busy_waits_rather_than_going_elsewhere(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 1, lane=(2,))
    before = [r.next_run for r in db.query(DailyUrl).all()]
    seen = []
    sent = daily.run_due(db, now=NOW,
                         dispatch=lambda job, base: seen.append(base) or {},
                         busy_check=lambda i, b: i == 2)  # its owner is mid-crawl; 0 and 1 are free
    assert sent == 0 and seen == []
    db.expire_all()
    # Waiting, not lost: it fires on the first tick its owner is free. This is also what stops a
    # still-running daily crawl being dispatched a second time when its retry comes round.
    assert [r.next_run for r in db.query(DailyUrl).all()] == before


def test_a_site_with_no_shard_row_is_claimed_by_the_lane_not_guessed(monkeypatch, db):
    """Supersedes an earlier assertion that such a site is not dispatched at all.

    That was right while the pin ran only at startup: with no shard row there was no owner, and
    picking one would have been a guess. Now the tick re-pins first, so a site with no row is
    CLAIMED by the lane — which is what a newly added line in daily_urls.txt has to do, or it would
    sit undispatchable for ever. The invariant that actually matters is unchanged and asserted
    below: it never goes to an instance outside the lane.
    """
    _env(monkeypatch)
    _sites(db, 1, lane=None)                      # DailyUrl, but nobody owns the domain yet
    seen = []
    sent = daily.run_due(db, now=NOW, dispatch=lambda job, base: seen.append(base) or {},
                         busy_check=lambda i, b: False, preempt=False)
    assert sent == 1
    assert seen[0] in ("http://c0:8099", "http://c1:8099", "http://c2:8099")
    assert db.get(DomainShard, "n0.example").instance in (0, 1, 2)


def test_a_row_with_no_domain_is_still_never_dispatched(monkeypatch, db):
    """The guess-guard that survives: nothing to pin means nothing to own means nothing to send."""
    _env(monkeypatch)
    db.add(DailyUrl(url="https://x/", domain="", enabled=True, next_run=NOW, created_at=NOW))
    db.commit()
    assert daily.run_due(db, now=NOW, dispatch=lambda job, base: {},
                         busy_check=lambda i, b: False, preempt=False) == 0


def test_pin_to_lane_repoints_a_domain_owned_outside_the_lane(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 1, lane=(7,))                      # owned by an instance the lane does not run on
    res = daily.pin_to_lane(db)
    assert res["moved"] == 1 and res["kept"] == 0
    assert db.get(DomainShard, "n0.example").instance in (0, 1, 2)


def test_pin_to_lane_is_stable_and_leaves_a_domain_already_in_the_lane_alone(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 3)                                 # already round-robined onto 0, 1, 2
    before = {d: i for d, i in db.query(DomainShard.domain, DomainShard.instance)}
    res = daily.pin_to_lane(db)
    assert res["moved"] == 0 and res["kept"] == 3
    db.expire_all()
    after = {d: i for d, i in db.query(DomainShard.domain, DomainShard.instance)}
    assert after == before                        # a stable pin is what makes incremental work


def test_pin_to_lane_spreads_rather_than_stacking_one_instance(monkeypatch, db):
    _env(monkeypatch)
    _sites(db, 30, lane=(7,))                     # all owned off-lane, all must be re-pointed
    daily.pin_to_lane(db)
    db.expire_all()
    used = {i for (i,) in db.query(DomainShard.instance).all()}
    assert used == {0, 1, 2}                      # a hash that stacked them would defeat the lane


def test_pin_to_lane_no_ops_without_a_lane(monkeypatch, db):
    _env(monkeypatch, instances="")
    _sites(db, 2, lane=(7,))
    assert daily.pin_to_lane(db)["moved"] == 0
    assert db.get(DomainShard, "n0.example").instance == 7   # untouched, not re-pointed to 0


# ── the hold, on the path the SCHEDULER actually uses ─────────────────────────────────────
#
# Found live, and only live. The lane's hold was written into orchestrate._dispatch_routed, which
# is the MANUAL BATCH path; the recurring scheduler dispatches through scheduler._dispatch_fleet
# and never touches it. So the hold existed, had tests, and did nothing where it mattered: instance
# 0 finished a daily site at 11:27 and was immediately handed cat_gov_portal_darpa while 36 daily
# sites sat due. These drive _dispatch_fleet directly, because that is the function that decides.

def _sched_job(db: Session, domain: str, inst: int, now=NOW):
    """A due catalogue job whose domain is OWNED by `inst`."""
    from mallory_orchestrator.models import ScheduledJob
    db.add(DomainShard(domain=domain, instance=inst))
    sj = ScheduledJob(name=domain, job={"job_id": "cat_" + domain.replace(".", "_"),
                                        "seed_urls": ["https://%s/" % domain]},
                      job_type="news", frequency="daily", next_run=now,
                      enabled=True, created_at=now)
    db.add(sj)
    db.commit()
    return sj


# _dispatch_fleet is the fleet-aware dispatcher. It does not exist in every tree this file is run
# against, so these skip rather than fail there — a missing function is a different deployment, not
# a broken hold, and reporting it as a failure would train someone to ignore a real one.
_HAS_FLEET = hasattr(scheduler, "_dispatch_fleet")
fleet_only = pytest.mark.skipif(not _HAS_FLEET, reason="scheduler has no _dispatch_fleet")


def _fleet(monkeypatch, db, *, daily_due: bool, free: list[int]):
    """Run one _dispatch_fleet tick with a known free-list; returns the job ids dispatched."""
    _env(monkeypatch)
    # _dispatch_fleet opens its OWN session per instance (`from .db import SessionLocal`), which
    # points at DATABASE_URL, not the fixture's file. Without this the workers query a database
    # that has no tables and every dispatch is swallowed by the per-instance except-log.
    from mallory_orchestrator import db as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal",
                        sessionmaker(bind=db.get_bind(), expire_on_commit=False))

    # _dispatch_fleet is FIRE-AND-RETURN: it submits to a module-level thread pool and returns
    # without waiting, because in production a dispatch is a whole crawl. Run those submissions
    # inline instead — a sleep here would be a race dressed up as a test.
    class _Inline:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)

    monkeypatch.setattr(scheduler, "_POOL", _Inline())
    scheduler._INFLIGHT.clear()   # module-level; a leak from an earlier test would mask a dispatch
    if daily_due:
        _sites(db, 1, prefix="d")             # one due daily site => has_due() is True
    sj = _sched_job(db, "cat0.example", 0)    # catalogue work owned by LANE instance 0
    _sched_job(db, "cat5.example", 5)         # and by non-lane instance 5
    monkeypatch.setattr(scheduler, "free_instances", lambda prefer=None: list(free))
    seen = []
    scheduler._dispatch_fleet(db, db.query(type(sj)).all(), NOW,
                              lambda job, **kw: seen.append(job["job_id"]) or {})
    return seen


@fleet_only
def test_the_scheduler_holds_lane_instances_while_daily_work_is_due(monkeypatch, db):
    sent = _fleet(monkeypatch, db, daily_due=True, free=[0, 5])
    # instance 0 is the lane's; its catalogue job must NOT go out while news is due.
    assert "cat_cat0_example" not in sent
    assert "cat_cat5_example" in sent          # the rest of the fleet keeps the backlog moving


@fleet_only
def test_the_lane_instances_take_catalogue_work_once_nothing_is_due(monkeypatch, db):
    sent = _fleet(monkeypatch, db, daily_due=False, free=[0, 5])
    assert "cat_cat0_example" in sent          # priority, not exclusive: they rejoin the fleet
    assert "cat_cat5_example" in sent


@fleet_only
def test_holding_every_free_instance_costs_one_tick_not_the_catalogue(monkeypatch, db):
    # Only lane instances are free. This tick dispatches no catalogue work — deliberately: the
    # nine other instances free up on later ticks, and the alternative is a lane that can only get
    # an instance by winning a race against a tick three times faster than its own.
    sent = _fleet(monkeypatch, db, daily_due=True, free=[0, 1, 2])
    assert sent == []


# ── the hold, on the AUTO-REFILL path ─────────────────────────────────────────────────────
#
# The third dispatch path, and the one that would have silently undone the other two: the refill
# watcher resumes a saved frontier on any idle instance within a minute, so a lane instance that
# finished its catalogue work would be handed straight back to the catalogue while the daily lane —
# which ticks no faster — raced it. A hold covering two paths out of three is not a hold.

# Same reason as fleet_only: the refill hold binds against the SCHEDULER's concurrency cap, which
# does not exist in every tree this file runs against.
refill_only = pytest.mark.skipif(not hasattr(scheduler, "MAX_BUSY_INSTANCES"),
                                 reason="scheduler has no MAX_BUSY_INSTANCES")


def _refill(monkeypatch, db, *, daily_due: bool, idle: set[int], force: bool = False):
    """Run refill_once against a fake fleet; returns the instances it resumed work on."""
    _env(monkeypatch)
    if daily_due:
        _sites(db, 1, prefix="r")
    from mallory_orchestrator import crawler_client, refill
    from mallory_orchestrator import db as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal",
                        sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    bases = ["http://c%d:8099" % i for i in range(12)]
    idx = {b: i for i, b in enumerate(bases)}
    monkeypatch.setattr(crawler_client, "pool", lambda: bases)
    monkeypatch.setattr(crawler_client, "batch_status",
                        lambda base=None: {"running": idx[base] not in idle})
    # Catalogue ids (recrawl_): the resume lane is scoped by OWNER (test_control_plane.py), and a
    # bare id nobody produced counts as campaign work and is parked in normal mode. These cases
    # are about the lane hold, so every frontier here is the catalogue's own.
    monkeypatch.setattr(crawler_client, "resumable",
                        lambda base=None: {"jobs": [{"job_id": "recrawl_j%d" % idx[base],
                                                     "has_config": True, "pending": 10}]})
    resumed: list[int] = []
    monkeypatch.setattr(crawler_client, "resume",
                        lambda ids, base=None, **kw: resumed.append(idx[base]) or {"resumed": ids})
    # The cap lives on the SCHEDULER and binds here too; zero disables it, so the only thing
    # that can exclude an instance in these tests is the hold under test.
    monkeypatch.setattr(scheduler, "MAX_BUSY_INSTANCES", 0)
    refill.refill_once(force=force)
    return sorted(resumed)


@refill_only
def test_auto_refill_leaves_the_lane_alone_while_news_is_due(monkeypatch, db):
    got = _refill(monkeypatch, db, daily_due=True, idle={0, 2, 7})
    assert got == [7]          # 0 and 2 are the lane's; refilling them is what starves the lane


@refill_only
def test_auto_refill_takes_the_lane_back_once_nothing_is_due(monkeypatch, db):
    got = _refill(monkeypatch, db, daily_due=False, idle={0, 2, 7})
    assert got == [0, 2, 7]    # priority, not exclusive: idle is still waste when no news is due


@refill_only
def test_force_does_not_lift_the_hold_because_the_autopilot_presses_that_button(monkeypatch, db):
    # `force` overrides a stale stop_pending — a dead flag. It must not override a live
    # reservation, because api.crawler_fleet_assign calls refill with force=True and the AUTOPILOT
    # calls that on a 900s timer. Measured: 17 catalogue jobs resumed onto a lane instance minutes
    # after being stopped for the sweep, and again every quarter hour. A carve-out an automation
    # walks through is a hole, not a carve-out.
    got = _refill(monkeypatch, db, daily_due=True, idle={0, 2, 7}, force=True)
    assert got == [7]


# ── defence first, always: auto-preempt ───────────────────────────────────────────────────
#
# Holding new catalogue work off the lane was not enough. A crawl already in flight is never
# interrupted, so the first sweep queued behind an 18-job batch on one instance and a six-hour
# gov-portal walk on another, and only moved once those were stopped BY HAND. Stopping is graceful —
# verified on the live fleet, 19 frontiers survived a stop intact and came back resumable — which is
# the only reason it is allowed to happen automatically.

def _preempt(monkeypatch, db, *, owner: int, running: str | None, stop_pending: bool = False,
             category_off: str | None = None):
    """One run_due tick; returns the lane instances whose batch was stopped."""
    _env(monkeypatch)
    _sites(db, 1, lane=(owner,))
    if category_off:
        scheduler.set_category(db, category_off, False)
        db.commit()
    bases = {"http://c%d:8099" % i: i for i in range(12)}
    stopped: list[int] = []
    daily.run_due(
        db, now=NOW,
        dispatch=lambda job, base: {},
        busy_check=lambda i, b: running is not None and i == owner,
        preempt_status=lambda base: ({"running": True, "current_job": running,
                                      "stop_pending": stop_pending}
                                     if bases[base] == owner and running else {"running": False}),
        preempt_stop=lambda base: stopped.append(bases[base]) or {"stopping": True},
    )
    return stopped


def test_catalogue_work_is_stopped_when_a_due_defence_site_owns_the_instance(monkeypatch, db):
    assert _preempt(monkeypatch, db, owner=2, running="cat_gov_portal_homeoffice") == [2]


def test_the_lane_never_stops_its_own_crawl(monkeypatch, db):
    # Preempting a daily job is the lane eating itself: it would stop a defence crawl to start
    # another defence crawl, and neither would finish.
    assert _preempt(monkeypatch, db, owner=2, running="daily_rusi_org") == []


def test_an_instance_already_draining_is_not_stopped_again(monkeypatch, db):
    # A 60s tick would re-issue the stop every minute and the batch would never tear down.
    assert _preempt(monkeypatch, db, owner=2, running="cat_x", stop_pending=True) == []


def test_a_free_instance_is_not_stopped(monkeypatch, db):
    assert _preempt(monkeypatch, db, owner=2, running=None) == []


def test_nothing_is_stopped_for_a_site_a_category_switch_is_holding(monkeypatch, db):
    # The switch filter runs BEFORE the preempt, so a held site never costs an instance its crawl.
    from mallory_orchestrator.models import Source
    db.add(Source(domain="n0.example", source_id="n0.example", category="general",
                  tier=2, created_at=NOW))
    db.commit()
    assert _preempt(monkeypatch, db, owner=2, running="cat_x", category_off="general") == []


def test_an_instance_no_due_site_is_waiting_for_is_left_alone(monkeypatch, db):
    # A stop has a real cost; paying it for an instance nothing is waiting on is worse than waiting.
    _env(monkeypatch)
    _sites(db, 1, lane=(0,))
    bases = {"http://c%d:8099" % i: i for i in range(12)}
    stopped: list[int] = []
    daily.run_due(db, now=NOW, dispatch=lambda job, base: {},
                  busy_check=lambda i, b: True,
                  preempt_status=lambda base: {"running": True, "current_job": "cat_y"},
                  preempt_stop=lambda base: stopped.append(bases[base]) or {})
    assert stopped == [0]          # only the OWNER of the due site, not all three


def test_a_domain_that_drifted_off_the_lane_is_repinned_and_still_runs(monkeypatch, db):
    """The pin is not a one-off migration — something keeps undoing it.

    shard.rebalance re-hashes finished domains across the whole fleet, and api.crawler_fleet_assign
    calls it whenever an instance is idle-and-empty, which the autopilot does on a timer. Measured
    after ~12 hours live: 22 of 37 daily domains had drifted onto instances 3-11, and since a site
    only ever goes to its owner, all 22 were UNDISPATCHABLE. 18 of the 25 sites then due could not
    run at all while two lane instances sat idle. The lane looked slow; it was starved.
    """
    _env(monkeypatch)
    _sites(db, 1, lane=(9,))            # drifted well off the lane, as rebalance would leave it
    # MOVING a domain is the `repin` toggle (policy.AUTOS), off by default like every other
    # timer-driven mover; with it off the tick reports the drift instead (test_control_plane.py).
    from mallory_orchestrator import policy
    policy.set_auto(db, "repin", True)
    seen = []
    sent = daily.run_due(db, now=NOW,
                         dispatch=lambda job, base: seen.append(base) or {},
                         busy_check=lambda i, b: False, preempt=False)
    assert sent == 1, "a drifted domain must be re-pinned by the tick, not left undispatchable"
    assert seen and seen[0] in ("http://c0:8099", "http://c1:8099", "http://c2:8099")
    db.expire_all()
    assert db.get(DomainShard, "n0.example").instance in (0, 1, 2)
