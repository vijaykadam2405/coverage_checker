"""A busy host must cost a REQUEUE, not a worker.

Measured 2026-07-31: 3,464 items abandoned by the 240s watchdog in twelve minutes. The obvious
reading — "those pages are slow" — was wrong. Timing the render stages on the two worst hosts:

    shvabe.com     goto 2.5s  netidle 1.5  settle 0.6  interact 3.3  settle 0.6  = 8.6s, settled
    www.gdbiw.com  goto 0.1s  netidle 1.5  settle 0.6  interact 1.9  settle 0.6  = 4.7s, settled

They render in under nine seconds and account for 1,758 of the abandonments between them. The time
was never spent rendering — it was spent queued on the per-host semaphore, whose acquire was
UNBOUNDED and sits inside the per-item watchdog. shvabe.com had 12,546 URLs against two permits.

Worse than the wasted work: the worker awaiting `_process` is pinned for those 240 seconds and can
serve no other host, which is what produced instances showing a 6,129-deep render queue with an
EMPTY lane (0/128).

The render lane already had this guard (RenderBusy). The httpx path did not, on the reasoning that
an httpx item holds no tab so waiting is free. It is not free — it costs the worker.

Runnable directly:  python tests/test_host_slot_wait.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402

SRC = Path(ae.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_env():
    """Put os.environ back after every test in this file.

    These tests drive behaviour through env vars, and env is PROCESS-global — leaving
    CRAWLER_HOST_QUEUE_MAX=1 behind broke six unrelated tests in the full run while every one of
    them passed in isolation, which is the most expensive kind of test failure to diagnose.
    """
    keys = [k for k in os.environ if k.startswith("CRAWLER_")]
    saved = {k: os.environ[k] for k in keys}
    yield
    for k in [k for k in os.environ if k.startswith("CRAWLER_")]:
        if k not in saved:
            del os.environ[k]
    os.environ.update(saved)



def _gate(limit: int = 2, wait_s: float = 0.2):
    """A REAL HostLimiter — hand-building the object missed fields (_delay) and only proved my fake
    was wrong. min_delay=0 so the politeness sleep does not dominate these timings."""
    os.environ["CRAWLER_HOST_SLOT_WAIT_S"] = str(wait_s)
    os.environ["CRAWLER_HOST_QUEUE_MAX"] = str(limit)
    return ae.HostLimiter(max_conc=limit, min_delay=0.0)


def test_a_free_host_is_acquired_immediately():
    async def go():
        g = _gate()
        async with g.slot("https://example.com/a"):
            return True
    assert asyncio.run(go()) is True


def test_a_saturated_host_raises_instead_of_waiting_for_ever():
    """The whole point: give the worker back long before the 240s watchdog would take it."""
    async def go():
        g = _gate(limit=1, wait_s=0.2)
        async with g.slot("https://busy.example/1"):          # holds the only permit
            try:
                async with g.slot("https://busy.example/2"):
                    return "acquired"
            except ae.HostSlotBusy as e:
                return str(e)
    out = asyncio.run(go())
    assert "no slot on busy.example" in out, out


def test_the_wait_is_far_below_the_item_watchdog():
    """A bound at or above CRAWLER_ITEM_TIMEOUT_S would never fire — the watchdog gets there first,
    and the watchdog is the destructive one (abandon + warning + budget spent) rather than the
    recoverable one (requeue, nothing recorded)."""
    assert 'CRAWLER_HOST_SLOT_WAIT_S", 45.0' in SRC
    assert 'CRAWLER_ITEM_TIMEOUT_S", 240' in SRC
    assert 45.0 < 240


def test_the_queue_depth_is_released_on_timeout():
    """The depth counter drives the render deflect guard. Leaking it on every timeout would make a
    host look permanently busier than it is and deflect work that could have run."""
    async def go():
        g = _gate(limit=1, wait_s=0.1)
        async with g.slot("https://busy.example/1"):
            for _ in range(3):
                try:
                    async with g.slot("https://busy.example/2"):
                        pass
                except ae.HostSlotBusy:
                    pass
            return g._queued.get("busy.example", 0)
    assert asyncio.run(go()) == 0


def test_zero_restores_the_old_unbounded_wait():
    """An escape hatch that must actually be reachable, since this changes long-standing behaviour."""
    async def go():
        g = _gate(limit=1, wait_s=0)
        async with g.slot("https://busy.example/1"):
            try:
                await asyncio.wait_for(g.slot("https://busy.example/2").__aenter__(), 0.3)
                return "acquired"
            except asyncio.TimeoutError:
                return "waited"                # blocked, exactly as before
            except ae.HostSlotBusy:
                return "raised"
    assert asyncio.run(go()) == "waited"


def test_every_acquire_site_reraises_past_its_broad_handler():
    """Each `async with self.host.slot(...)` sits inside an `except Exception` that would otherwise
    convert "the host was busy" into a recorded failure for that URL."""
    lines = SRC.split("\n")
    sites = [i for i, ln in enumerate(lines) if "async with self.host.slot(" in ln]
    assert len(sites) >= 8, f"expected the known acquire sites, found {len(sites)}"
    for i in sites:
        window = "\n".join(lines[i:i + 14])
        assert "except HostSlotBusy" in window, (
            f"the acquire at line {i + 1} is not guarded:\n{window[:400]}")


def test_the_worker_requeues_it_and_returns_the_budget():
    """Requeue without unreserve would let a merely-popular host silently eat max_pages."""
    body = SRC.split("except HostSlotBusy:")[-1]
    handler = SRC[SRC.index("            except HostSlotBusy:\n                # Not a failure"):][:1400]
    assert "item.ctx.unreserve()" in handler, "budget is not returned on a busy-host requeue"
    assert "_schedule_busy" in handler, "an httpx item is not requeued to the frontier"
    assert "_requeue_render" in handler, "a render item is not returned to the render lane"
    assert "deflected_host" in handler, "the deflection is not counted"
    assert body is not None


def test_a_render_item_goes_back_to_the_render_lane_not_the_frontier():
    """A render item returned to the frontier jumps ahead of cheap work again, which is exactly the
    prioritisation the two-lane split exists to remove."""
    handler = SRC[SRC.index("            except HostSlotBusy:\n                # Not a failure"):][:1400]
    assert "if item.needs_render:" in handler
    assert handler.index("_requeue_render") < handler.index("_schedule_busy"), (
        "the render branch must be the one guarded by needs_render")


def test_no_unbounded_acquire_is_left_inside_the_item_watchdog():
    """Both gates, or the wait just moves one level up.

    Bounding only the per-host gate left the retry path waiting on the GLOBAL render permit
    with no timeout — shvabe.com still produced 630 abandonments in six minutes for pages that
    render in 9.4s. Any acquire without a bound is inside the 240s watchdog and will pin a
    worker until it fires.
    """
    for line in SRC.splitlines():
        st = line.strip()
        if st.startswith("async with self.adaptive.slot("):
            assert "timeout=" in st, "unbounded render-permit acquire: %s" % st


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} HOST-SLOT-WAIT TESTS PASSED")
