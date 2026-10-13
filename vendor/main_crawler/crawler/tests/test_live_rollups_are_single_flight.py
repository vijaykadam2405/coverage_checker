"""The ingest API's Postgres rollups must be cached AND single-flight.

MEASURED on the live stack, and the reason this exists. Crawl throughput had HALVED — 1.19 to 0.48
pages/s — with the box still at load 80. cgroup usage_usec deltas put POSTGRES at 7.16 of 40 cores,
more than any crawler container, where it normally sits below 1. pg_stat_activity explained it:

    3 x  SELECT count(*), count(DISTINCT source_id), sum(html_len), sum(text_len) ...   ~39s each
    1 x  WITH r AS (... row_number() OVER (PARTITION BY source_id, content_hash) ...)   8m07s

Every one is a full pass over `documents_current`, a DISTINCT-ON view over 927k rows. The dashboard
polls faster than the query returns, so each poll started another one and they stacked until the
database was starving the crawl it exists to serve.

A TTL alone does NOT fix this. With nothing cached yet, N simultaneous polls still launch N
queries — which is exactly the state the measurement caught. The LOCK is the load-bearing part:
the first caller runs the query, the rest wait and take its result.

Only MinIO was cached in this module. The orchestrator had already learned this lesson for its own
rollups; the ingest API never got it.
"""
from __future__ import annotations

import threading
import time

import pytest

from ingest_api import live


@pytest.fixture(autouse=True)
def _clear():
    live._PG_CACHE.clear()
    yield
    live._PG_CACHE.clear()


def test_concurrent_callers_run_the_query_ONCE():
    """The failure that was actually observed: three identical 39s queries in flight together."""
    calls = []
    barrier = threading.Barrier(8)

    def slow():
        calls.append(1)
        time.sleep(0.25)                       # long enough that the others pile up behind it
        return {"pages": 927194}

    def worker(out, i):
        barrier.wait()                         # everyone hits it at the same instant
        out[i] = live._cached_pg("summary", slow)

    out = [None] * 8
    ts = [threading.Thread(target=worker, args=(out, i)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1, "%d concurrent callers ran %d queries — not single-flight" % (8, len(calls))
    assert all(o == {"pages": 927194} for o in out), "waiters did not get the result"


def test_a_second_call_inside_the_ttl_is_served_from_cache():
    calls = []
    live._cached_pg("k", lambda: calls.append(1) or "v")
    live._cached_pg("k", lambda: calls.append(1) or "v")
    assert len(calls) == 1


def test_the_cache_expires():
    calls = []
    live._cached_pg("k", lambda: calls.append(1) or "v", ttl=0.01)
    time.sleep(0.05)
    live._cached_pg("k", lambda: calls.append(1) or "v", ttl=0.01)
    assert len(calls) == 2


def test_keys_do_not_share_a_cache_slot():
    live._cached_pg("a", lambda: "A")
    assert live._cached_pg("b", lambda: "B") == "B"
    assert live._cached_pg("a", lambda: "SHOULD NOT RUN") == "A"


def test_quality_gets_a_much_longer_ttl_than_the_others():
    """It is an 8-minute audit, not a live gauge. At the 30s rollup TTL it could still run 120
    times an hour, which is the same fault in slower motion."""
    assert live._QUALITY_TTL >= 600
    assert live._QUALITY_TTL > live._PG_TTL * 10


def test_every_expensive_live_endpoint_goes_through_the_cache():
    """A new rollup endpoint that forgets the cache reintroduces the whole problem, silently — the
    symptom is not an error, it is the crawl getting slower."""
    import inspect

    for name in ("summary", "types", "quality"):
        src = inspect.getsource(getattr(live, name))
        assert "_cached_pg(" in src, "/api/live/%s queries Postgres uncached" % name


def test_invalidate_clears_it():
    """Same reasoning as the MinIO cache: right after a flush the operator is looking at the page
    to confirm the rows are gone, and a stale count reads as a failed delete."""
    live._cached_pg("k", lambda: "old")
    live.invalidate_pg_cache()
    assert live._cached_pg("k", lambda: "new") == "new"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
