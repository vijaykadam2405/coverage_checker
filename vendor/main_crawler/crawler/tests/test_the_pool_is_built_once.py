"""Two batches starting together must not build two pools.

CAUGHT ON THE CANARY, NOT BY THE SUITE. Ninety seconds after the shared pool first went live on
instance 6:

    09:35:58  shared browser pool: 2 browser(s) launched for the life of this process
    09:35:59  shared browser pool: 2 browser(s) launched for the life of this process
    canary : 4 node drivers, 54 chrome
    control: 3 node drivers, 28 chrome

Two batches started within a second of each other. Both read an empty _SHARED_POOL, both awaited
through the driver start and the browser launches, and whichever finished last overwrote the
other's entry — orphaning a complete driver tree for the life of the process.

That is the exact leak the pool was built to remove, walking back in through the door the pool
opened. Worth stating plainly: a check-then-await is not mutual exclusion. Everything between
`if _SHARED_POOL["browsers"]` and the final assignment is suspended, so any number of batches can
pass the check together, and the more instances start batches in parallel the more certain it is.

It is safe to hold a lock across those awaits for the same reason the pool works at all — every
batch now runs on the single loop from _engine_loop.

Runnable directly:  python tests/test_the_pool_is_built_once.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def is_connected(self) -> bool:
        # A real playwright Browser always has this, and _shared_browsers now asks before reusing a
        # cached pool (see test_a_dead_pool_is_rebuilt). A fake without it reads as DEAD, and the
        # pool is then rebuilt on every call — which fails this file's "built once" assertions for a
        # reason that has nothing to do with what they test. Fidelity to the real object, not a
        # concession to the check.
        return not self.closed

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, counter: list) -> None:
        self._counter = counter

    async def launch(self, **_kw):
        self._counter.append(1)
        await asyncio.sleep(0)          # the suspension point that makes the race possible
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, starts: list, launches: list) -> None:
        starts.append(1)
        self.chromium = _FakeChromium(launches)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _install_fake_playwright(monkey_starts: list, monkey_launches: list):
    """Patch async_playwright so no real driver is spawned. Returns the undo callable."""
    import types

    async def _start(_self=None):
        await asyncio.sleep(0)          # driver start is an await in the real thing too
        return _FakePlaywright(monkey_starts, monkey_launches)

    fake_mod = types.ModuleType("playwright.async_api")

    def async_playwright():
        return types.SimpleNamespace(start=_start)

    fake_mod.async_playwright = async_playwright
    real = sys.modules.get("playwright.async_api")
    sys.modules["playwright.async_api"] = fake_mod

    def undo():
        if real is not None:
            sys.modules["playwright.async_api"] = real
        else:
            sys.modules.pop("playwright.async_api", None)
    return undo


def t_concurrent_batches_build_exactly_one_pool() -> None:
    """THE CANARY BUG. Eight batches at once, one driver and one set of browsers."""
    starts: list = []
    launches: list = []
    undo = _install_fake_playwright(starts, launches)
    ae.reset_shared_pool()
    try:
        async def go():
            pools = await asyncio.gather(*[ae._shared_browsers(2) for _ in range(8)])
            return pools

        pools = asyncio.run(go())
        assert len(starts) == 1, (
            "%d Playwright drivers were started for one process; %d of them are orphaned and "
            "leak for the life of the container" % (len(starts), len(starts) - 1))
        assert len(launches) == 2, (
            "%d browsers launched where 2 were asked for — the surplus is unreachable"
            % len(launches))
        first = pools[0]
        for p in pools[1:]:
            assert p is first, "batches were handed different pools; only one can be the real one"
    finally:
        ae.reset_shared_pool()
        undo()


def t_a_later_batch_reuses_the_pool_without_relaunching() -> None:
    """The ordinary path still costs nothing: a second batch launches no browser at all."""
    starts: list = []
    launches: list = []
    undo = _install_fake_playwright(starts, launches)
    ae.reset_shared_pool()
    try:
        async def go():
            a = await ae._shared_browsers(2)
            b = await ae._shared_browsers(2)
            return a, b

        a, b = asyncio.run(go())
        assert a is b, "the second batch got a different pool"
        assert len(starts) == 1 and len(launches) == 2, (
            "the second batch relaunched: %d starts, %d launches" % (len(starts), len(launches)))
    finally:
        ae.reset_shared_pool()
        undo()


def t_reset_drops_the_lock_too() -> None:
    """reset_shared_pool is used between tests, and each test runs on its own loop. A lock left
    behind from a previous loop would be awaited from a loop that never created it."""
    ae.reset_shared_pool()
    assert ae._POOL_LOCK is None, "reset left a lock bound to the previous test's loop"


def test_the_pool_is_built_once_suite():
    """pytest collects test_* only — without this wrapper the t_* checks above never run."""
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} POOL-BUILT-ONCE TESTS PASSED")
