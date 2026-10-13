"""Single-flight caching of the whole-corpus rollups.

WHY THIS EXISTS. The dashboard's fast timer is 3s; the corpus aggregates behind /api/data/summary
and /api/data/types each scan `documents_current` and cost ~20s. Measured on the live stack, that
mismatch had FIVE copies of the same query running concurrently, staggered six seconds apart: it
pinned Postgres, pushed the query past the client's 20s timeout so the tiles rendered "timed out",
and held an unbroken ACCESS SHARE lock on `documents` that made schema changes impossible — an
ALTER TABLE could not find a lock window in thirty consecutive attempts.

A plain expiry cache does NOT fix that. N simultaneous misses all still reach the database. The
lock is the whole point, so the lock is what is tested here — with real threads, because a
single-threaded test passes just as happily against a cache that has no lock at all.

Runnable directly:  python tests/test_rollup_cache.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api  # noqa: E402


def _reset() -> None:
    api._rollup_cache.clear()
    api._rollup_locks.clear()


def test_concurrent_callers_make_exactly_one_call() -> None:
    """The regression itself: twelve callers at once must produce ONE query, not twelve."""
    _reset()
    calls = []
    barrier = threading.Barrier(12)

    def slow():
        calls.append(1)
        time.sleep(0.3)                     # long enough that every thread is inside the window
        return {"reachable": True, "documents": 167493}

    def worker(out, i):
        barrier.wait()                      # all twelve arrive on a cold cache together
        out[i] = api._rollup("summary", slow)

    out = [None] * 12
    ts = [threading.Thread(target=worker, args=(out, i)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(calls) == 1, f"stampede: {len(calls)} concurrent calls reached the store"
    # and every caller got the real answer, not a placeholder while it waited
    assert all(o and o["documents"] == 167493 for o in out), out


def test_age_is_reported_not_hidden() -> None:
    """A cached number is fine; a cached number presented as live is not. `as_of_s` is how the
    dashboard can say which it is, so it must be 0 on the fetch and non-zero on a hit."""
    _reset()
    fresh = api._rollup("k", lambda: {"reachable": True, "n": 1})
    assert fresh["as_of_s"] == 0.0
    time.sleep(0.05)
    hit = api._rollup("k", lambda: {"reachable": True, "n": 2})
    assert hit["n"] == 1, "second caller inside the TTL must not re-query"
    assert hit["as_of_s"] > 0, "a cached answer must not claim to be live"


def test_expiry_requeries() -> None:
    _reset()
    old_ttl = api._ROLLUP_TTL_S
    api._ROLLUP_TTL_S = 0.05
    try:
        assert api._rollup("k", lambda: {"reachable": True, "n": 1})["n"] == 1
        time.sleep(0.08)
        assert api._rollup("k", lambda: {"reachable": True, "n": 2})["n"] == 2
    finally:
        api._ROLLUP_TTL_S = old_ttl


def test_failures_cache_briefly_but_not_for_the_full_ttl() -> None:
    """Failures MUST be cached or a slow-failing ingest turns the stampede into a queue, each
    waiter serially paying the full timeout. They must NOT be cached for the full TTL, or a
    recovered service stays red on screen long after it came back."""
    _reset()
    assert api._ROLLUP_FAIL_TTL_S < api._ROLLUP_TTL_S

    calls = []

    def failing():
        calls.append(1)
        return {"reachable": False, "error": "boom"}

    api._rollup("k", failing)
    api._rollup("k", failing)
    assert len(calls) == 1, "an error answer must be cached too, or failures serialise"

    old = api._ROLLUP_FAIL_TTL_S
    api._ROLLUP_FAIL_TTL_S = 0.05
    try:
        _reset()
        calls.clear()
        api._rollup("k", failing)
        time.sleep(0.08)
        api._rollup("k", failing)
        assert len(calls) == 2, "a failure must expire on the SHORT ttl so recovery shows up"
    finally:
        api._ROLLUP_FAIL_TTL_S = old


def test_a_slow_query_is_held_longer_than_the_base_ttl() -> None:
    """A query slower than its own TTL expires the instant it lands, so the next poll restarts it
    and the scan never stops — a cache that caches nothing. /api/data/types measures 39s against a
    30s TTL, which is exactly that. The hold must scale with what the answer cost."""
    _reset()
    old_ttl, old_fac = api._ROLLUP_TTL_S, api._ROLLUP_SLOW_FACTOR
    api._ROLLUP_TTL_S, api._ROLLUP_SLOW_FACTOR = 0.1, 3.0
    try:
        def slow():
            time.sleep(0.2)                 # twice the base TTL: naively this is stale on arrival
            return {"reachable": True, "n": 1}

        t0 = time.monotonic()
        api._rollup("k", slow)
        cost = time.monotonic() - t0
        _, _, ttl = api._rollup_cache["k"]
        # Against the MEASURED cost, not a hard 0.6. time.sleep(0.2) is a floor request, not a
        # guarantee: on Windows the timer granularity returns it at ~0.188s, so `ttl >= 0.6` failed
        # under full-suite load and passed in isolation — a flake that says nothing about the code.
        # The claim being tested is "held in proportion to what it cost", so measure the cost.
        assert ttl >= cost * api._ROLLUP_SLOW_FACTOR * 0.99, \
            f"slow answer cost {cost:.3f}s but was held only {ttl}s — it would re-run immediately"
        assert ttl > api._ROLLUP_TTL_S, "a slow answer must outlive the base TTL"
        # and a FAST answer must not be given an inflated hold
        api._rollup("fast", lambda: {"reachable": True, "n": 1})
        assert api._rollup_cache["fast"][2] == 0.1
    finally:
        api._ROLLUP_TTL_S, api._ROLLUP_SLOW_FACTOR = old_ttl, old_fac


def test_sources_cache_is_keyed_by_limit() -> None:
    """The Watchfloor asks for 800 rows and the category rollup for 5000. Serving one from the
    other's entry silently truncates a panel — a rollup that loses rows understates the store."""
    _reset()
    a = api._rollup("sources:800", lambda: {"reachable": True, "rows": [0] * 800})
    b = api._rollup("sources:5000", lambda: {"reachable": True, "rows": [0] * 5000})
    assert len(a["rows"]) == 800 and len(b["rows"]) == 5000


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all good")
