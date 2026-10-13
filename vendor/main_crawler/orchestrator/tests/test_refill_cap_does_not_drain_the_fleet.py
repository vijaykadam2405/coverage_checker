"""The concurrency cap must not turn the refill watcher into a fleet drain.

MEASURED 2026-08-15 on the live fleet:

    MAX_BUSY_INSTANCES = 7
    busy               = 10
    room = 7 - 10      = -3   ->  {"checked": 0, "idle": 0, "resumed": 0, "at_capacity": 10}

`refill_once` returns at `room <= 0` before checking a single instance, and `loop()` only logged
when it RESUMED something. So with more instances busy than the cap allows — which the scheduler and
manual resumes can both cause, since the cap gates only this watcher — every tick returned silently
having done nothing, for days.

The consequence is one-way. An instance that finishes is never restarted, because the cap only
falls when even MORE instances go idle. The fleet can shrink and cannot grow: 12 -> 9 -> 7 over
three days, with 54,767 pending URLs sitting on three idle instances and no log line anywhere.

Two properties are pinned here:
  * the cap still binds — this watcher must not be the thing that saturates the box; and
  * being capped is VISIBLE, because a watcher that does nothing is indistinguishable from a
    healthy one when both are silent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import refill, scheduler  # noqa: E402


class _Client:
    """crawler_client for a fleet where `running` says which instances are busy."""

    def __init__(self, running: set[int], pending: dict[int, int] | None = None, n: int = 12):
        self.n = n
        self.running = running
        self.pending = pending or {}
        self.resumed: list[int] = []
        self._bases = ["http://i%d" % i for i in range(n)]

    def pool(self):
        return list(self._bases)

    def _idx(self, base):
        return self._bases.index(base)

    def batch_status(self, base=None):
        return {"running": self._idx(base) in self.running, "stop_pending": False}

    def resumable(self, limit=1000, base=None):
        i = self._idx(base)
        p = self.pending.get(i, 0)
        return {"jobs": [{"job_id": "j%d" % i, "pending": p, "has_config": True}] if p else []}

    def resume(self, ids, background=True, forward_to_ingest=True, base=None):
        self.resumed.append(self._idx(base))
        return {"resumed": list(ids)}


def _run(client, max_busy, halted=False, maintenance=False):
    # Patch the scheduler MODULE, not refill.scheduler: refill imports it lazily inside
    # refill_once, so the attribute does not exist on the refill module until first call.
    saved = (refill.crawler_client, scheduler.MAX_BUSY_INSTANCES)
    refill.crawler_client = client
    scheduler.MAX_BUSY_INSTANCES = max_busy

    import mallory_orchestrator.db as odb
    saved_sess = odb.SessionLocal

    class _S:
        def close(self): pass
    odb.SessionLocal = lambda: _S()
    saved_h, saved_m, saved_l = scheduler.halted, scheduler.maintenance, scheduler.lane_open
    saved_o = scheduler.owned_by_mode
    scheduler.halted = lambda _db: halted
    scheduler.maintenance = lambda _db: maintenance
    scheduler.lane_open = lambda _db, _lane: True     # the dispatch policy: open, not under test here
    scheduler.owned_by_mode = lambda _db, ids: list(ids)   # mode scoping: not under test here
    try:
        return refill.refill_once()
    finally:
        refill.crawler_client, scheduler.MAX_BUSY_INSTANCES = saved
        odb.SessionLocal = saved_sess
        scheduler.halted, scheduler.maintenance, scheduler.lane_open = saved_h, saved_m, saved_l
        scheduler.owned_by_mode = saved_o


def t_over_cap_reports_capacity_instead_of_failing_silently() -> None:
    """THE BUG'S SIGNATURE. Still refuses to add load — but says so, and says how far over it is."""
    c = _Client(running=set(range(10)), pending={10: 33_000, 11: 18_000})
    out = _run(c, max_busy=7)
    assert out["resumed"] == 0, "must not add load while over the cap"
    assert out["at_capacity"] == 10, out
    assert out.get("room") == -3, "the operator needs to see HOW far over, not just that it is: %r" % out


def t_the_cap_at_fleet_size_refills_the_idle_instances() -> None:
    """THE FIX. Cap raised to the fleet size — the cpuset now enforces the CPU budget the cap was
    only ever a proxy for — so the two idle instances holding 51k URLs get resumed."""
    c = _Client(running=set(range(10)), pending={10: 33_000, 11: 18_000})
    out = _run(c, max_busy=12)
    assert out["resumed"] == 2, out
    assert sorted(c.resumed) == [10, 11], c.resumed


def t_a_busy_instance_is_never_disturbed() -> None:
    """The cap's real job. Refill must never touch an instance that is working."""
    c = _Client(running={0, 1, 2}, pending={0: 999, 1: 999, 3: 5_000})
    out = _run(c, max_busy=12)
    assert c.resumed == [3], "resumed a working instance: %r" % c.resumed
    assert out["resumed"] == 1


def t_room_bounds_how_many_start_not_whether_any_do() -> None:
    """With 11 busy and a cap of 12 there is room for exactly one. It must start one, not zero and
    not both — the cap has to keep meaning something once it stops aborting the pass."""
    c = _Client(running=set(range(11)), pending={11: 9_000})
    out = _run(c, max_busy=12)
    assert out["resumed"] == 1, out


def t_idle_with_no_saved_work_is_not_counted_as_idle() -> None:
    """An instance that genuinely finished has nothing to resume; it must not be reported as idle
    work waiting, or the number the operator reads is a lie."""
    c = _Client(running=set(range(11)), pending={})
    out = _run(c, max_busy=12)
    assert out["resumed"] == 0 and out["idle"] == 0, out


def t_halt_and_maintenance_still_win() -> None:
    """Raising the cap must not weaken the two flags that mean 'stay stopped'."""
    for kw in ({"halted": True}, {"maintenance": True}):
        c = _Client(running=set(), pending={0: 5_000})
        out = _run(c, max_busy=12, **kw)
        assert out["resumed"] == 0, (kw, out)
        assert c.resumed == [], (kw, c.resumed)


def t_the_loop_survives_its_own_first_tick() -> None:
    """The tests above all drive refill_once directly, so they cannot see a loop() that dies before
    reaching it — which is exactly what happened: a scheduler.* reference added to loop()'s startup
    line raised NameError, because refill imports scheduler LAZILY inside refill_once to dodge a
    circular import. An exception in a bare asyncio task surfaces nowhere until awaited, so the
    watcher was simply dead and silent. One real tick, asserted to complete."""
    import asyncio

    c = _Client(running=set(range(10)), pending={10: 5_000, 11: 5_000})

    async def _one_tick():
        stop = asyncio.Event()
        saved = (refill.crawler_client, scheduler.MAX_BUSY_INSTANCES)
        import mallory_orchestrator.db as odb
        saved_sess = odb.SessionLocal
        saved_h, saved_m = scheduler.halted, scheduler.maintenance

        class _S:
            def close(self): pass
        refill.crawler_client = c
        scheduler.MAX_BUSY_INSTANCES = 12
        odb.SessionLocal = lambda: _S()
        scheduler.halted = lambda _db: False
        scheduler.maintenance = lambda _db: False
        os.environ["CRAWLER_REFILL_TICK_S"] = "30"
        try:
            task = asyncio.create_task(refill.loop(stop))
            await asyncio.sleep(1.5)          # long enough for one tick to run
            stop.set()
            await asyncio.wait_for(task, timeout=10)   # re-raises anything the task swallowed
        finally:
            refill.crawler_client, scheduler.MAX_BUSY_INSTANCES = saved
            odb.SessionLocal = saved_sess
            scheduler.halted, scheduler.maintenance = saved_h, saved_m

    asyncio.run(_one_tick())
    assert sorted(c.resumed) == [10, 11], "the loop never reached refill_once: %r" % c.resumed


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d REFILL-CAP TESTS PASSED" % len(fns))


def test_refill_cap_suite() -> None:
    main()


if __name__ == "__main__":
    main()
