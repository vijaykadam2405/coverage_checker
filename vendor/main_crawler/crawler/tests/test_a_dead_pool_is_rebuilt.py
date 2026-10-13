"""A pool that outlives its browsers must be rebuilt, not served.

MEASURED ON THE LIVE FLEET, 2026-08-31. Four instances, each up five days, were sent a batch of
real jobs. All four reported it COMPLETE within seconds:

    api-1  run=False  done=6/6  pages=0  fetched=0  kept=0
    api-4  run=False  done=6/6  pages=0  fetched=0  kept=0

and every one of them had died in the same place:

    File "/app/crawler/async_engine.py", line 2425, in start
      await self._open_contexts(browsers)
    playwright._impl._errors.TargetClosedError:
      Browser.new_context: Target page, context or browser has been closed

The shared pool is built ONCE for the life of the process and was never revalidated. That is the
whole point of it -- it is what stopped a node driver leaking per batch -- but it also means that
once the browsers die (a crash, an OOM, a camofox restart, anything), every later batch in that
process fails at start() and the API reports it finished with zero pages. Silent, permanent until
someone restarts the container, and indistinguishable on the dashboard from "this site had nothing".

The failure mode is worse than a crash precisely because it reports SUCCESS. A batch that raised
would be visible; this one produced a green "done 6/6".

Runnable directly:  python tests/test_a_dead_pool_is_rebuilt.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


class _Browser:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_connected(self) -> bool:
        return self._alive

    async def close(self) -> None:
        self._alive = False


class _Chromium:
    def __init__(self, counter: list) -> None:
        self._counter = counter

    async def launch(self, **_kw):
        self._counter.append(1)
        await asyncio.sleep(0)
        return _Browser()


class _Playwright:
    def __init__(self, starts: list, launches: list) -> None:
        starts.append(1)
        self.chromium = _Chromium(launches)

    async def stop(self) -> None:
        pass


def _fake_playwright(starts: list, launches: list):
    import types

    async def _start(_self=None):
        await asyncio.sleep(0)
        return _Playwright(starts, launches)

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: types.SimpleNamespace(start=_start)
    real = sys.modules.get("playwright.async_api")
    sys.modules["playwright.async_api"] = mod

    def undo():
        if real is not None:
            sys.modules["playwright.async_api"] = real
        else:
            sys.modules.pop("playwright.async_api", None)
    return undo


def t_a_disconnected_pool_is_rebuilt() -> None:
    """THE PRODUCTION BUG. Browsers die; the next batch must get working ones."""
    starts, launches = [], []
    undo = _fake_playwright(starts, launches)
    ae.reset_shared_pool()
    try:
        async def go():
            first = await ae._shared_browsers(2)
            for b in first:                      # the browsers die under the process
                b._alive = False
            second = await ae._shared_browsers(2)
            return first, second

        first, second = asyncio.run(go())
        assert all(b.is_connected() for b in second), (
            "a dead pool was served again — every batch in this process will fail at "
            "new_context() and still report done with 0 pages")
        assert second[0] is not first[0], "the same dead browser object came back"
        assert len(starts) == 2 and len(launches) == 4, (
            "expected a genuine rebuild: %d starts, %d launches" % (len(starts), len(launches)))
    finally:
        ae.reset_shared_pool()
        undo()


def t_a_live_pool_is_never_rebuilt() -> None:
    """The leak fix must survive this change. A healthy pool is reused, or we are back to launching
    a browser tree per batch — the exact thing the pool exists to stop."""
    starts, launches = [], []
    undo = _fake_playwright(starts, launches)
    ae.reset_shared_pool()
    try:
        async def go():
            a = await ae._shared_browsers(2)
            b = await ae._shared_browsers(2)
            c = await ae._shared_browsers(2)
            return a, b, c

        a, b, c = asyncio.run(go())
        assert a is b is c, "a live pool was not reused"
        assert len(starts) == 1 and len(launches) == 2, (
            "a healthy pool was rebuilt: %d starts, %d launches" % (len(starts), len(launches)))
    finally:
        ae.reset_shared_pool()
        undo()


def t_a_browser_that_cannot_answer_counts_as_dead() -> None:
    """is_connected() itself can raise once the driver transport is gone. An exception there must
    read as dead, not propagate into the batch — the caller is start(), and an exception escaping
    here is the very failure this check exists to prevent."""
    class _Broken:
        def is_connected(self):
            raise RuntimeError("driver transport closed")

    ae.reset_shared_pool()
    try:
        ae._SHARED_POOL.update(browsers=[_Broken()], W=1)
        assert ae._pool_is_alive() is False, "a browser that raises must count as dead"
    finally:
        ae.reset_shared_pool()


def t_an_empty_pool_is_alive() -> None:
    """Vacuously true, and it matters: the empty case is the FIRST call, which must fall through to
    a normal build rather than take the 'dead pool' branch and log a rebuild warning on every boot."""
    ae.reset_shared_pool()
    assert ae._pool_is_alive() is True


def test_a_dead_pool_is_rebuilt_suite():
    """pytest collects test_* only — without this wrapper the t_* checks above never run."""
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} DEAD-POOL TESTS PASSED")
