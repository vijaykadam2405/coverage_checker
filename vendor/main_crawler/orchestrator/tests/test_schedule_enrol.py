"""Putting FINISHED sites onto a cadence, and editing a cadence in place.

Three things here are worth a test:

1. **"Complete" must mean complete.** A host with stored pages but NO crawl report has not been
   shown to have finished — we only know pages exist. Enrolling it would put a half-crawled site on
   a weekly re-crawl and quietly call it done. `unresolved == 0` and `unresolved is None` are the
   two states that must not be confused, which is the same rule the whole dashboard runs on.

2. **Idempotence.** The button is meant to be pressed after every batch, so a second press must
   enrol nothing new rather than creating a duplicate schedule per press.

3. **Editing a cadence re-derives next_run.** Leaving the old next_run keeps a site on its previous
   rhythm for one more cycle, which reads as the change not having applied.

Runnable directly:  python tests/test_schedule_enrol.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api, scheduler  # noqa: E402
from mallory_orchestrator.models import ScheduledJob  # noqa: E402


def _jobs(db):
    """Only the ScheduledJob rows. scheduler.log_event() writes a ScheduleLog into the same session,
    so counting everything in the fake DB counts audit entries as if they were schedules."""
    return [r for r in db.rows if isinstance(r, ScheduledJob)]


class FakeDB:
    """Just enough Session for these endpoints: add / scalars / get / commit / refresh."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.committed = 0
        self._next_id = 1

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1
        self.rows.append(obj)

    def scalars(self, *_a, **_k):
        # Models `select(ScheduledJob)`: real SQLAlchemy returns only that entity's rows. Returning
        # everything made the fake hand back the ScheduleLog audit entries that log_event() writes
        # into the same session, which is a defect in the fake, not in the endpoint.
        rows = [r for r in self.rows if isinstance(r, ScheduledJob)]

        class R:
            def all(self_inner):
                return rows

            def first(self_inner):
                # patch_schedule reaches scheduler.policy_for, which does .scalars(...).first() to
                # find the site's cadence policy. No policy is a legitimate answer (None), and it is
                # the one this fake models — the tests here are about the edit, not the policy.
                return rows[0] if rows else None
        return R()

    def get(self, _model, sid):
        return next((r for r in self.rows if getattr(r, "id", None) == sid), None)

    def commit(self):
        self.committed += 1

    def refresh(self, _obj):
        pass

    def flush(self):
        pass


def _sched(host, freq="weekly", sid=1):
    return ScheduledJob(
        id=sid, name=host, job={"job_id": f"recrawl_{host}", "job_type": "manufacturer_ir",
                                "seed_urls": [f"https://{host}/"], "source_id": host},
        job_type="manufacturer_ir", frequency=freq, retry_count=1, retries_left=1,
        start_at=None, next_run=None, last_run_at=None, last_status="pending",
        enabled=True, created_at=scheduler.utcnow())


SITES = [
    # finished: pages stored AND nothing unresolved
    {"host": "alpha.com", "stored_pages": 400, "unresolved": 0, "category": "manufacturer_ir"},
    {"host": "beta.com", "stored_pages": 90, "unresolved": 0, "category": "manufacturer_ir"},
    # still has unresolved URLs — NOT finished
    {"host": "gamma.com", "stored_pages": 300, "unresolved": 17, "category": "manufacturer_ir"},
    # pages exist but no crawl report at all (unresolved is None) — unknown, not finished
    {"host": "delta.com", "stored_pages": 55, "unresolved": None, "category": "manufacturer_ir"},
    # a report saying zero pages stored — nothing to re-crawl on a cadence
    {"host": "epsilon.com", "stored_pages": 0, "unresolved": 0, "category": "manufacturer_ir"},
]


def _patch_logs(rows):
    api.logs_sites = lambda limit=5000, db=None: {"rows": rows}       # noqa: ARG005


def test_only_verified_complete_sites_are_enrolled() -> None:
    _patch_logs(SITES)
    db = FakeDB()
    out = api.enrol_completed(api.EnrolCompleted(frequency="weekly"), db=db)

    got = sorted(r.name for r in _jobs(db))
    assert got == ["alpha.com", "beta.com"], got
    assert out["enrolled"] == 2
    # the three excluded ones are counted, not silently dropped
    assert out["not_complete"] == 3, out
    assert all(r.frequency == "weekly" and r.enabled for r in _jobs(db))
    assert all(r.next_run is not None for r in _jobs(db)), "an enrolled job that never fires is a no-op"


def test_pressing_twice_enrols_nothing_new() -> None:
    _patch_logs(SITES)
    db = FakeDB()
    api.enrol_completed(api.EnrolCompleted(frequency="weekly"), db=db)
    n_after_first = len(_jobs(db))
    out = api.enrol_completed(api.EnrolCompleted(frequency="weekly"), db=db)
    assert len(_jobs(db)) == n_after_first, "a second press duplicated the schedule"
    assert out["enrolled"] == 0 and out["already_scheduled"] == 2, out


def test_www_and_apex_are_the_same_site() -> None:
    """The crawl log spells a host as fetched (www.alpha.com); the seed spells it bare. Treating
    those as two sites would schedule the same crawl twice."""
    _patch_logs([{"host": "www.alpha.com", "stored_pages": 400, "unresolved": 0,
                  "category": "manufacturer_ir"}])
    db = FakeDB([_sched("alpha.com")])
    out = api.enrol_completed(api.EnrolCompleted(frequency="weekly"), db=db)
    assert out["enrolled"] == 0 and out["already_scheduled"] == 1, out


def test_editing_cadence_rederives_next_run() -> None:
    sj = _sched("alpha.com", freq="weekly")
    sj.last_run_at = scheduler.utcnow() - dt.timedelta(days=1)
    sj.next_run = sj.last_run_at + dt.timedelta(days=7)
    db = FakeDB([sj])
    before = sj.next_run

    api.patch_schedule(1, api.SchedulePatch(frequency="daily"), db=db)
    assert sj.frequency == "daily"
    assert sj.next_run != before, "next_run still on the OLD cadence — the change looks unapplied"
    assert sj.next_run < before, "a faster cadence must fire sooner, not later"


def test_editing_rejects_a_frequency_that_is_not_real() -> None:
    db = FakeDB([_sched("alpha.com")])
    try:
        api.patch_schedule(1, api.SchedulePatch(frequency="fortnightly"), db=db)
    except Exception as e:      # HTTPException
        assert "frequency" in str(e)
    else:
        raise AssertionError("accepted a cadence the scheduler cannot compute")


def test_max_pages_edit_reassigns_the_json_column() -> None:
    """SQLAlchemy does not track in-place mutation of a JSON column, so an in-place edit commits
    nothing and the cap silently stays where it was."""
    sj = _sched("alpha.com")
    original = sj.job
    db = FakeDB([sj])
    api.patch_schedule(1, api.SchedulePatch(max_pages=50), db=db)
    assert sj.job["max_pages"] == 50
    assert sj.job is not original, "job dict was mutated in place — the change will not persist"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all good")
