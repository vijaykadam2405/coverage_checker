"""One host must not be allowed to own the render lane by sheer queue depth.

Measured 2026-08-20 across the live twelve-instance fleet, ten hours after a clean restart:

    fleet  deflected_host 24,887,085   fetched 42,658   give-ups 7,824  (0.03% of deflections)
    fleet  0.96 pages/s while burning 23.9 of 40 cores  (~25 core-seconds per page)
    inst 6 deflected_host 11,651,845   fetched  2,313   give-ups 28
    inst 5 deflected_host  4,447,365   fetched  5,712   give-ups 0
    inst 12 deflected_host 6,508,389   fetched  4,701   give-ups 0

Instance 12's issue log named the cause: three hosts owned everything —
defenceconnect.com.au 70,250 host_busy rows, military.africa 30,058, forcesnews.com 17,084.

The give-up messages settle what was NOT happening. Instance 6 logged eleven and a half million
deflections, yet the items that finally gave up had deflected seven or eight times each. No item
was retrying forever; there were simply tens of thousands of items each polling a few times. The
volume came from BREADTH, and a per-item patience budget cannot see breadth.

Two polls fed it, both scaling with queue depth against a resource fixed by host concurrency:

  * _pull_render scanned and rotated up to CRAWLER_RENDER_SCAN (64) entries on every pull; with
    the queue owned by capped hosts, every pull rotated 64 entries and returned nothing.
  * the blocking waiter took an at-cap item, put it straight back, and slept 250ms — roughly four
    laps a second per worker, about 24 workers, for as long as the queue lasted.

And the loop closed on itself: that CPU is what the adaptive governor reads. It saw ~98% against a
target of 85 and clamped the render lane to its FLOOR of 4 of 24 permits, so the queue drained
slower, so it got deeper, so the polling got worse.

The existing give-up budget could not have helped and was right not to fire: those hosts were
serving pages, so host_last_ok kept resetting `waited` below the 180s budget. It is built for a
WEDGED host. This is a SATURATED one, and the answer to saturation is not to abandon the site —
it is to stop asking it the same question thousands of times a second.

Runnable directly:  python tests/test_render_lane_admission.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_env():
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


class _Item:
    """Minimal stand-in: admission only ever reads the URL."""

    def __init__(self, url: str) -> None:
        self.url = url


def _engine(lane_max: int = 4, render_max: int = 2):
    """An engine with only the lane machinery built — no browsers, no Playwright."""
    os.environ["CRAWLER_HOST_LANE_MAX"] = str(lane_max)
    os.environ["CRAWLER_HOST_RENDER_MAX"] = str(render_max)
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e.render_q = asyncio.Queue()
    e.frontier = asyncio.Queue()
    e._render_hosts = {}
    e._lane_hosts = {}
    e._host_hold = {}
    e._held_items = 0
    return e


def t_a_deep_host_does_not_fill_the_lane() -> None:
    """THE REGRESSION. 70,000 URLs on one host must not become 70,000 queue entries.

    Every entry in render_q is re-examined by the scan and by the blocking waiter, so queue depth
    IS the polling rate. The host's politeness cap is unchanged — this bounds only how many of its
    URLs wait in the lane to use it.
    """
    async def go():
        e = _engine(lane_max=4)
        for i in range(5000):
            e._lane_put(_Item(f"https://deep.example/{i}"))
        assert e.render_q.qsize() == 4, (
            "one host put %d entries in the lane; every one of them is polled"
            % e.render_q.qsize())
        assert e._held_items == 4996, "the surplus was dropped, not held: %d" % e._held_items

    asyncio.run(go())


def t_held_work_is_never_lost() -> None:
    """Every URL comes back out, exactly once. A hold that loses pages is worse than the livelock:
    the crawl would look healthy and quietly skip thousands of pages."""
    async def go():
        e = _engine(lane_max=4)
        want = {f"https://deep.example/{i}" for i in range(500)}
        for u in want:
            e._lane_put(_Item(u))
        seen = set()
        # Drain the way the engine does: take what is queued, then release as permits come back.
        while e.render_q.qsize() or e._held_items:
            while e.render_q.qsize():
                it = e.render_q.get_nowait()
                e._lane_release(it)
                assert it.url not in seen, "a held item came back twice: %s" % it.url
                seen.add(it.url)
            e._release_held("deep.example")
        assert seen == want, "lost %d URLs in the hold" % len(want - seen)

    asyncio.run(go())


def t_a_freed_permit_refills_the_lane() -> None:
    """The wake-up must be the returned permit, not a timer.

    This is the whole point of the change: the old path put every blocked item on a call_later of
    up to 60s, so the wake-up rate scaled with how many items were blocked. Releasing on permit
    return makes it scale with work COMPLETED instead — one wake-up per finished render.
    """
    async def go():
        e = _engine(lane_max=2)
        for i in range(10):
            e._lane_put(_Item(f"https://deep.example/{i}"))
        assert e.render_q.qsize() == 2 and e._held_items == 8
        # A worker takes both and finishes one.
        for _ in range(2):
            e._lane_release(e.render_q.get_nowait())
        assert e.render_q.qsize() == 0, "precondition: lane drained"
        e._release_held("deep.example")
        assert e.render_q.qsize() == 2, (
            "returning a permit did not refill the lane (%d) — the permits would sit idle while "
            "the work sat in the hold" % e.render_q.qsize())

    asyncio.run(go())


def t_one_host_cannot_starve_another() -> None:
    """The reason the fix is admission and not a longer backoff.

    A slow host with 5,000 queued URLs and a fast host with 5 must not mean the fast one waits.
    Under the old shape the scan rotated 64 entries of the deep host per pull and the shallow
    host's five pages sat behind them.
    """
    async def go():
        e = _engine(lane_max=4)
        for i in range(5000):
            e._lane_put(_Item(f"https://deep.example/{i}"))
        for i in range(3):
            e._lane_put(_Item(f"https://quick.example/{i}"))
        queued = []
        while e.render_q.qsize():
            queued.append(e.render_q.get_nowait().url)
        quick = [u for u in queued if "quick" in u]
        assert len(quick) == 3, (
            "the shallow host got %d of its 3 pages into the lane behind a 5,000-deep "
            "neighbour" % len(quick))
        assert len(queued) == 7, "lane held %d entries, expected 4 deep + 3 quick" % len(queued)

    asyncio.run(go())


def t_the_drain_counts_parked_work_as_outstanding() -> None:
    """A batch must not finish while pages sit in the hold.

    _drain exits when the queues are empty and nothing is in flight, and on exit the engine marks
    the batch complete. Parked items are in NEITHER queue, so without this the batch would call
    itself done with thousands of pages unfetched — and the durable frontier would record them,
    so the resume built to recover exactly this would skip them. Pinned against the source because
    the failure is silent: the crawl reports success.
    """
    src = Path(ae.__file__).read_text(encoding="utf-8")
    i = src.index("if (self.frontier.qsize() == 0 and self.render_q.qsize() == 0")
    cond = src[i:i + 400]
    assert "_held_items == 0" in cond, (
        "the drain exit does not count held work; a batch can finish with pages parked")


def t_admission_survives_an_unparseable_url() -> None:
    """This runs on the worker hot path. An exception here kills the worker task and takes its
    lane slot with it — the same reasoning as _render_host_at_cap's bare getattr."""
    async def go():
        e = _engine(lane_max=1)
        e._lane_put(_Item("http://[not-a-host"))   # raises in urlsplit
        e._lane_put(_Item(""))
        assert e.render_q.qsize() == 2, "an unparseable URL must pass through, not be swallowed"

    asyncio.run(go())


def test_render_lane_admission_suite():
    """pytest collects test_* only — without this wrapper the t_* checks above never run."""
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()


def t_lane_counts_come_back_down() -> None:
    """THE STALL. Every item that leaves render_q must decrement its host's lane count.

    _lane_hosts is incremented in one place and decremented in three, and a missed decrement is a
    ONE-WAY RATCHET: the count climbs to the admission cap, _release_held then computes zero room,
    and the hold stops draining permanently. Measured on the live fleet six hours after this
    feature shipped -- frontier=0, render_q=0, render_limit=16, active=0, held=1,895. Every render
    permit free, the lane empty, and thousands of pages parked in a hold that could not release
    them. The crawl looked busy and was doing nothing.

    So assert the invariant directly: drain the lane completely and the counts must be empty.
    """
    async def go():
        e = _engine(lane_max=4)
        for i in range(50):
            e._lane_put(_Item(f"https://deep.example/{i}"))
        while e.render_q.qsize():
            e._lane_release(e.render_q.get_nowait())
        assert not e._lane_hosts, (
            "lane counts survived an empty queue: %r -- the hold can never drain again"
            % e._lane_hosts)

    asyncio.run(go())


def t_an_empty_lane_repairs_a_drifted_count() -> None:
    """The counts are maintained across several call sites, so treat drift as inevitable and make
    it self-correcting. An empty render_q proves every count should be zero."""
    async def go():
        e = _engine(lane_max=4)
        for i in range(20):
            e._lane_put(_Item(f"https://deep.example/{i}"))
        while e.render_q.qsize():                 # take them WITHOUT releasing: simulate the bug
            e.render_q.get_nowait()
        assert e._lane_hosts, "precondition: counts are drifted"
        held_before = e._held_items
        e._release_held_all()
        assert e.render_q.qsize() > 0, "the repair did not re-admit any held work"
        assert e._held_items < held_before, "held work did not move after the repair"

    asyncio.run(go())


def t_the_waiter_releases_what_it_takes() -> None:
    """Source-pinned. The take path in the blocking waiter is the one that was missing its
    release, and a behavioural test cannot reach it without a full engine and two live queues."""
    src = Path(ae.__file__).read_text(encoding="utf-8")
    i = src.index("if is_render and self._render_host_at_cap(")
    j = src.index("self._inflight_items += 1", i)
    assert src.count("_lane_release", i, j) >= 2, (
        "the blocking waiter takes a render item without releasing its lane slot; "
        "_lane_hosts becomes a one-way ratchet and the hold stops draining")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} RENDER-LANE-ADMISSION TESTS PASSED")
