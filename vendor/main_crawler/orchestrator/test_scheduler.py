"""Scheduler self-check — pure sync layer, injected dispatch stub, no HTTP/asyncio/crawler.

Run:  .venv/Scripts/python.exe test_scheduler.py     (from the orchestrator/ dir)
"""
import datetime as dt
import os
import pathlib
import sys

# Point the package at a throwaway DB BEFORE importing it (config/db read env at import).
os.environ["DATABASE_URL"] = "sqlite:///./test_sched.db"
_dbfile = pathlib.Path("test_sched.db")
if _dbfile.exists():
    _dbfile.unlink()
sys.path.insert(0, "src")

from mallory_orchestrator import scheduler as sch  # noqa: E402
from mallory_orchestrator.db import Base, SessionLocal, engine  # noqa: E402
from mallory_orchestrator.models import (JobRun, ScheduledJob, ScheduleLog,  # noqa: E402
                                         Setting, SiteSchedule)

Base.metadata.create_all(engine)


def _reset(db):
    for m in (JobRun, ScheduleLog, ScheduledJob):
        db.query(m).delete()
    db.commit()
    # Arm the master switch. run_due() now re-reads it before EVERY dispatch (it blocks for a whole
    # crawl per job, so the flag the tick was admitted on goes stale), and an absent Setting row
    # reads as off — so without this every dispatch test would silently assert on a halted pipeline.
    # t_master_gate flips it deliberately and is unaffected.
    sch.set_master(db, True)


def _clear_categories(db):
    """Setting rows survive _reset (they are app state, not run state) — clear the category
    switches so one test's disabled category can't silently hold another test's jobs."""
    db.query(Setting).filter(Setting.key.like("category_off:%")).delete(synchronize_session=False)
    db.query(SiteSchedule).delete()
    db.commit()


def _mk(db, **kw):
    now = sch.utcnow()
    d = dict(name="t", job={"job_id": "j1", "job_type": "news", "seed_urls": ["https://x"]},
             job_type="news", frequency="once", retry_count=0, retries_left=0, start_at=None,
             next_run=now - dt.timedelta(seconds=1), last_status="pending", enabled=True,
             created_at=now)
    d.update(kw)
    sj = ScheduledJob(**d)
    db.add(sj)
    db.commit()
    db.refresh(sj)
    return sj


def ok(job):
    return {"summary": {"fetched": 3, "kept": 2, "records_emitted": 1}}


def boom(job):
    raise RuntimeError("boom")


FREE = lambda: False   # noqa: E731
BUSY = lambda: True    # noqa: E731


def t_compute_next_run():
    now = dt.datetime(2026, 7, 17, 9, 3, 0)
    assert sch.compute_next_run("once", now, now) is None
    prev = dt.datetime(2026, 7, 17, 9, 0, 0)
    assert sch.compute_next_run("daily", prev, now) == dt.datetime(2026, 7, 18, 9, 0, 0)
    # missed windows collapse to a single next firing, phase preserved
    prev2 = dt.datetime(2026, 7, 14, 9, 0, 0)
    n2 = sch.compute_next_run("daily", prev2, now)
    assert n2 > now and (n2 - now).total_seconds() <= 86400 and (n2 - prev2).total_seconds() % 86400 == 0
    assert sch.compute_next_run("6h", dt.datetime(2026, 7, 17, 0, 0, 0),
                                dt.datetime(2026, 7, 17, 1, 0, 0)) == dt.datetime(2026, 7, 17, 6, 0, 0)
    assert sch.compute_next_run("weekly", prev, now) == prev + dt.timedelta(days=7)


def t_fire_once():
    with SessionLocal() as db:
        _reset(db)
        sj = _mk(db, frequency="once")
        assert sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) == 1
        db.refresh(sj)
        assert sj.enabled is False and sj.next_run is None and sj.last_status == "done"
        assert db.query(JobRun).filter_by(status="ok").count() == 1
        ev = {r.event for r in db.query(ScheduleLog).all()}
        assert {"dispatched", "completed"} <= ev


def t_daily_advance():
    with SessionLocal() as db:
        _reset(db)
        now = sch.utcnow()
        sj = _mk(db, frequency="daily", next_run=now - dt.timedelta(seconds=1))
        old = sj.next_run
        sch.run_due(db, now=now, dispatch=ok, busy_check=FREE)
        db.refresh(sj)
        assert sj.enabled is True
        assert abs((sj.next_run - (old + dt.timedelta(days=1))).total_seconds()) < 2


def t_master_gate():
    with SessionLocal() as db:
        _reset(db)
        sch.set_master(db, False)
        assert sch.master_enabled(db) is False
        sj = _mk(db, frequency="once")
        fired = sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) if sch.master_enabled(db) else 0
        assert fired == 0
        db.refresh(sj)
        assert sj.last_status == "pending"
        sch.set_master(db, True)
        fired2 = sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) if sch.master_enabled(db) else 0
        assert fired2 == 1


def t_retry_backoff():
    with SessionLocal() as db:
        _reset(db)
        now = sch.utcnow()
        sj = _mk(db, frequency="once", retry_count=2, retries_left=2, next_run=now - dt.timedelta(seconds=1))
        sch.run_due(db, now=now, dispatch=boom, busy_check=FREE)
        db.refresh(sj)
        assert sj.retries_left == 1 and sj.last_status == "retrying"
        assert abs((sj.next_run - (now + dt.timedelta(seconds=60))).total_seconds()) < 2
        now2 = sj.next_run + dt.timedelta(seconds=1)
        sch.run_due(db, now=now2, dispatch=boom, busy_check=FREE)
        db.refresh(sj)
        assert sj.retries_left == 0 and sj.last_status == "retrying"
        assert abs((sj.next_run - (now2 + dt.timedelta(seconds=120))).total_seconds()) < 2
        now3 = sj.next_run + dt.timedelta(seconds=1)
        sch.run_due(db, now=now3, dispatch=boom, busy_check=FREE)
        db.refresh(sj)
        assert sj.enabled is False and sj.last_status == "error" and sj.next_run is None
        assert db.query(JobRun).filter_by(status="error").count() == 3
        ev = [r.event for r in db.query(ScheduleLog).all()]
        assert ev.count("retry") == 2 and ev.count("failed") == 1


def t_busy_skip():
    with SessionLocal() as db:
        _reset(db)
        now = sch.utcnow()
        sj = _mk(db, frequency="once", next_run=now - dt.timedelta(seconds=1))
        old = sj.next_run
        assert sch.run_due(db, now=now, dispatch=ok, busy_check=BUSY) == 0
        db.refresh(sj)
        assert sj.next_run == old and sj.last_status == "pending"
        assert any(r.event == "skipped_busy" for r in db.query(ScheduleLog).all())


def t_start_at_respected():
    with SessionLocal() as db:
        _reset(db)
        now = sch.utcnow()
        future = now + dt.timedelta(hours=1)
        sj = _mk(db, frequency="once", start_at=future, next_run=future)
        assert sch.run_due(db, now=now, dispatch=ok, busy_check=FREE) == 0
        db.refresh(sj)
        assert sj.last_status == "pending"


def t_category_gate_blocks_dispatch():
    """Turning a category off must stop dispatch WITHOUT burning the job's window — re-enabling
    fires it on the next tick, not at some future next_run."""
    with SessionLocal() as db:
        _reset(db)
        _clear_categories(db)
        sj = _mk(db, frequency="once", job_type="news")
        due_at = sj.next_run
        sch.set_category(db, "news", False)
        assert sch.category_enabled(db, "news") is False
        assert sch.disabled_categories(db) == {"news"}

        assert sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) == 0
        db.refresh(sj)
        assert sj.last_status == "pending" and sj.next_run == due_at   # still due, untouched
        assert any(r.event == "skipped_category" for r in db.query(ScheduleLog).all())

        sch.set_category(db, "news", True)
        assert sch.category_enabled(db, "news") is True
        assert sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) == 1
        db.refresh(sj)
        assert sj.last_status == "done"
        _clear_categories(db)


def t_new_data_flag():
    """A re-crawl must say WHETHER anything new landed; the first crawl of a site must not
    (everything is new by definition, so a NEW DATA badge there is meaningless)."""
    def nothing(job):
        return {"summary": {"fetched": 5, "kept": 0, "skipped_unchanged": 5}}

    with SessionLocal() as db:
        _reset(db)
        _clear_categories(db)
        _mk(db, frequency="once")                                   # ok() ⇒ fetched=3 kept=2
        assert sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) == 1
        ev = {r.event for r in db.query(ScheduleLog).all()}
        assert "new_data" in ev and "no_new_data" not in ev, ev
        comp = db.query(ScheduleLog).filter_by(event="completed").first()
        assert "cycle ok" in comp.detail and "stored=" in comp.detail, comp.detail

        _reset(db)
        _mk(db, frequency="once")
        assert sch.run_due(db, now=sch.utcnow(), dispatch=nothing, busy_check=FREE) == 1
        ev = {r.event for r in db.query(ScheduleLog).all()}
        assert "no_new_data" in ev and "new_data" not in ev, ev

        # baseline phase ⇒ labelled `baseline`, never new_data
        _reset(db)
        _clear_categories(db)
        db.add(SiteSchedule(domain="x.test", source_id="SRC1", phase="baseline", tier="weekly"))
        db.commit()
        _mk(db, frequency="once",
            job={"job_id": "j1", "job_type": "news", "source_id": "SRC1", "seed_urls": ["https://x"]})
        assert sch.run_due(db, now=sch.utcnow(), dispatch=ok, busy_check=FREE) == 1
        ev = {r.event for r in db.query(ScheduleLog).all()}
        assert "baseline" in ev and "new_data" not in ev, ev
        _clear_categories(db)


if __name__ == "__main__":
    tests = [t_compute_next_run, t_fire_once, t_daily_advance, t_master_gate,
             t_retry_backoff, t_busy_skip, t_start_at_respected,
             t_category_gate_blocks_dispatch, t_new_data_flag]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nALL {len(tests)} SCHEDULER TESTS PASSED")
    engine.dispose()
    if _dbfile.exists():
        _dbfile.unlink()
