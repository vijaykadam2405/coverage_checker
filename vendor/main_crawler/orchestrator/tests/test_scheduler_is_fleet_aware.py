"""The scheduler had a twelve-instance fleet and could see one instance of it.

Measured 2026-07-31, from the orchestrator's own schedule log, every twenty seconds for hours:

    skipped_busy   20 job(s) due; crawler busy
    skipped_busy   20 job(s) due; crawler busy
    ...

while five instances sat completely idle. Of 507 enrolled weekly re-crawls, **485 had never run
once**. Two independent causes, both the same mistake — a helper called with no `base`, which is
instance 0 and only instance 0:

  * `_crawler_busy()` asked instance 0 whether it was busy and let that answer speak for all twelve,
    so one busy container blocked the entire schedule;
  * `dispatch()` posted every job to instance 0 regardless of which instance owned the domain.

And a third, structural: dispatch BLOCKS for the whole crawl, so even once unblocked exactly one
scheduled re-crawl could be in flight fleet-wide. That is not a slow scheduler, it is an eleven-
twelfths idle fleet with a full queue — the exact symptom the operator kept seeing.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from mallory_orchestrator import scheduler as sch


class _Resp:
    def __init__(self, running):
        self.running = running


def _fanout(states: dict[int, bool | None]):
    """states: instance -> running (True/False), or None for 'did not answer'."""
    def _f(_fn, *a, **k):
        return [{"instance": i, "ok": r is not None,
                 "data": ({"running": r} if r is not None else None)}
                for i, r in sorted(states.items())]
    return _f


def test_one_busy_instance_does_not_speak_for_the_fleet(monkeypatch):
    """THE bug. Instance 0 crawling, eleven idle — and the whole schedule was skipped."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, **{i: False for i in range(1, 12)}}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 0)          # cap off; this is about the verdict
    assert sch._crawler_busy() is False
    assert sch.free_instances() == list(range(1, 12))


def test_the_fleet_concurrency_cap_is_enforced_by_trimming_the_list(monkeypatch):
    """Measured the day the scheduler learned to dispatch fleet-wide: 11-12 instances crawling put
    us at 38.3 of 40 cores (budget 30) across 884 Chromium processes — and throughput FELL from
    190 pages/min to 22. The extra concurrency bought negative work.

    Callers take one job per entry, so trimming the returned LIST is what enforces it; a check made
    after dispatch would be too late."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, 1: True, 2: True, **{i: False for i in range(3, 12)}}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert sch.free_instances() == [3, 4, 5, 6, 7]              # 3 busy + 5 = the cap of 8


def test_at_capacity_the_fleet_reports_no_room(monkeypatch):
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({i: i < 8 for i in range(12)}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert sch.free_instances() == []
    assert sch._crawler_busy() is True, "at capacity must read as busy, or the tick logs a skip"


def test_a_scarce_slot_goes_to_an_instance_that_HAS_work(monkeypatch):
    """Measured live, and it wasted every tick: 7 instances crawling under a cap of 8 left exactly
    ONE free slot. `idle[:room]` handed it to instance 5 by index order — which owned 0 of the 53
    due jobs — while instances 8-11 sat idle owning 25 of them. The tick dispatched nothing, and
    kept dispatching nothing for as long as that shape held.

    A cap applied before owners are known always cuts the wrong thing; this is the same bug as
    truncating the DUE list before the owner match, on the instance axis instead of the job axis.
    """
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({**{i: True for i in range(5)}, 5: False, 6: True,
                                 **{i: False for i in range(7, 12)}}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert sch.free_instances() == [5, 7]                    # index order — 5 owns nothing
    assert sch.free_instances(prefer={8, 9, 10, 11}) == [8, 9]


def test_preferring_never_exceeds_the_cap(monkeypatch):
    """The reordering must change WHICH instances are admitted, never HOW MANY — the cap is a
    measured CPU budget, not a hint."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, 1: True, **{i: False for i in range(2, 12)}}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert len(sch.free_instances(prefer=set(range(12)))) == 6   # 2 busy + 6 = the cap of 8


def test_an_empty_preference_behaves_exactly_as_before(monkeypatch):
    """Nothing due, or nothing shardable — the old ordering must be untouched."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, **{i: False for i in range(1, 12)}}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert sch.free_instances(prefer=set()) == sch.free_instances() == [1, 2, 3, 4, 5, 6, 7]


def test_a_preferred_instance_that_is_BUSY_is_still_not_free(monkeypatch):
    """Owning work does not make an instance available — dispatching into a running batch is
    refused and costs the job a retry backoff."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, 1: True, 2: False}))
    monkeypatch.setattr(sch, "MAX_BUSY_INSTANCES", 8)
    assert sch.free_instances(prefer={0, 1}) == [2]


def test_the_dispatcher_computes_owners_BEFORE_asking_which_are_free():
    """The ordering is the whole fix. If `free_instances()` is called first it cannot be told which
    instances have work, and the cap goes back to trimming by index."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert body.index("shard.instance_for") < body.index("free_instances("), \
        "the free list is computed before owners are known"
    assert "free_instances(prefer=" in body, "the dispatcher does not pass its owners to the cap"


def test_the_fleet_is_busy_only_when_every_instance_is(monkeypatch):
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({i: True for i in range(12)}))
    assert sch._crawler_busy() is True
    assert sch.free_instances() == []


def test_an_instance_that_did_not_answer_is_not_treated_as_free(monkeypatch):
    """Dispatching into a crawler we cannot see burns a retry and its backoff for nothing."""
    monkeypatch.setattr(sch.crawler_client, "fanout",
                        _fanout({0: True, 1: None, 2: False}))
    assert sch.free_instances() == [2]


def test_a_totally_unreachable_fleet_reads_as_busy(monkeypatch):
    """Better to hold the jobs due than to fire 20 dispatches at nothing."""
    monkeypatch.setattr(sch.crawler_client, "fanout", _fanout({0: None, 1: None}))
    assert sch._crawler_busy() is True


# ── routing and parallelism ───────────────────────────────────────────────────────────────────

def test_jobs_are_routed_to_the_instance_that_owns_the_domain():
    """A job's frontier and seen-set live inside ONE container. Sent anywhere else, the site
    restarts from its seed while its real progress sits unreachable next door."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "shard.instance_for" in body, "dispatch is not routed by domain shard"
    assert "crawler_client.dispatch(job, base=" in body, "dispatch still goes to the default base"


def test_one_job_per_free_instance_not_one_job_total():
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "ThreadPoolExecutor" in body, "dispatch is still serial across the fleet"
    # `setdefault` keeps the FIRST (highest-priority, the list is already ordered) job per owner and
    # silently drops the rest. Same guarantee the old `owner not in picked` check made — one
    # blocking job per instance — expressed as the dict operation that enforces it.
    assert "owner_of.setdefault(owner" in body, \
        "an instance could be given two blocking jobs at once"


def test_each_parallel_job_gets_its_own_session():
    """SQLAlchemy's Session is not thread-safe, and the failure mode is not an exception — it is two
    dispatches interleaving writes on the same rows."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "SessionLocal()" in body and "s.close()" in body


def test_the_per_tick_cap_does_not_truncate_before_the_owner_match():
    """Measured live: 507 jobs due, five instances free, and the tick dispatched NOTHING.

    `due` was cut to MAX_DISPATCH_PER_TICK (25) by priority BEFORE owners were matched, so if those
    25 happened to belong to busy instances — likely, at 12 instances and one owner per domain —
    every free container stayed idle, and stayed idle for as long as the priority order held. The
    fleet path must scan the whole ordered list; its real cap is one job per free instance.
    """
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def run_due")[1].split("\ndef ")[0]
    fleet_call = body.index("_dispatch_fleet(db, due")
    # the truncation may only appear inside the stubbed/serial branch, after the fleet call is set up
    assert "due = due[:max_dispatch]" not in body, "the fleet path still sees a truncated due list"
    assert "capped = due[:max_dispatch]" in body[:fleet_call], "the serial cap was lost entirely"


def test_a_job_whose_owner_is_busy_is_left_due_not_failed():
    """Dispatching it anyway means the crawler refuses ("a batch is already running"), the job takes
    a retry backoff, and the site is delayed for no reason."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "if not picked" in body and "return 0" in body
    assert "_on_failure" not in body, "an unroutable job is recorded as a failure"


# ── the sequential contract the existing suites depend on ─────────────────────────────────────

def test_injecting_dispatch_still_selects_the_sequential_path():
    """Every existing scheduler test drives run_due with a stub and asserts sequential behaviour —
    defence-first order, master switch honoured mid-tick, N counted. A stub has no fleet to fan out
    across, so it must not be sent down the parallel path."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def run_due")[1].split("\ndef ")[0]
    assert "stubbed = dispatch is not None" in body
    assert "if stubbed:" in body and "_dispatch_serial" in body


def test_the_sequential_path_still_rechecks_the_master_switch_per_job():
    """Preserved verbatim from the original loop: dispatch blocks for minutes to hours, so the flag
    the tick was admitted on is stale by the second job. Off must mean off."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_serial")[1].split("\ndef ")[0]
    assert "db.expire_all()" in body
    assert "master_enabled(db)" in body
    assert "skipped_master_off" in body


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


def test_the_tick_does_not_block_on_the_slowest_dispatch():
    """Measured on screen: 1 of 12 crawling with 483 sites due.

    `with ThreadPoolExecutor(...) as ex: ex.map(...)` waits for every dispatch in the batch, and a
    dispatch IS a whole crawl. One ten-minute site pinned the tick while the four instances that
    finished in ten seconds sat idle for the rest of it. The barrier synchronised nothing — the next
    tick re-reads which instances are free.
    """
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "_POOL.submit" in body, "dispatch still waits for the batch"
    # Comments quote the old blocking form on purpose — that is the explanation. Assert on CODE.
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "with ThreadPoolExecutor" not in code and "ex.map" not in code


def test_a_running_job_is_not_dispatched_twice():
    """due_jobs() selects on next_run, which is only advanced when a crawl SUCCEEDS. Without an
    in-flight guard the next tick re-picks a job that is still crawling and hands it to a second
    instance — two crawls of one site, and the loser is refused."""
    src = Path(sch.__file__).read_text(encoding="utf-8")
    body = src.split("def _dispatch_fleet")[1].split("\ndef ")[0]
    assert "_INFLIGHT" in body
    assert "_INFLIGHT.discard" in body, "a finished job never leaves the in-flight set"


# ── "reached nothing" is not a clean cycle ────────────────────────────────────────────────────

class _LogDB:
    def __init__(self):
        self.events = []

    def add(self, row):
        self.events.append((row.event, row.detail or ""))

    def commit(self):
        pass


def _cycle(**summary):
    db = _LogDB()
    sch.log_cycle(db, 1, "j1", summary, 12.3, baseline=summary.pop("_baseline", False))
    return dict(db.events)


def test_a_crawl_that_fetched_nothing_is_not_reported_as_ok():
    """92 catalogued sites sat at zero pages while every one of their first crawls logged
    `cycle ok · fetched=0 kept=0 stored=0` — indistinguishable from a re-crawl that fetched 400
    pages and found them all unchanged. A dead apex must not read green."""
    ev = _cycle(fetched=0, kept=0)
    assert "reached_nothing" in ev
    assert "cycle ok" not in ev["completed"]
    assert "reached nothing" in ev["completed"]


def test_an_honest_no_op_recrawl_is_still_ok():
    """unchanged>0 means the pages WERE reached and were 304 or hash-identical. That is the normal,
    healthy re-crawl and must stay distinguishable from reaching nothing."""
    ev = _cycle(fetched=0, kept=0, skipped_unchanged=101)
    assert "reached_nothing" not in ev
    assert "no_new_data" in ev and "cycle ok" in ev["completed"]


def test_reaching_nothing_is_not_recorded_as_a_failure():
    """The dispatch worked and the site answered nothing — a fact about the site. Calling it an
    error would put it into retry backoff for a condition retrying does not fix."""
    ev = _cycle(fetched=0, kept=0)
    assert "failed" not in ev and "retry" not in ev
