"""One host must not be able to hold the whole global render pool.

Measured pathology: the adaptive gate is acquired BEFORE the per-host slot, so a tab waiting for a
render permit is invisible to per-host accounting. czdefence.com held the pool while every other
host's render-needing items sat on the gate until the 240s item watchdog killed them — 99 timeouts
in 3 minutes with ZERO HTTP requests issued, and the only host still making progress was one served
entirely by httpx (no render permit needed).

Guards the two halves of the fix: a bounded wait (RenderBusy instead of parking) and a per-host cap.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["CRAWLER_HOST_RENDER_MAX"] = "2"
os.environ["CRAWLER_RENDER_WAIT_S"] = "0.2"

from crawler.async_engine import AdaptiveGate, AsyncEngine, RenderBusy  # noqa: E402

ENGINE_SRC = Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py"


def engine() -> AsyncEngine:
    e = AsyncEngine.__new__(AsyncEngine)          # __init__ builds browsers; we only need the gate
    e.adaptive = AdaptiveGate(initial=1, min_limit=1, max_limit=1)
    e._render_hosts = {}
    return e


def t_bounded_wait_raises_instead_of_parking() -> None:
    async def run():
        gate = AdaptiveGate(initial=1, min_limit=1, max_limit=1)
        held = asyncio.Event()

        async def holder():
            async with gate.slot():
                held.set()
                await asyncio.sleep(0.5)

        t = asyncio.create_task(holder())
        await held.wait()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            async with gate.slot(timeout=0.15):
                raise AssertionError("acquired a permit that was already held")
        except RenderBusy:
            pass
        waited = loop.time() - t0
        assert waited < 0.4, f"waited {waited:.2f}s — did not bound the wait"
        await t
    asyncio.run(run())


def t_unbounded_still_waits() -> None:
    """timeout=None must keep the original blocking behaviour (the 4xx retry path relies on it)."""
    async def run():
        gate = AdaptiveGate(initial=1, min_limit=1, max_limit=1)
        held = asyncio.Event()
        order = []

        async def holder():
            async with gate.slot():
                held.set()
                await asyncio.sleep(0.2)
                order.append("holder")

        async def waiter():
            async with gate.slot():
                order.append("waiter")

        t = asyncio.create_task(holder())
        await held.wait()
        await asyncio.gather(t, waiter())
        assert order == ["holder", "waiter"], f"unbounded acquire did not queue: {order}"
    asyncio.run(run())


def t_per_host_cap_deflects() -> None:
    async def run():
        e = engine()
        e.adaptive = AdaptiveGate(initial=8, min_limit=1, max_limit=8)   # pool is NOT the limit here
        rel = asyncio.Event()

        async def hold(host):
            async with e._render_permit(host):
                await rel.wait()

        a = asyncio.create_task(hold("busy.example.com"))
        b = asyncio.create_task(hold("busy.example.com"))
        await asyncio.sleep(0.05)
        assert e._render_hosts["busy.example.com"] == 2
        try:
            async with e._render_permit("busy.example.com"):
                raise AssertionError("3rd permit granted despite CRAWLER_HOST_RENDER_MAX=2")
        except RenderBusy:
            pass
        # a different host must still get in — that's the whole point of the cap
        async with e._render_permit("other.example.com"):
            pass
        rel.set()
        await asyncio.gather(a, b)
        assert "busy.example.com" not in e._render_hosts, "per-host counter leaked after release"
    asyncio.run(run())


def t_counter_released_on_renderbusy() -> None:
    """A deflected acquire must not leave the host's count incremented, or the host locks itself out."""
    async def run():
        e = engine()                                   # pool size 1
        rel = asyncio.Event()

        async def hold():
            async with e._render_permit("h.example.com"):
                await rel.wait()

        t = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        for _ in range(3):                             # each times out on the saturated pool
            try:
                async with e._render_permit("h.example.com"):
                    pass
            except RenderBusy:
                pass
        assert e._render_hosts.get("h.example.com", 0) == 1, \
            f"leaked: {e._render_hosts.get('h.example.com')} (deflected acquires never decremented)"
        rel.set()
        await t
    asyncio.run(run())


def t_worker_requeues_and_refunds_budget() -> None:
    tree = ast.parse(ENGINE_SRC.read_text(encoding="utf-8"))
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_worker")
    handlers = [h for n in ast.walk(worker) if isinstance(n, ast.Try) for h in n.handlers
                if h.type is not None and "RenderBusy" in ast.unparse(h.type)]
    assert handlers, "_worker no longer handles RenderBusy — it will kill the worker task"
    body = ast.unparse(handlers[0])
    assert "unreserve" in body, "deflected item does not refund its page budget — budget leaks"
    # Pinned to the PROPERTY, not one helper's name: the requeue moved from _schedule_busy to
    # _requeue_render and this assertion went on failing green because nothing collected the file.
    # Accept either spelling so a rename is not a false alarm, but keep it specific enough that
    # DROPPING the item still fails.
    assert any(k in body for k in ("_schedule_busy", "_requeue_render", "requeue")), (
        f"deflected item is dropped instead of requeued — handler body: {body!r}")


def t_render_path_does_not_swallow_renderbusy() -> None:
    """The render call sits inside `except Exception` (render-crash fallback). If RenderBusy is
    caught there it becomes a fake fetch failure and the item is never requeued."""
    src = ENGINE_SRC.read_text(encoding="utf-8")
    i = src.index("_render_permit(host)")
    after = src[i:i + 900]
    assert "except RenderBusy:" in after and after.index("except RenderBusy:") < after.index("except Exception"), \
        "RenderBusy must be re-raised before the generic render-crash handler"


# ── the LANE, one layer above the permit ─────────────────────────────────────────────────────
# The cap above stops a host holding every render PERMIT. It does not stop that host's items
# filling every render LANE SLOT: a worker takes a slot, calls _render_permit, gets RenderBusy,
# releases the slot and requeues on a 3s timer. Measured on the live crawl — 128 of 128 slots held
# by condornaoletal.com.br, 385,296 deflections against 1,871 fetched pages, and 52 of 64 browser
# permits idle the whole time. Raising CRAWLER_HOST_RENDER_MAX only moved the number, because
# nothing looked at the host until AFTER the slot was taken. These pin the pull-time check.

def _lane_engine(render_hosts=None) -> AsyncEngine:
    e = AsyncEngine.__new__(AsyncEngine)
    e.render_q = asyncio.Queue()
    e._render_hosts = dict(render_hosts or {})
    return e


class _LaneItem:
    def __init__(self, url):
        self.url = url


def t_a_capped_host_is_skipped_at_pull_time() -> None:
    """THE fix. A capped host's item must not be pulled into a slot only to bounce out of it."""
    os.environ["CRAWLER_HOST_RENDER_MAX"] = "8"
    e = _lane_engine({"hog.example": 8})
    for i in range(5):
        e.render_q.put_nowait(_LaneItem(f"https://hog.example/{i}"))
    e.render_q.put_nowait(_LaneItem("https://free.example/a"))
    got = e._pull_render()
    assert got is not None, "nothing pulled although a free host had work queued"
    assert "free.example" in got.url, f"pulled the capped host anyway: {got.url}"
    assert e.render_q.qsize() == 5, f"skipped items were lost: {e.render_q.qsize()}"
    os.environ["CRAWLER_HOST_RENDER_MAX"] = "2"


def t_skipped_items_keep_their_order() -> None:
    """Rotation must not shuffle: a resumed crawl depends on breadth-first ordering."""
    os.environ["CRAWLER_HOST_RENDER_MAX"] = "8"
    e = _lane_engine({"hog.example": 8})
    for i in range(4):
        e.render_q.put_nowait(_LaneItem(f"https://hog.example/{i}"))
    e.render_q.put_nowait(_LaneItem("https://free.example/a"))
    e._pull_render()
    left = []
    while not e.render_q.empty():
        left.append(e.render_q.get_nowait().url)
    assert left == [f"https://hog.example/{i}" for i in range(4)], left
    os.environ["CRAWLER_HOST_RENDER_MAX"] = "2"


def t_all_capped_yields_nothing_rather_than_a_doomed_slot() -> None:
    e = _lane_engine({"a.example": 2, "b.example": 2})          # module default cap is 2
    for i in range(6):
        e.render_q.put_nowait(_LaneItem(f"https://{'ab'[i % 2]}.example/{i}"))
    assert e._pull_render() is None, "took an item whose host provably cannot render"
    assert e.render_q.qsize() == 6, "items lost while scanning"


def t_the_scan_is_bounded() -> None:
    """A deep queue of one capped host must not be walked in full on every pull."""
    os.environ["CRAWLER_RENDER_SCAN"] = "10"
    e = _lane_engine({"hog.example": 2})
    for i in range(500):
        e.render_q.put_nowait(_LaneItem(f"https://hog.example/{i}"))
    assert e._pull_render() is None
    assert e.render_q.qsize() == 500, "items lost"
    os.environ.pop("CRAWLER_RENDER_SCAN", None)


def t_a_free_host_is_taken_immediately() -> None:
    e = _lane_engine({})
    e.render_q.put_nowait(_LaneItem("https://free.example/a"))
    got = e._pull_render()
    assert got is not None and "free.example" in got.url
    assert e.render_q.qsize() == 0


def t_an_unparseable_url_is_handed_on_not_stranded() -> None:
    """A URL we cannot parse must not become permanently unrenderable — let _render_permit judge."""
    e = _lane_engine({})
    e.render_q.put_nowait(_LaneItem("not-a-url"))
    assert e._pull_render() is not None, "an unparseable URL was skipped rather than handed on"


def main() -> None:
    for fn in (t_bounded_wait_raises_instead_of_parking, t_unbounded_still_waits,
               t_per_host_cap_deflects, t_counter_released_on_renderbusy,
               t_worker_requeues_and_refunds_budget, t_render_path_does_not_swallow_renderbusy,
               t_a_capped_host_is_skipped_at_pull_time, t_skipped_items_keep_their_order,
               t_all_capped_yields_nothing_rather_than_a_doomed_slot, t_the_scan_is_bounded,
               t_a_free_host_is_taken_immediately,
               t_an_unparseable_url_is_handed_on_not_stranded):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL RENDER FAIRNESS TESTS PASSED")



def test_render_fairness_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
