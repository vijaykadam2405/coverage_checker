"""The one check the unit suite structurally cannot make: a REAL browser, reused across batches.

The shared pool was shipped once and reverted, and 1,012 green tests did not stop it. They could
not have. Every one of them drives a fake browser, and a fake does not care which event loop
created it — but a real Playwright object is bound to its loop and raises the moment it is touched
from another:

    ValueError: Browser.new_context: The future belongs to a different loop than the one
                specified as the loop argument

100 to 176 times per instance, discovered in production, on the fleet.

So the property is only observable with a real driver behind it: take the pool from one loop
submission, take it again from a second, and open a context on it. Under the old per-batch
asyncio.run() the second open raises. Nothing else in the suite can tell the difference.

Skipped automatically when no chromium binary is installed — CI runs without one on purpose (692
tests pass with no browser), so this must not fail there. Run it wherever a browser exists, and in
the container before deploying:

    python tests/test_pool_reuse_with_a_real_browser.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


def _has_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            b.close()
        return True
    except Exception:
        return False


HAVE_BROWSER = _has_browser()
skip_no_browser = pytest.mark.skipif(
    not HAVE_BROWSER, reason="no chromium binary — CI runs without one by design")


def _batch(coro):
    """Submit to the process-wide engine loop exactly the way run_batch_async does."""
    return asyncio.run_coroutine_threadsafe(coro, ae._engine_loop()).result(timeout=180)


@skip_no_browser
def test_a_second_batch_can_use_the_first_batch_s_browsers():
    """THE REVERTED BUG, pinned with a real driver.

    Two submissions to the loop, one pool. The second must be able to open a context on a browser
    the first one launched — that is what "shared" means, and it is exactly what failed in
    production while the unit suite stayed green.
    """
    ae.reset_shared_pool()
    ae.reset_engine_loop()
    try:
        async def first():
            browsers = await ae._shared_browsers(1)
            ctx = await browsers[0].new_context()
            await ctx.close()
            return [id(b) for b in browsers]

        async def second():
            browsers = await ae._shared_browsers(1)
            # THE LINE THAT RAISED. Loop-bound objects reached from a second loop fail here.
            ctx = await browsers[0].new_context()
            page = await ctx.new_page()
            await page.close()
            await ctx.close()
            return [id(b) for b in browsers]

        a = _batch(first())
        b = _batch(second())
        assert a == b, ("the second batch got different browsers (%s vs %s) — the pool is not "
                        "shared, so the per-batch launch/teardown cycle is still there" % (a, b))
    finally:
        _batch(_close_pool())
        ae.reset_engine_loop()


@skip_no_browser
def test_repeated_batches_do_not_grow_the_driver_count():
    """The leak itself, measured rather than reasoned about.

    Fleet history: one node driver and its browser tree leaked per BATCH, reaching 31-deep on
    instance 6 over two days. Three fixes each addressed a real bug and none stopped it, because a
    Playwright close can return success while the OS process lives. A pool built once cannot leak
    per batch — so the count after five batches must equal the count after one.
    """
    ae.reset_shared_pool()
    ae.reset_engine_loop()
    try:
        async def one():
            browsers = await ae._shared_browsers(1)
            ctx = await browsers[0].new_context()
            await ctx.close()

        _batch(one())
        after_first = len(ae._driver_children())
        for _ in range(4):
            _batch(one())
        after_five = len(ae._driver_children())
        assert after_five == after_first, (
            "drivers grew %d -> %d over five batches; the per-batch cycle is back"
            % (after_first, after_five))
    finally:
        _batch(_close_pool())
        ae.reset_engine_loop()


async def _close_pool():
    """Tear the real pool down. Production never does this — the browsers die with the container,
    which is the only teardown the kernel actually guarantees — but a test process that leaves
    chromium running would leak into the developer's machine and into the next test."""
    for b in list(ae._SHARED_POOL.get("browsers") or []):
        try:
            await b.close()
        except Exception:
            pass
    pw = ae._SHARED_POOL.get("pw")
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    ae.reset_shared_pool()


if __name__ == "__main__":
    if not HAVE_BROWSER:
        print("SKIPPED — no chromium binary available")
        raise SystemExit(0)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} REAL-BROWSER POOL TESTS PASSED")
