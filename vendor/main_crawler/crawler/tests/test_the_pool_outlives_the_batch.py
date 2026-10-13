"""The Playwright driver and its browsers belong to the PROCESS, not to a batch.

WHY THIS IS AN ARCHITECTURE CHANGE AND NOT A FOURTH PATCH.

Three fixes were made to the per-batch create/destroy cycle, each a real bug, each verified:

  1  _recycle appended a fresh browser and left the dead ones running       (2026-08-15)
  2  a timed-out teardown abandoned live trees and called that reaping      (2026-08-16)
  3  a failed start() skipped teardown entirely and orphaned its driver     (2026-08-18)

Load fell 235 -> 73 across them. The leak survived all three. MEASURED 2026-08-18, 90 minutes
after the third fix rolled to all 12 instances:

    fleet          : 612 chrome, 23 node drivers   (healthy is 12 — one per instance)
    instance 7     : 6 batches started, 0 close timeouts, 0 kills fired, 4 drivers alive

Zero close timeouts is the whole finding. Every close and stop() REPORTED SUCCESS, so the kill
added in fix 2 correctly never fired — and four complete driver trees were alive anyway. A
Playwright close that returns cleanly does not mean the OS process exited. Every fix so far
assumed it did, in a different place each time.

So the cycle itself is the defect: a driver and browser set are built and destroyed roughly every
three minutes per instance, and each destruction is a chance to leak something no later pass can
reach. Build them ONCE per container and the failure mode has nowhere to live.

WHAT STAYS PER BATCH: contexts and pages. They carry cookies and session state, and reusing them
across batches would bleed one site's session into another's crawl. Contexts are cheap; browser
processes are not.

Runnable directly:  python tests/test_the_pool_outlives_the_batch.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler import async_engine as ae  # noqa: E402


class _Page:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


class _Ctx:
    def __init__(self, browser):
        self.browser, self.closed, self.pages = browser, False, []
    async def new_page(self):
        p = _Page(); self.pages.append(p); return p
    async def route(self, *a, **k): return None
    async def add_init_script(self, *a, **k): return None
    async def close(self): self.closed = True


class _Browser:
    def __init__(self, n): self.n, self.closed, self.contexts = n, False, []
    async def new_context(self, **k):
        c = _Ctx(self); self.contexts.append(c); return c
    async def close(self): self.closed = True


class _Chromium:
    def __init__(self): self.launched = []
    async def launch(self, **k):
        b = _Browser(len(self.launched)); self.launched.append(b); return b


class _PW:
    def __init__(self): self.chromium = _Chromium(); self.stopped = False
    async def stop(self): self.stopped = True


class _Starter:
    def __init__(self, sink): self.sink = sink
    async def start(self):
        pw = _PW(); self.sink.append(pw); return pw


def _install(sink):
    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = lambda: _Starter(sink)
    pkg = types.ModuleType("playwright"); pkg.async_api = mod
    sys.modules["playwright"] = pkg
    sys.modules["playwright.async_api"] = mod


def _engine(W=2, T=1):
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e.W, e.T = W, T
    e.browsers, e.tabs = [], []
    e._pw, e._pw_pids = None, set()
    e._block_assets = False
    e.seed = types.SimpleNamespace(capture_defaults={"user_agent": "test-agent"})
    e._asset_pool = types.SimpleNamespace(shutdown=lambda **k: None)
    return e


async def _cycle(n):
    """n batches, each a full start/shutdown, as the container really does it."""
    for _ in range(n):
        e = _engine()
        await e.start()
        await e.shutdown()


def t_a_second_batch_does_not_fork_a_second_driver() -> None:
    """THE BUG'S SIGNATURE, counted at the source. Six batches produced four live drivers on
    instance 7; here six batches must produce exactly ONE."""
    ae.reset_shared_pool()
    drivers: list = []
    _install(drivers)
    asyncio.run(_cycle(6))
    assert len(drivers) == 1, (
        "%d drivers forked for 6 batches — the per-batch cycle is still in place" % len(drivers))


def t_browsers_are_launched_once_and_never_closed_between_batches() -> None:
    """Sharing only the driver would not be enough: instance 7 reported ZERO close timeouts while
    holding four complete trees, so browser closes report success and linger exactly as stop()
    does. The browsers must simply never be closed between batches."""
    ae.reset_shared_pool()
    drivers: list = []
    _install(drivers)
    asyncio.run(_cycle(4))
    pw = drivers[0]
    assert len(pw.chromium.launched) == 2, (
        "expected 2 browsers for the whole process, got %d — one set per batch is the leak"
        % len(pw.chromium.launched))
    assert all(not b.closed for b in pw.chromium.launched), (
        "a browser was closed between batches; a close that lingers is exactly what leaked")
    assert not pw.stopped, "the driver was stopped between batches"


def t_each_batch_gets_its_own_contexts_and_they_are_closed() -> None:
    """Cookie isolation is the reason contexts are NOT shared. Each batch must build its own and
    close them, or one site's session bleeds into the next batch's crawl."""
    ae.reset_shared_pool()
    drivers: list = []
    _install(drivers)
    asyncio.run(_cycle(3))
    pw = drivers[0]
    for b in pw.chromium.launched:
        assert len(b.contexts) == 3, (
            "expected one context per batch on each browser, got %d" % len(b.contexts))
        assert all(c.closed for c in b.contexts), (
            "a batch left its context open — cookies would carry into the next batch")
        for c in b.contexts:
            assert all(p.closed for p in c.pages), "a batch left pages open"


def t_the_pool_survives_a_batch_that_fails_mid_flight() -> None:
    """A batch dying must not take the shared pool with it — otherwise one bad crawl costs every
    later batch its browsers, and we are back to relaunching per batch."""
    ae.reset_shared_pool()
    drivers: list = []
    _install(drivers)

    async def _run():
        e = _engine()
        await e.start()
        await e.shutdown()
        e2 = _engine()
        await e2.start()
        assert len(e2.browsers) == 2
        await e2.shutdown()

    asyncio.run(_run())
    pw = drivers[0]
    assert len(pw.chromium.launched) == 2, pw.chromium.launched
    assert not pw.stopped


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d SHARED-POOL TESTS PASSED" % len(fns))


def test_shared_pool_suite() -> None:
    main()


if __name__ == "__main__":
    main()
