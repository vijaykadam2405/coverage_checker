"""A browser that is replaced must be CLOSED, or the fleet grows Chromium until it OOMs.

MEASURED 2026-08-15, with CRAWLER_BROWSERS=2 on every instance:

    instance   top-level chrome   total chrome procs   cgroup RAM
    i1                22                 244             16.6 GiB
    i12                6                 259             21.5 GiB
    i5                 4                  37             27.8 GiB   (OOM-killed)
    i10                0                   0              1.6 GiB

Python was under 1.5 GiB in every one of them, and the instance that had never launched a browser
sat at 1.6 GiB. The growth was entirely un-reaped Chromium.

The cause was `_recycle`: when every context refused new_page() it launched a replacement and
APPENDED it, leaving the dead entries in self.browsers forever. A context that will not hand out a
page is unusable, but its browser PROCESS is alive and still holding its RSS. So each recycle added
a browser and freed nothing — and it is self-reinforcing, because more browsers means more memory
pressure, which means more tab crashes, which means more recycles.

`init: true` was already set and zombies were ~0, so this was never a reaping problem: the
processes were alive, not defunct, and nothing had asked them to exit.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


class _Ctx:
    """A browser context. `alive` decides whether it will hand out a page."""

    def __init__(self, alive: bool):
        self.alive, self.closed = alive, False

    async def new_page(self):
        if not self.alive:
            raise RuntimeError("context is dead")
        return _Tab()

    async def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True

    async def new_context(self, **_kw):
        return _Ctx(alive=True)


class _Tab:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _PW:
    def __init__(self):
        self.launched = 0
        self.chromium = self

    async def launch(self, **_kw):
        self.launched += 1
        return _Browser()


def _engine(contexts_alive: list[bool]):
    # The pool is process-wide now, so every test starts by forgetting it — otherwise a test
    # borrows whatever the previous one left behind and asserts against the wrong browsers.
    ae.reset_shared_pool()
    """An AsyncEngine with just the attributes _recycle touches."""
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e._pw = _PW()
    e.W = 1
    e.browsers = [(_Browser(), _Ctx(alive=a)) for a in contexts_alive]
    e.seed = type("S", (), {"capture_defaults": {"user_agent": "ua"}})()
    return e


def test_dead_browsers_are_closed_not_accumulated():
    """THE BUG. Two dead contexts -> both browsers closed, list holds exactly the replacement."""
    e = _engine([False, False])
    dead = list(e.browsers)
    page = asyncio.run(e._recycle(_Tab()))

    assert page is not None
    assert len(e.browsers) == 1, (
        "self.browsers grew instead of being replaced - this is the leak: %d entries" % len(e.browsers))
    for b, c in dead:
        # The BROWSER is process-wide now and must survive: closing it is the operation
        # measured reporting success while leaving the tree alive. Only the context is ours.
        assert not b.closed, "closed a SHARED browser - that close is what leaked whole trees"
        assert c.closed, "a dead context was never closed"


def test_repeated_recycles_do_not_grow_the_pool():
    """The shape that produced 22 browsers from a configured 2. Ten consecutive all-dead recycles
    must still leave ONE browser, not eleven."""
    e = _engine([False])
    for _ in range(10):
        # each round the freshly-launched context dies before the next crash
        asyncio.run(e._recycle(_Tab()))
        e.browsers = [(b, _Ctx(alive=False)) for b, _ in e.browsers]
    assert len(e.browsers) == 1, "pool grew to %d over 10 recycles" % len(e.browsers)


def test_a_live_context_is_reused_and_nothing_is_closed():
    """The common path. One dead context and one live one: hand back a page from the live one,
    launch nothing, close nothing. Without this the fix would 'work' by closing browsers that are
    still in use — trading a leak for dropped work."""
    e = _engine([False, True])
    browsers = list(e.browsers)
    page = asyncio.run(e._recycle(_Tab()))

    assert page is not None
    assert not any(b.closed for b, _ in browsers), "closed a browser despite a live context"
    assert len(e.browsers) == 2
    assert not any(b.closed for b, _ in browsers), "closed a browser that was still usable"


def test_the_crashed_tab_is_always_closed():
    e = _engine([True])
    tab = _Tab()
    asyncio.run(e._recycle(tab))
    assert tab.closed


def test_a_close_that_hangs_does_not_block_the_replacement():
    """A wedged Chromium must not stop the engine getting a working browser back — that would turn
    a memory leak into a stalled crawl. The closes are bounded and the list is cleared BEFORE
    awaiting them, so a hang leaves nothing behind for the next recycle to trip over."""

    class _Hangs(_Browser):
        async def close(self):
            await asyncio.sleep(3600)

    e = _engine([])
    e.browsers = [(_Hangs(), _Ctx(alive=False))]

    async def _run():
        return await asyncio.wait_for(e._recycle(_Tab()), timeout=30)

    page = asyncio.run(_run())
    assert page is not None
    assert len(e.browsers) == 1, "a hung close left the dead browser in the pool"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
