"""A start() that dies partway through must not leave a Playwright driver running.

MEASURED 2026-08-19, two days after the teardown-kill fix shipped. The leak came back in full:

    instance 6 : 595 chrome processes, 54 TOP-LEVEL browsers under 31 node drivers
    fleet      : 2,539 chrome processes
    kills fired: 19 on that instance — the teardown fix WAS live and WAS working

The teardown fix was never going to be enough, because the leaking path never reaches teardown.
`AsyncEngine.start()` forks the node driver, then launches W browsers in a loop, and it has no
exception handling of its own. Its caller (crawl_batch_async) does:

    eng = AsyncEngine(...)
    await eng.start()          # <-- OUTSIDE the guard
    try:
        ctxs = await eng.run(jobs, forward)
    finally:
        await eng.shutdown()   # <-- never runs if start() raised

`chromium.launch` is precisely the call already measured blowing its 180s timeout when the box is
loaded (load average was 235 at the time of this measurement). Every one of those raises escapes
start(), skips shutdown(), and drops the only reference to `_pw` — so the driver and whatever
browsers it had already launched are unreachable forever. No later pass can reclaim them.

The corroborating arithmetic: 31 drivers should hold 62 browsers at W=2, and only 54 exist. The
8-browser shortfall is drivers that died mid-loop — the fingerprint of a partial start.

Runnable directly:  python tests/test_a_failed_start_leaks_nothing.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler import async_engine as ae  # noqa: E402


class _Ctx:
    def __init__(self, browser): self.browser = browser; self.closed = False
    async def new_page(self): return _Page()
    async def route(self, *a, **k): return None
    async def add_init_script(self, *a, **k): return None
    async def close(self): self.closed = True


class _Page:
    async def close(self): return None


class _Browser:
    def __init__(self): self.closed = False
    async def new_context(self, **k): return _Ctx(self)
    async def close(self): self.closed = True


class _Chromium:
    """Launches `ok_count` browsers, then raises — the measured launch-timeout shape."""
    def __init__(self, ok_count): self.ok_count = ok_count; self.launched = []
    async def launch(self, **k):
        if len(self.launched) >= self.ok_count:
            raise RuntimeError("BrowserType.launch: Timeout 180000ms exceeded")
        b = _Browser(); self.launched.append(b); return b


class _PW:
    def __init__(self, ok_count):
        self.chromium = _Chromium(ok_count)
        self.stopped = False
    async def stop(self): self.stopped = True


_REAL_PLAYWRIGHT = {k: v for k, v in sys.modules.items()
                    if k == "playwright" or k.startswith("playwright.")}


def _restore_playwright() -> None:
    """Put the REAL playwright modules back.

    _install_fake_playwright overwrites sys.modules process-wide and nothing used to undo it, so
    every later test that wanted a real browser silently got this file's fake instead — including
    the real-browser pool check, which passed alone and failed in the full run. Module-level
    monkeypatching outlives the test that did it unless something puts it back.

    EVERY playwright entry, not just the two that were replaced. Dropping only "playwright" and
    "playwright.async_api" leaves the real submodules cached under a parent that no longer exists,
    and the next real import dies on `module 'playwright' has no attribute '_impl'`.
    """
    for k in [k for k in sys.modules
              if k == "playwright" or k.startswith("playwright.")]:
        del sys.modules[k]
    sys.modules.update(_REAL_PLAYWRIGHT)


@pytest.fixture(autouse=True)
def _unpatch_playwright():
    yield
    _restore_playwright()


def _install_fake_playwright(pw):
    mod = types.ModuleType("playwright.async_api")

    class _Starter:
        async def start(self): return pw

    mod.async_playwright = lambda: _Starter()
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod
    sys.modules["playwright"] = pkg
    sys.modules["playwright.async_api"] = mod


def _engine(W=2, T=1):
    # Forget the process-wide pool first. Without this a later test borrows the pool an earlier one
    # built, chromium.launch is never called, and "the second launch fails" silently tests nothing —
    # which is exactly what happened when the shared pool landed.
    ae.reset_shared_pool()
    """Only the attributes start() touches — constructing the real thing would drag in the seed
    corpus, the robots cache and a HostLimiter for no added coverage."""
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e.W, e.T = W, T
    e.browsers, e.tabs = [], []
    e._pw, e._pw_pids = None, set()
    e._block_assets = False
    e.seed = types.SimpleNamespace(capture_defaults={"user_agent": "test-agent"})
    return e


def t_a_launch_failure_tears_down_what_it_already_built() -> None:
    """THE BUG. The first browser launches, the second times out. Without cleanup inside start(),
    that first browser and the driver behind it are orphaned the moment the exception escapes."""
    pw = _PW(ok_count=1)
    _install_fake_playwright(pw)
    eng = _engine(W=2, T=1)

    raised = False
    try:
        asyncio.run(eng.start())
    except RuntimeError:
        raised = True

    assert raised, "start() swallowed a launch failure — the caller must still see it fail"
    assert pw.chromium.launched[0].closed, (
        "the browser launched before the failure was left RUNNING — this is the leak, one "
        "orphaned browser per failed start")
    assert pw.stopped, (
        "a HALF-BUILT pool was left behind. The driver is process-wide and normally outlives every "
        "batch, but a pool that failed midway must never be published: a later batch would borrow "
        "a short browser list and the missing browsers would never appear.")
    assert eng.browsers == [], "start() left dead browser handles on the engine"
    assert eng._pw is None, "start() left a stopped driver referenced as live"


def t_a_clean_start_is_untouched() -> None:
    """Cleanup must only run on the failure path; a good start keeps its browsers."""
    pw = _PW(ok_count=2)
    _install_fake_playwright(pw)
    eng = _engine(W=2, T=1)
    asyncio.run(eng.start())

    assert len(eng.browsers) == 2, eng.browsers
    assert len(eng.tabs) == 2, eng.tabs
    assert not pw.stopped, "a successful start stopped the process-wide driver"
    assert all(not b.closed for b in pw.chromium.launched), "a successful start closed its browsers"


def t_the_caller_tears_down_even_when_start_fails() -> None:
    """Defence in depth, pinned against the source: start() must sit INSIDE the try whose finally
    calls shutdown(). Belt (cleanup inside start) and braces (the caller's guard) — because the
    engine object holds the only reference to the driver, so a miss here is unrecoverable."""
    import re
    src = (ROOT / "crawler" / "async_engine.py").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    m = re.search(r"eng\s*=\s*AsyncEngine\((.*?)\n(.*?)await eng\.shutdown\(\)", code, re.S)
    assert m, "could not locate the engine lifecycle block"
    block = m.group(2)
    start_at = block.index("await eng.start()")
    try_at = block.index("try:")
    assert try_at < start_at, (
        "await eng.start() is still OUTSIDE the try/finally that calls shutdown(); a partial "
        "start leaks its driver with no path left to reclaim it")


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d FAILED-START TESTS PASSED" % len(fns))


def test_failed_start_suite() -> None:
    main()


if __name__ == "__main__":
    main()
