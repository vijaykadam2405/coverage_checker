"""A cleanup that gives up must kill what it abandoned.

MEASURED 2026-08-16 on the live fleet. Counting TOP-LEVEL chrome processes — not all of them, which
is what hid this — the shape is unmistakable:

    instance 10 :   25 chrome,  2 browsers under ONE   node driver   <- healthy
    instance 11 :  265 chrome, 24 browsers under TWELVE node drivers <- twelve leaked batches
    instance  7 :  320 chrome
    fleet       : 2,266 chrome processes against a ~300 baseline
    result      : 0.14 pages/s on 0.2 cores, load average 293, 33,679 tasks in crawler containers

AsyncEngine.shutdown() bounds every close at 5s and abandons what does not finish, which is right —
a wedged Chromium close never returns and it once left a batch stuck at running=True forever. But
its docstring justified the abandonment with "the process is ending and tini reaps the browser
children anyway", and BOTH HALVES ARE FALSE in the deployed shape:

  * the process is not ending — shutdown() runs at the end of every BATCH and the container stays
    up for the next one;
  * tini reaps ZOMBIES, and a Chromium that ignored close() is not a zombie. It is alive, holding
    its RSS, and its parent node driver is alive too.

Each batch calls async_playwright().start(), forking a `node` driver that owns that batch's
browsers. Every timed-out teardown orphaned one of those trees, and the next batch started a fresh
one beside it. `recycle:` appears 0-2 times in those instances' logs — this is NOT the _recycle
leak fixed the day before, it is the teardown path, and it accumulates per batch rather than per
hour (every instance had the same 25h uptime and counts ranged from 25 to 320).

Runnable directly:  python tests/test_abandoned_browsers_are_killed.py
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler import async_engine as ae  # noqa: E402

SRC = ROOT / "crawler" / "async_engine.py"


# ── the behaviour ────────────────────────────────────────────────────────────

def t_a_close_that_completes_reports_true() -> None:
    """_bounded_close's return value is what tells shutdown whether anything leaked, so it has to
    be right in the ordinary case too — a blanket False would kill on every clean teardown."""
    async def _ok():
        return None
    assert asyncio.run(ae._bounded_close(_ok(), 5.0, "x")) is True


def t_a_close_that_hangs_reports_false_and_does_not_raise() -> None:
    """The signature of the bug: the close never returns. It must be abandoned (not raise, not
    block) AND must be reported as unfinished."""
    async def _hang():
        await asyncio.sleep(30)

    t0 = time.time()
    got = asyncio.run(ae._bounded_close(_hang(), 0.3, "wedged browser"))
    assert got is False, "an abandoned close reported success — shutdown would never kill it"
    assert time.time() - t0 < 5, "bounded close blocked; the batch thread would hang here"


def t_the_killer_takes_the_whole_tree_not_just_the_parent() -> None:
    """Killing the node driver alone would leave its Chromium children running — which is the exact
    leak, one level down. Uses real processes: a parent that spawns a child and sleeps."""
    if ae.psutil is None:
        print("     (skipped: psutil unavailable)")
        return
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
         "time.sleep(120)"])
    try:
        deadline = time.time() + 15
        kids = []
        while time.time() < deadline:
            try:
                kids = ae.psutil.Process(parent.pid).children(recursive=True)
            except Exception:
                kids = []
            if kids:
                break
            time.sleep(0.2)
        assert kids, "test setup never spawned a grandchild; nothing would be proven"
        kid_pids = [k.pid for k in kids]

        killed = ae._kill_driver_tree({parent.pid}, "test driver")
        assert killed >= 2, "killed %d — the child was left running, which IS the leak" % killed

        for pid in [parent.pid] + kid_pids:
            gone = False
            for _ in range(50):
                try:
                    p = ae.psutil.Process(pid)
                    if not p.is_running() or p.status() == ae.psutil.STATUS_ZOMBIE:
                        gone = True
                        break
                except Exception:
                    gone = True
                    break
                time.sleep(0.1)
            assert gone, "pid %d survived the kill" % pid
    finally:
        try:
            parent.kill()
            parent.wait(timeout=10)
        except Exception:
            pass


def t_killing_a_dead_pid_is_harmless() -> None:
    """Teardown runs on the unhappy path by definition; it must never raise because a process it
    was going to kill had already exited."""
    assert ae._kill_driver_tree({999_999_999}, "ghost") == 0
    assert ae._kill_driver_tree(set()) == 0


def t_the_driver_pids_are_captured_by_difference_not_by_name() -> None:
    """Several things in this container are `node`. Picking by name would kill a bystander; the
    capture must be the difference across the start() call."""
    src = SRC.read_text(encoding="utf-8")
    # The capture moved from start() into _shared_browsers when the pool became process-wide; the
    # property is unchanged, so match the assignment wherever it now lives.
    m = re.search(r"_before\s*=\s*_driver_children\(\).*?_driver_children\(\)\s*-\s*_before",
                  src, re.S)
    assert m, "the driver pids are no longer captured as a set difference around the start call"


# ── the wiring (pinned against the source: importing the engine's shutdown for real needs a live
#    playwright, and the point is that this code path only runs when that has gone wrong) ─────────

def _shutdown_body() -> str:
    src = SRC.read_text(encoding="utf-8")
    i = src.index("    async def shutdown(self)")
    body = src[i:i + 4000]
    # Strip comments and the docstring: both explain the bug at length and must not satisfy the
    # assertions below with their own prose.
    body = body.split('"""', 2)[-1]
    return "\n".join(ln.split("#")[0] for ln in body.splitlines())


def t_shutdown_never_touches_the_shared_pool() -> None:
    """THE PROPERTY THAT REPLACED THE KILL.

    shutdown() used to close every browser and stop the driver, and kill whatever refused. That was
    the right fix for the wrong shape: measured on instance 7, six batches closed with ZERO timeouts
    and still left four whole driver trees alive, because a Playwright close returning success does
    not mean the OS process exited. The browsers are process-wide now, so shutdown closes only this
    batch's contexts and tabs — there is no longer any per-batch close that CAN strand a process.
    """
    body = _shutdown_body()
    squashed = "".join(body.split())
    assert "b.close()" not in squashed and "browser.close()" not in squashed, (
        "shutdown still closes a browser; those are shared and must outlive the batch")
    assert "_pw.stop()" not in squashed, (
        "shutdown still stops the driver; it is process-wide and shared by every later batch")
    assert "ctx.close()" in squashed, "shutdown no longer closes this batch's contexts"


def t_the_killer_is_still_available_as_a_last_resort() -> None:
    """_kill_driver_tree is no longer called per batch, but it must not be deleted: it is the only
    thing that can reclaim a driver whose process outlived its API, and the half-built-pool path in
    _shared_browsers is exactly such a case."""
    src = SRC.read_text(encoding="utf-8")
    assert "def _kill_driver_tree" in src, (
        "the last-resort killer was removed along with its per-batch caller")


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d ABANDONED-BROWSER TESTS PASSED" % len(fns))


def test_abandoned_browser_suite() -> None:
    main()


if __name__ == "__main__":
    main()
