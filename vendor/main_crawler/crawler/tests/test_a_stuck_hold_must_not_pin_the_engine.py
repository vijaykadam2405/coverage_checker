"""A hold whose host never drains must not pin the engine for the life of the process.

MEASURED ON THE LIVE FLEET, 2026-08-21, thirteen hours after a clean restart:

    api-9   21 crawl dispatches, ONE live batch, and THREE adaptive heartbeats per 10s
    api-8   two heartbeats per 10s, reporting render_limit=16/9/16 within half a second
    api-8   held=1038, render_q=34, frontier=0, fetched=9 in 562s, 33,201 deflected_host
    api-8   every give-up: "topwar.ru slots never drained"

The heartbeat fires once per ~10s per ENGINE, so two and three heartbeats mean two and three
AsyncEngine objects alive at once inside one process. They had not been dispatched concurrently —
api-9 took no crawl request at all in the preceding hour. They were engines that STARTED HOURS
EARLIER AND NEVER EXITED.

Both exit doors are closed for an engine whose remaining work is one jammed host:

  * the empty-check needs `_held_items == 0`, and the hold cannot drain because draining it
    requires the host to hand a render permit back, which a jammed host never does;
  * the idle watchdog cannot fire either, because HostLimiter.slot heartbeats `_last_progress`
    on every cooldown wait — deliberately, so a legitimately slow host is not killed as a wedge.

So the coroutine lives forever on the shared engine loop. That is not merely untidy: each stale
engine keeps its OWN adaptive gate, so the render limit stops being global — three engines at
render_limit=16 admit 48 concurrent renders on a lane sized for 16, at roughly 1.2 cores each.

The asymmetry that causes it: an item in render_q is examined, ages against
CRAWLER_HOST_DEFLECT_BUDGET_S and eventually gives up (api-8 had 4,070 such give-ups). An item in
`_host_hold` is in NO queue. Nothing examines it, nothing ages it, no budget reaches it.

The fix is not to drop the `_held_items == 0` clause — that clause is what stops a batch marking
unfetched pages done in the durable frontier. It is to let the hold age exactly like the queue.

Runnable directly:  python tests/test_a_stuck_hold_must_not_pin_the_engine.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402

BUDGET = 180.0          # CRAWLER_HOST_DEFLECT_BUDGET_S default, shared with the deflect path


@pytest.fixture(autouse=True)
def _restore_env():
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


class _Item:
    def __init__(self, url: str) -> None:
        self.url = url


def _engine(lane_max: int = 4):
    """An engine with only the lane and host-clock machinery built — no browsers, no Playwright."""
    os.environ["CRAWLER_HOST_LANE_MAX"] = str(lane_max)
    os.environ["CRAWLER_HOST_RENDER_MAX"] = "2"
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e.render_q = asyncio.Queue()
    e.frontier = asyncio.Queue()
    e._render_hosts = {}
    e._lane_hosts = {}
    e._host_hold = {}
    e._held_items = 0
    e.host_busy_since = {}
    e.host_last_ok = {}
    return e


def _fill(e, host: str, n: int) -> None:
    for i in range(n):
        e._lane_put(_Item(f"https://{host}/{i}"))


def t_a_wedged_hosts_hold_drains_so_the_engine_can_exit() -> None:
    """THE LEAK. A host jammed past the budget with nothing delivered must release its hold.

    `_drain` returns only when `_held_items == 0`, so this assertion IS the engine's ability to
    finish. While it fails, the batch object stays resident with its own adaptive gate.
    """
    async def go():
        e = _engine(lane_max=4)
        _fill(e, "wedged.example", 500)
        assert e._held_items == 496, "precondition: the surplus is parked"
        # The host jammed four minutes ago and has never served a page this run — exactly what
        # host_busy_since means, since it self-clears the moment the host has queue room.
        e.host_busy_since["wedged.example"] = asyncio.get_running_loop().time() - (BUDGET + 60)

        e._release_held_all()

        assert e._held_items == 0, (
            "%d pages are still parked behind a host that gave up four minutes ago; _drain can "
            "never return and this engine leaks for the life of the process" % e._held_items)
        assert not e._host_hold, "the hold dict still holds a host: %r" % list(e._host_hold)

    asyncio.run(go())


def t_unparked_work_is_not_lost() -> None:
    """The engine may only stop waiting for these pages by handing them somewhere that disposes of
    them. Dropping them silently would be worse than the leak: the batch reports success and the
    durable frontier records pages that were never fetched, so a resume skips them."""
    async def go():
        e = _engine(lane_max=4)
        want = {f"https://wedged.example/{i}" for i in range(200)}
        for u in want:
            e._lane_put(_Item(u))
        e.host_busy_since["wedged.example"] = asyncio.get_running_loop().time() - (BUDGET + 60)

        e._release_held_all()

        seen = set()
        while e.render_q.qsize():
            seen.add(e.render_q.get_nowait().url)
        assert seen == want, "lost %d URLs on the way out of the hold" % len(want - seen)

    asyncio.run(go())


def t_a_saturated_host_keeps_its_hold() -> None:
    """SATURATED IS NOT WEDGED, and this is the distinction most likely to regress.

    A host whose every page needs a browser runs permanently at its queue cap while working
    perfectly. Measured once on yakovlev.ru: 184 give-ups on a site that was serving us fine. The
    signal that separates them is host_last_ok — a working host keeps delivering, so the budget is
    measured from its last delivery, not from when the jam started.
    """
    async def go():
        e = _engine(lane_max=4)
        _fill(e, "busy.example", 500)
        now = asyncio.get_running_loop().time()
        e.host_busy_since["busy.example"] = now - (BUDGET + 60)   # jammed a long time...
        e.host_last_ok["busy.example"] = now - 5                  # ...but delivering a page ago

        e._release_held_all()

        assert e._held_items == 496, (
            "a working-but-saturated host had its hold dumped (%d parked); those pages will be "
            "given up on as host_busy and the site will be half-crawled" % e._held_items)

    asyncio.run(go())


def t_an_unjammed_host_keeps_its_admission_cap() -> None:
    """The ordinary path is untouched: with no jam recorded, the hold is still the hold and the
    lane still admits only its cap. Without this the whole admission feature quietly disappears."""
    async def go():
        e = _engine(lane_max=4)
        _fill(e, "fine.example", 500)

        e._release_held_all()

        assert e.render_q.qsize() == 4, (
            "the lane took %d entries for one host with no jam recorded — admission is off and the "
            "polling livelock is back" % e.render_q.qsize())
        assert e._held_items == 496

    asyncio.run(go())


def t_unparking_survives_a_missing_host_clock() -> None:
    """This runs on the drain tick, which also runs on engines built without a full __init__
    (fixtures, the C3-only path). An AttributeError here escapes into _drain and kills the batch —
    the same reasoning as _render_host_at_cap's bare getattr."""
    async def go():
        e = _engine(lane_max=2)
        _fill(e, "bare.example", 10)
        del e.host_busy_since
        del e.host_last_ok
        e._release_held_all()          # must not raise

    asyncio.run(go())


def test_a_stuck_hold_must_not_pin_the_engine_suite():
    """pytest collects test_* only — without this wrapper the t_* checks above never run."""
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} STUCK-HOLD TESTS PASSED")
