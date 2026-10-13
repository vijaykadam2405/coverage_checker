"""Per-host queue cap: a slow host must not park the tab pool.

Measured pathology: 97% of item timeouts were on hosts already at concurrency 1. HostLimiter.slot
waits for the permit UNBOUNDED, and that wait sits inside the 240s per-item watchdog — so on a
conc=1 host every extra worker idles a tab until the watchdog kills it. One host had 22 tabs blow
the watchdog inside the same 17ms (all released from the queue at once).

Guards: the waiter count is accurate (including on cancellation), the permit is released even when
the body raises, and _worker deflects rather than parking. Also pins the deliberate NON-behaviour —
the watchdog must NOT feed the weak-host tally, since promoting to conc=1 creates more queues.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["CRAWLER_HOST_WEAK_FAILS"] = "3"

from crawler.async_engine import AsyncEngine, HostLimiter  # noqa: E402

ENGINE_SRC = Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py"


def limiter() -> HostLimiter:
    h = HostLimiter.__new__(HostLimiter)          # bypass __init__: it reads a lot of env
    h._sem = {"slow.example.com": asyncio.Semaphore(1)}
    h._cap = {"slow.example.com": 1}
    h._delay = {"slow.example.com": 0.0}
    h._timeout = {}
    h._next_ok = {"slow.example.com": 0.0}
    h._inflight = {"slow.example.com": 0}
    h.peak_inflight = {"slow.example.com": 0}
    h._queued = {}
    h._waiting = 0
    h._progress = None
    h.min_delay = 0.0
    h.base_timeout_s = 30.0
    h.force_careful = set()
    h.overrides = {}

    async def _ensure(host):                       # state is pre-seeded above
        return None
    h._ensure = _ensure
    return h


def t_queue_depth_tracks_waiters() -> None:
    async def run():
        h = limiter()
        url = "https://slow.example.com/a"
        started = asyncio.Event()

        async def holder():
            async with h.slot(url):
                started.set()
                await asyncio.sleep(0.25)

        async def waiter():
            async with h.slot(url):
                pass

        t1 = asyncio.create_task(holder())
        await started.wait()
        assert h.queue_depth("slow.example.com") == 0, "holder counted as a waiter"
        ws = [asyncio.create_task(waiter()) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert h.queue_depth("slow.example.com") == 3, \
            f"expected 3 blocked waiters, got {h.queue_depth('slow.example.com')}"
        await asyncio.gather(t1, *ws)
        assert h.queue_depth("slow.example.com") == 0, "depth leaked after drain"
    asyncio.run(run())


def t_depth_released_on_cancel() -> None:
    """A cancelled waiter must decrement, else depth ratchets up and deflects forever."""
    async def run():
        h = limiter()
        url = "https://slow.example.com/a"
        started = asyncio.Event()

        async def holder():
            async with h.slot(url):
                started.set()
                await asyncio.sleep(0.3)

        async def waiter():
            async with h.slot(url):
                pass

        t1 = asyncio.create_task(holder())
        await started.wait()
        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert h.queue_depth("slow.example.com") == 1
        w.cancel()
        await asyncio.gather(w, return_exceptions=True)
        assert h.queue_depth("slow.example.com") == 0, "cancelled waiter leaked queue depth"
        await t1
    asyncio.run(run())


def t_permit_released_when_body_raises() -> None:
    """The restructure swapped `async with sem` for manual acquire/release — a raising body must
    still release, or the host deadlocks after the first error."""
    async def run():
        h = limiter()
        url = "https://slow.example.com/a"
        try:
            async with h.slot(url):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        await asyncio.wait_for(h.slot(url).__aenter__(), timeout=0.5)  # would hang if leaked
        assert h._inflight["slow.example.com"] >= 0
    asyncio.run(run())


def t_worker_deflects_instead_of_parking() -> None:
    """The regression itself: _worker must check queue_depth and requeue, before the watchdog."""
    tree = ast.parse(ENGINE_SRC.read_text(encoding="utf-8"))
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_worker")
    body = ast.unparse(worker)
    assert "queue_depth" in body, "_worker no longer caps per-host queue depth — pool can park again"
    assert "_schedule_busy" in body, "_worker does not requeue the deflected item"
    assert body.index("queue_depth") < body.index("wait_for"), \
        "queue check must precede the watchdog, else the tab is already committed"


def t_watchdog_does_not_promote() -> None:
    """Deliberate non-behaviour: promoting on watchdog timeouts manufactures more conc=1 queues."""
    tree = ast.parse(ENGINE_SRC.read_text(encoding="utf-8"))
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_worker")
    handlers = [h for n in ast.walk(worker) if isinstance(n, ast.Try) for h in n.handlers
                if h.type is not None and "TimeoutError" in ast.unparse(h.type)]
    assert handlers, "watchdog handler gone"
    assert not any("_note_weak_host" in ast.unparse(h) for h in handlers), (
        "watchdog feeds the weak-host tally again — 97% of item timeouts are on hosts ALREADY at "
        "conc=1, so this promotes healthy hosts into the state that causes the drain"
    )


def t_busy_requeue_preserves_budget() -> None:
    """A busy host is not a failure: retries and deadhost_waits must survive the requeue, or a
    merely-popular host silently drops URLs."""
    src = ast.parse(ENGINE_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.FunctionDef) and n.name == "_schedule_busy")
    body = ast.unparse(fn)
    assert "item.retries + 1" not in body and "retries + 1" not in body, \
        "_schedule_busy burns a retry"
    assert "deadhost_waits + 1" not in body, "_schedule_busy burns a deadhost wait"
    assert "_pending_retries" in body, "_schedule_busy must hold the drain open"


def main() -> None:
    for fn in (t_queue_depth_tracks_waiters, t_depth_released_on_cancel,
               t_permit_released_when_body_raises, t_worker_deflects_instead_of_parking,
               t_watchdog_does_not_promote, t_busy_requeue_preserves_budget):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL HOST-QUEUE CAP TESTS PASSED")



def test_weak_host_watchdog_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
