"""Master switch: OFF must mean OFF, and the flush must never run against a live crawl.

The switch used to gate exactly one thing — whether the scheduler tick dispatches due jobs — so
flipping it off while a crawl was in flight left that crawl running to completion. An operator
pressing a master switch means "stop", so it now also halts the crawl and drops the durable
frontier, making the next start a clean slate rather than a resume.

The ORDERING is the whole thing, and it is why this is tested with a stub rather than live: the
crawler refuses /v1/resumable/clear with a 409 while a batch is running, because those rows ARE the
running crawl's resume point. Stop, then WAIT for the teardown, then clear. Get it wrong and the
flush either 409s (annoying) or — far worse — the wait is skipped, the clear lands mid-crawl, and a
crawl with 81,446 queued URLs loses its position with no way to get it back.

Runnable directly:  python tests/test_master_switch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api  # noqa: E402


class _FakeCrawler:
    """Stands in for crawler_client. `runs_for` = how many status polls report "still running"."""

    def __init__(self, runs_for: int = 0, stop_raises: bool = False, clear_raises: bool = False):
        self.runs_for = runs_for
        self.stop_raises = stop_raises
        self.clear_raises = clear_raises
        self.calls: list[str] = []

    def stop_crawl(self) -> dict:
        self.calls.append("stop")
        if self.stop_raises:
            raise RuntimeError("connection refused")
        return {"stopping": True}

    def fanout(self, fn, *a, **kw) -> list[dict]:
        """One row per instance, in the real client's shape: {instance, ok, data|error}.

        A stop must reach EVERY instance — one that only reached instance 0 left the rest crawling
        while the dashboard reported "not running". The single-instance fanout below is enough to
        exercise that path; what matters is that a raising fn becomes ok=False rather than an
        exception, because "no instance answered" and "the call blew up" are different outcomes.
        """
        try:
            return [{"instance": 0, "ok": True, "data": fn(*a, **kw)}]
        except Exception as e:  # noqa: BLE001
            return [{"instance": 0, "ok": False, "error": str(e)}]

    def batch_status(self) -> dict:
        self.calls.append("status")
        running = self.runs_for > 0
        self.runs_for -= 1
        return {"running": running}

    def clear_resumable(self, job_ids=None) -> dict:
        self.calls.append("clear")
        if self.clear_raises:
            raise RuntimeError("409 a batch is running — stop it before clearing resume state")
        return {"jobs": 3, "rows": 4242}


class _FakeScheduler:
    def __init__(self):
        self.master = None
        self.halted = None

    def set_master(self, db, enabled):
        self.master = enabled

    def set_halted(self, db, halted):
        # Turning the master switch OFF also halts the fleet, or auto-refill sees plain idleness and
        # resumes the crawl the operator just stopped. A fake without this raises AttributeError and
        # the test fails for a reason that has nothing to do with what it is checking.
        self.halted = halted


def _patched(fake, wait_s=2, sleep_calls=None):
    """Swap the module globals the endpoint reaches for, and give it a sleep that does not sleep."""
    sched = _FakeScheduler()
    saved = (api.crawler_client, api.scheduler, api._MASTER_STOP_WAIT_S, api.time.sleep)
    api.crawler_client = fake
    api.scheduler = sched
    api._MASTER_STOP_WAIT_S = wait_s
    api.time.sleep = lambda s: (sleep_calls.append(s) if sleep_calls is not None else None)
    return sched, saved


def _restore(saved):
    api.crawler_client, api.scheduler, api._MASTER_STOP_WAIT_S, api.time.sleep = saved


def _call(enabled, flush, fake, **kw):
    sched, saved = _patched(fake, **kw)
    try:
        return api.set_master(api.MasterToggle(enabled=enabled, flush=flush), db=None), sched
    finally:
        _restore(saved)


def t_switching_off_with_flush_stops_then_waits_then_clears() -> None:
    """The happy path, and the order is the assertion. `clear` must come after a `status` poll that
    saw running=False — never straight after `stop`, which only SIGNALS a teardown."""
    fake = _FakeCrawler(runs_for=2)          # still running for two polls, then down
    out, sched = _call(False, True, fake)
    assert sched.master is False
    assert out["stopped"] is True and out["flushed"] is True, out
    assert "4242" in out["detail"], out
    assert fake.calls[0] == "stop", fake.calls
    assert fake.calls[-1] == "clear", fake.calls
    assert fake.calls.count("status") >= 3, f"must poll until it is really down: {fake.calls}"


def t_a_crawl_that_will_not_stop_keeps_its_resume_position() -> None:
    """The safety property. If the crawl is still up when patience runs out, the frontier is LEFT
    ALONE — a stuck teardown must not cost the operator their queued URLs. Reported honestly rather
    than silently skipped, so they know the flush did not happen."""
    fake = _FakeCrawler(runs_for=10_000)     # never stops
    out, _ = _call(False, True, fake, wait_s=0)
    assert out["flushed"] is False, out
    assert "clear" not in fake.calls, f"cleared the frontier of a live crawl: {fake.calls}"
    assert "NOT cleared" in out["detail"], out


def t_switching_off_without_flush_still_stops_the_crawl() -> None:
    """THE OPERATOR-REPORTED BUG, 2026-08-10: "master switch and category switch both didn't work,
    maintenance mode stopped it".

    `flush` used to gate the STOP as well as the frontier drop, so declining to throw away the
    resume position — the safe-looking answer, and the one the dialog recommended — meant the fleet
    was never told to stop at all. The switch showed OFF over twelve crawling instances.

    OFF now always stops. `flush` decides ONLY whether the resume position is also dropped."""
    fake = _FakeCrawler(runs_for=1)
    out, sched = _call(False, False, fake)
    assert sched.master is False
    assert "stop" in fake.calls, f"master OFF did not stop the crawl: {fake.calls}"
    assert "clear" not in fake.calls, f"declining the flush must keep the frontier: {fake.calls}"
    assert out["stopped"] is True and out["flushed"] is None, out
    assert out["still_running"] == [], out
    assert "resume position kept" in out["detail"], out


def t_without_flush_a_stuck_teardown_is_reported_not_hidden() -> None:
    """A stop that has been signalled but not completed is a real state. Saying "stopped" while six
    instances still hold workers is the same class of lie the switch was already telling."""
    fake = _FakeCrawler(runs_for=10_000)
    out, _ = _call(False, False, fake, wait_s=0)
    assert out["still_running"] == [0], out
    assert "winding down" in out["detail"], out
    assert out["flushed"] is None, "nothing was asked to be flushed, so nothing failed to flush"
    assert "clear" not in fake.calls


def t_switching_on_never_stops_or_clears_anything() -> None:
    """Arming the switch is not a destructive operation, whatever `flush` says."""
    fake = _FakeCrawler()
    out, sched = _call(True, True, fake)
    assert sched.master is True
    assert fake.calls == [], f"arming must not touch the crawler: {fake.calls}"
    assert out["flushed"] is None, out


def t_an_unreachable_crawler_still_flips_the_switch() -> None:
    """The switch is the control plane's own state. If the crawler is down the operator must still
    be able to disarm — and must be told the stop did not land, not left assuming it did."""
    fake = _FakeCrawler(stop_raises=True)
    out, sched = _call(False, True, fake)
    assert sched.master is False, "the switch must flip even when the crawler is unreachable"
    assert out["stopped"] is False and out["flushed"] is None, out
    assert "could not stop" in out["detail"], out
    assert "clear" not in fake.calls


def t_a_failed_clear_is_reported_not_swallowed() -> None:
    fake = _FakeCrawler(runs_for=0, clear_raises=True)
    out, _ = _call(False, True, fake)
    assert out["stopped"] is True and out["flushed"] is False, out
    assert "could not be cleared" in out["detail"], out


# ── the dispatch loop must obey the switch mid-flight ────────────────────────
#
# The endpoint above is only half of "off means off". A tick reads the switch ONCE and may then
# dispatch up to MAX_DISPATCH_PER_TICK jobs, each dispatch() BLOCKING for the length of a whole
# crawl. Flipping the switch off used to stop only the crawl in flight; the loop moved on to the
# next job, and the crawler clears its own stop signal on the way into a batch, so the halt was
# undone. The operator saw a "stopped" pipeline keep crawling for up to 25 more jobs.

from mallory_orchestrator import scheduler as sch  # noqa: E402


class _Job:
    """The slice of ScheduledJob run_due() touches."""

    def __init__(self, i: int):
        self.id = i
        self.job_type = "news"
        self.job = {"job_id": f"j{i}", "job_type": "news", "seed_urls": ["https://x"]}
        self.last_status = "pending"
        self.last_run_at = None


class _DB:
    """A Session whose `get` models the ONE property this fix turns on: SessionLocal is
    expire_on_commit=False, so a row already in the identity map is returned from memory and never
    re-read. `value` here only becomes visible after expire_all() — exactly like the real thing.
    Without the expire_all() in run_due(), the re-check reads the value the tick started with and
    the fix is a no-op that still passes a naive test."""

    def __init__(self, master: bool = True):
        self.stored = "true" if master else "false"
        self.visible = self.stored
        self.events: list[str] = []

    def flip_off(self):                    # what the API session does, from another connection
        self.stored = "false"

    def expire_all(self):
        self.visible = self.stored

    def get(self, _model, key):
        # KEY-AWARE, because `_halt_reason` now also reads maintenance_mode. A fake that returned
        # the master's value for every key made "master on" read as "maintenance on" and halted
        # every batch — a test failure that says nothing about the code under test.
        if key != "pipeline_enabled":
            return None                      # unset ⇒ off, which is the real default
        return type("Setting", (), {"value": self.visible})()

    def add(self, row):
        self.events.append(getattr(row, "event", ""))

    def commit(self):
        pass


def _run_due_with(db, jobs, dispatch):
    """run_due with every DB-touching collaborator stubbed — this is about the loop, not the ORM."""
    saved = {k: getattr(sch, k) for k in
             ("due_jobs", "disabled_categories", "order_jobs", "policy_for", "log_cycle",
              "_on_success", "tiering")}
    sch.due_jobs = lambda _db, _now: list(jobs)
    sch.disabled_categories = lambda _db: set()
    sch.order_jobs = lambda _db, due, _now: due
    sch.policy_for = lambda _db, _sj: None
    sch.log_cycle = lambda *a, **k: None
    sch._on_success = lambda *a, **k: None
    sch.tiering = type("T", (), {"observe": staticmethod(lambda *a, **k: None)})()
    rec = sch.orchestrate.record_run                 # writes Source/coverage rows — real ORM, not us
    sch.orchestrate.record_run = lambda *a, **k: None
    try:
        return sch.run_due(db, dispatch=dispatch, busy_check=lambda: False)
    finally:
        sch.orchestrate.record_run = rec
        for k, v in saved.items():
            setattr(sch, k, v)


def t_flipping_the_switch_off_stops_the_rest_of_the_tick() -> None:
    """The reported bug. Five jobs are due; the switch goes off while the first is crawling.
    Jobs 2-5 must never be dispatched."""
    db = _DB(master=True)
    sent: list[str] = []

    def dispatch(payload):
        sent.append(payload["job_id"])
        db.flip_off()                      # the operator hits the master switch mid-crawl
        return {"summary": {"fetched": 1, "kept": 1}}

    n = _run_due_with(db, [_Job(i) for i in range(5)], dispatch)
    assert sent == ["j0"], f"kept dispatching after the switch went off: {sent}"
    assert n == 1, n
    assert "skipped_master_off" in db.events, f"the abandoned jobs were not logged: {db.events}"


def t_the_switch_is_re_read_from_the_db_not_the_identity_map() -> None:
    """Guards the expire_all(). _DB.get() returns the cached value until expire_all() runs, so if
    that call is ever dropped this test fails while the one above would still pass."""
    db = _DB(master=True)
    seen: list[str] = []
    orig = db.expire_all
    db.expire_all = lambda: (seen.append("expired"), orig())[1]
    _run_due_with(db, [_Job(0), _Job(1)], lambda p: (db.flip_off(), {"summary": {}})[1])
    assert seen, "run_due never expired the session — it cannot see another session's write"


def t_a_switch_left_on_dispatches_everything() -> None:
    """The negative control: without it, a re-check that always said 'off' would pass the two
    tests above and silently disable the scheduler."""
    db = _DB(master=True)
    sent: list[str] = []
    n = _run_due_with(db, [_Job(i) for i in range(4)],
                      lambda p: (sent.append(p["job_id"]), {"summary": {}})[1])
    assert sent == ["j0", "j1", "j2", "j3"], sent
    assert n == 4, n
    assert "skipped_master_off" not in db.events, db.events


# ── the MANUAL batch loop must obey the switch too ──────────────────────────
#
# The scheduler loop above was only one of the two things that dispatch crawls. "Run all now" goes
# through orchestrate.run_manual_batch, which walks the site list in CHUNKS and blocks on each. That
# loop read nothing between chunks, so switching the master off stopped the five sites in flight and
# then started the next five — the exact symptom the operator reported. Same shape as the scheduler
# bug, same fix, and it needs its own test because the two loops share no code.

from mallory_orchestrator import orchestrate as orch  # noqa: E402


def _run_manual_with(jobs, parallel, dispatch, *, master=lambda: True, stop_pending=lambda: False):
    """run_manual_batch with the DB, the crawler and the recording stubbed — this is about which
    chunks get dispatched, nothing else."""
    saved = {"SessionLocal": orch.SessionLocal if hasattr(orch, "SessionLocal") else None,
             "client": orch.crawler_client, "rec": orch._record_manual,
             "halt": orch._halt_reason}
    import mallory_orchestrator.db as odb
    saved_sess = odb.SessionLocal

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def add(self, _r): pass

    odb.SessionLocal = lambda: _S()
    # `pool()` as well as dispatch_batch: run_manual_batch now routes each chunk to the crawler
    # instance that owns its domains, so the stub has to model that much of the interface. One
    # entry keeps these tests on the single-instance path, which is the behaviour they are about.
    orch.crawler_client = type("C", (), {"dispatch_batch": staticmethod(dispatch),
                                         "pool": staticmethod(lambda: ["http://stub:8099"])})()
    orch._record_manual = lambda db, job, summ, each: {"job_id": job.get("job_id")}
    # The real thing, wired to the two stub signals — so the ORDER of the checks and the fact that
    # it runs BEFORE dispatch are both under test, not just that some flag exists.
    orch._halt_reason = lambda _db, first_chunk=False: (
        "master switch off" if not master()
        else None if first_chunk
        else ("stop requested on the crawler" if stop_pending() else None))
    try:
        orch.run_manual_batch(jobs, parallel)
        return orch.manual_status()
    finally:
        odb.SessionLocal = saved_sess
        orch.crawler_client, orch._record_manual = saved["client"], saved["rec"]
        orch._halt_reason = saved["halt"]


def t_the_master_switch_stops_the_next_chunk_not_just_the_running_one() -> None:
    """THE reported bug: 'it stops the current 5 running, then the next 5 start automatically'."""
    on = [True]
    sent: list[list[str]] = []

    def dispatch(payload, timeout=None):
        sent.append([j["job_id"] for j in payload["jobs"]])
        on[0] = False                              # operator hits the master switch mid-chunk
        return {"results": [{"job_id": j["job_id"], "summary": {}} for j in payload["jobs"]]}

    jobs = [{"job_id": f"j{i}", "seed_urls": ["https://x"]} for i in range(15)]
    st = _run_manual_with(jobs, 5, dispatch, master=lambda: on[0])
    assert sent == [["j0", "j1", "j2", "j3", "j4"]], f"dispatched past the switch: {sent}"
    assert st["halted"] and "master switch off" in st["halted"], st["halted"]
    assert "10 site(s) not crawled" in st["halted"], st["halted"]
    assert st["running"] is False


def t_a_stop_pressed_on_the_crawler_is_not_undone_by_the_next_chunk() -> None:
    """The crawler ARMS itself on every batch (clear_stop on the way in), so a STOP pressed on its
    own dashboard survived only until this loop asked for another chunk."""
    stopped = [False]

    def dispatch(payload, timeout=None):
        stopped[0] = True
        return {"results": []}

    jobs = [{"job_id": f"j{i}", "seed_urls": ["https://x"]} for i in range(9)]
    st = _run_manual_with(jobs, 3, dispatch, stop_pending=lambda: stopped[0])
    assert st["halted"] and "stop requested on the crawler" in st["halted"], st["halted"]


def t_a_stale_stop_flag_does_not_refuse_a_brand_new_batch() -> None:
    """THE regression. The crawler's stop signal is a FILE, so it outlives the container: the Stop
    the master switch itself sends when it halts a crawl is still on disk when you press Run the
    next day. Read before the FIRST chunk it refused the whole batch — measured on the deployed
    stack as "stop requested on the crawler — 802 site(s) not crawled", with nothing running.

    It is not suppressed, only deferred: crawler-api arms itself on the way into every dispatch, so
    by the second chunk the flag can only have been set DURING this run."""
    sent: list[list[str]] = []

    def dispatch(payload, timeout=None):
        sent.append([j["job_id"] for j in payload["jobs"]])
        return {"results": [{"job_id": j["job_id"], "summary": {}} for j in payload["jobs"]]}

    jobs = [{"job_id": f"j{i}", "seed_urls": ["https://x"]} for i in range(6)]
    st = _run_manual_with(jobs, 3, dispatch, stop_pending=lambda: True)   # stale from the start
    assert sent == [["j0", "j1", "j2"]], f"a stale flag blocked the batch entirely: {sent}"
    assert st["halted"] and "stop requested on the crawler" in st["halted"], st["halted"]
    assert "3 site(s) not crawled" in st["halted"], st["halted"]


def t_an_untouched_switch_crawls_every_chunk() -> None:
    """Negative control: a halt check that always fired would pass both tests above and quietly
    reduce every batch to its first chunk."""
    sent: list[list[str]] = []

    def dispatch(payload, timeout=None):
        sent.append([j["job_id"] for j in payload["jobs"]])
        return {"results": [{"job_id": j["job_id"], "summary": {}} for j in payload["jobs"]]}

    jobs = [{"job_id": f"j{i}", "seed_urls": ["https://x"]} for i in range(7)]
    st = _run_manual_with(jobs, 3, dispatch)
    assert sent == [["j0", "j1", "j2"], ["j3", "j4", "j5"], ["j6"]], sent
    assert st["halted"] is None, st["halted"]


def t_an_unreachable_crawler_does_not_abandon_a_running_batch() -> None:
    """_halt_reason swallows a status-poll failure on purpose: the master switch is the authority,
    and a transient blip must not silently leave a batch half-done."""
    import mallory_orchestrator.orchestrate as real

    class _Boom:
        @staticmethod
        def batch_status():
            raise RuntimeError("connection refused")

        @staticmethod
        def fanout(fn, *a, **kw):
            # The real fanout never raises — it reports the failure per instance. _halt_reason must
            # cope with every row being ok=False, which is what a whole fleet being down looks like.
            try:
                return [{"instance": 0, "ok": True, "data": fn(*a, **kw)}]
            except Exception as e:  # noqa: BLE001
                return [{"instance": 0, "ok": False, "error": str(e)}]

    saved, real.crawler_client = real.crawler_client, _Boom()
    saved_sched = sys.modules.get("mallory_orchestrator.scheduler")
    try:
        db = _DB(master=True)
        # A SCHEDULED batch: the fake DB has no mode row, so it reads as normal mode, where the
        # "manual" lane is closed by design (test_dispatch_policy.py). This case is about the blip.
        assert real._halt_reason(db, lane="scheduled") is None, "a status blip halted the batch"
        db.flip_off()
        assert str(real._halt_reason(db, lane="scheduled")).startswith("master switch off"), \
            "the switch stopped being authoritative"
    finally:
        real.crawler_client = saved
        if saved_sched is not None:
            sys.modules["mallory_orchestrator.scheduler"] = saved_sched


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} MASTER-SWITCH TESTS PASSED")


def test_master_switch_suite() -> None:
    main()


if __name__ == "__main__":
    main()
