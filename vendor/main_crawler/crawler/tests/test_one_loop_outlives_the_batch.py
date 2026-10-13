"""Every batch must run on the SAME event loop.

This is the prerequisite for a process-wide browser pool, and the reason the first attempt at one
had to be reverted. Playwright objects are bound to the loop that created them, so with
`asyncio.run()` per batch the second batch inherited browsers from a dead loop and every single
context raised:

    ValueError: Browser.new_context: The future belongs to a different loop than the one
                specified as the loop argument

100 to 176 times per instance across the fleet. The pool itself was right — chrome processes fell
2,539 -> 237 and load 235 -> 11 before the errors surfaced. It was the loop underneath it that was
wrong, and no amount of care in the pool could have fixed that.

Unit tests could not catch it, which is the part worth remembering: the pool's tests drive fakes,
and a fake browser does not care which loop it was made on. The property that actually mattered was
never about browsers at all — it is that the loop outlives the batch. So that is what this pins.

Runnable directly:  python tests/test_one_loop_outlives_the_batch.py
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402

SRC = Path(ae.__file__).read_text(encoding="utf-8")


def _on_loop(loop):
    """Run a trivial coroutine on *loop* and report the loop it actually ran on."""
    async def _who():
        return asyncio.get_running_loop()
    return asyncio.run_coroutine_threadsafe(_who(), loop).result(timeout=10)


def t_the_loop_is_reused_across_batches() -> None:
    """THE WHOLE POINT. Two batches, one loop — otherwise a shared pool cannot exist."""
    ae.reset_engine_loop()
    try:
        first = ae._engine_loop()
        second = ae._engine_loop()
        assert first is second, "a second batch got a different loop; a shared pool would die here"
        assert _on_loop(first) is first, "work did not actually run on the loop we were handed"
        assert _on_loop(second) is first, "the second batch's work ran somewhere else"
    finally:
        ae.reset_engine_loop()


def t_the_loop_survives_a_failing_batch() -> None:
    """A batch that raises must not take the loop with it.

    The old shape was accidentally safe here: each batch had its own loop, so a crash cost nothing
    shared. A process-wide loop makes one bad batch able to wedge every later one, so this is a new
    failure mode the change introduces and has to answer for.
    """
    ae.reset_engine_loop()
    try:
        loop = ae._engine_loop()

        async def _boom():
            raise RuntimeError("batch exploded")

        fut = asyncio.run_coroutine_threadsafe(_boom(), loop)
        try:
            fut.result(timeout=10)
        except RuntimeError:
            pass
        else:                                   # pragma: no cover
            raise AssertionError("the failure was swallowed before reaching the caller")

        assert not loop.is_closed(), "a failed batch closed the shared loop"
        assert _on_loop(loop) is loop, "the loop stopped serving work after a failure"
        assert ae._engine_loop() is loop, "a later batch was handed a different loop"
    finally:
        ae.reset_engine_loop()


def t_a_dead_loop_is_replaced_not_reused() -> None:
    """If the loop does die, the next batch gets a working one rather than a closed one."""
    ae.reset_engine_loop()
    try:
        first = ae._engine_loop()
        first.call_soon_threadsafe(first.stop)
        for _ in range(200):                    # let run_forever return, then close it
            if not first.is_running():
                break
            import time
            time.sleep(0.01)
        first.close()
        second = ae._engine_loop()
        assert second is not first, "a closed loop was handed out again"
        assert _on_loop(second) is second, "the replacement loop does not run work"
    finally:
        ae.reset_engine_loop()


def t_the_batch_entry_point_no_longer_owns_a_loop() -> None:
    """Source-pinned: run_batch_async must not create a loop of its own.

    Behavioural tests above would still pass if someone reintroduced `asyncio.run()` alongside the
    shared loop for one code path, and that path's browsers would then be loop-orphaned again —
    silently, because nothing fails until a browser is reused.
    """
    i = SRC.index("def run_batch_async(")
    m = re.search(r"\n(?:def |async def |# ─)", SRC[i + 1:])   # next top-level thing
    body = SRC[i:i + 1 + m.start()] if m else SRC[i:]
    # CODE ONLY. The comment above the submit explains what asyncio.run used to do here, and a
    # naive substring search matches that explanation and fails on a correct file.
    body = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith("#"))
    assert "asyncio.run(" not in body, (
        "run_batch_async creates its own event loop again; Playwright objects made on it die with "
        "the batch")
    assert "run_coroutine_threadsafe" in body, "the batch no longer submits to the shared loop"


def test_one_loop_outlives_the_batch_suite():
    """pytest collects test_* only — without this wrapper the t_* checks above never run."""
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} ENGINE-LOOP TESTS PASSED")
