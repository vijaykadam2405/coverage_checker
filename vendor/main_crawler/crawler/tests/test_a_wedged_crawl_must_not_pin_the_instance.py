"""A crawl that is alive but stuck must not pin the instance for the life of the process.

THE BUG THIS CLOSES, STATED EXACTLY
-----------------------------------
`POST /v1/crawl` had NO single-flight guard at all, and it calls `_arm()`, which clears the stop
FILE. So a second single crawl could start beside a running one, and pressing Stop during the
overlap halted nothing — `_arm()` wiped the flag the engine polls.

The obvious repair is to copy the guard from `/v1/crawl/batch`. That is the one thing that must
not be done, and it is why this file exists. That guard is:

    if _batch_status["running"] and owner is not None and owner.is_alive(): refuse

`is_alive()` tests a THREAD. The failure this fleet actually has is a thread that is alive for
ever: a dispatch blocked on `.result()` while `_host_hold` never drains is alive by every measure
Python can take. Under that guard the instance refuses every future crawl until the container is
restarted — so copying it to a second route would double the surface of a guard with a known way
to wedge the service it protects. The batch route carried that latent exposure too.

So `crawlclaim.CrawlClaim` asks a different question — not "is the thread alive" but "is the CRAWL
moving" — which is the same distinction `async_engine` already draws between a wedged host and a
merely saturated one (`host_busy_since` vs `host_last_ok`).

THE TWO ASSERTIONS THAT MATTER MOST
-----------------------------------
Symmetrical, and each is a way the fix could be worse than the bug:

  * `test_a_long_but_progressing_crawl_is_never_called_wedged` — a crawl that runs for hours is
    normal on this fleet. A guard that kills it has replaced a rare wedge with a routine one.
    (Same shape as "a saturated host must KEEP its hold".)
  * `test_a_wedge_is_refused_not_silently_granted` — auto-granting past a stuck owner starts a
    SECOND engine beside a pinned one, which is exactly the render-lane over-admission that
    `5eb6060` exists to prevent. A guard that heals itself into the failure the engine fix removes
    has not helped anybody. The wedge is made visible and overridable, never silently concurrent.

Run:  python tests/test_a_wedged_crawl_must_not_pin_the_instance.py
      python -m pytest tests/test_a_wedged_crawl_must_not_pin_the_instance.py -q

It imports `crawlclaim` only — stdlib, no fastapi — so it runs anywhere the engine tests do.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_api.crawlclaim import CrawlClaim  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "crawler_api" / "app.py"


class FakeThread:
    """A thread whose liveness we control — including alive-for-ever, the case that matters."""

    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _held(claim, clock, owner=None, route="/v1/crawl/batch", jobs=("j1",)):
    owner = owner or FakeThread(alive=True)
    got = claim.acquire(route=route, job_ids=list(jobs), owner=owner)
    assert got["granted"], got
    return owner


# --------------------------------------------------------------------------- concurrency

def test_a_second_concurrent_crawl_is_refused_on_either_route():
    clock = Clock()
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    _held(c, clock, route="/v1/crawl/batch")
    # The single route is the one that had no guard at all. It must now be refused by the SAME
    # claim the batch route took -- two claims would be no guard.
    r = c.acquire(route="/v1/crawl", job_ids=["j2"], owner=FakeThread())
    assert not r["granted"] and r["reason"] == "running", r
    # ...and the reverse direction, which is how the stop file got cleared under a live batch.
    c.release()
    _held(c, clock, route="/v1/crawl")
    r = c.acquire(route="/v1/crawl/batch", job_ids=["j3"], owner=FakeThread())
    assert not r["granted"] and r["reason"] == "running", r


def test_a_dead_owner_heals_without_a_container_restart():
    clock = Clock()
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    owner = _held(c, clock)
    owner._alive = False
    r = c.acquire(route="/v1/crawl", job_ids=["j2"], owner=FakeThread())
    assert r["granted"] and r["reason"] == "stale_owner", r


# --------------------------------------------------------------------------- the wedge

def test_a_wedge_is_refused_not_silently_granted():
    """An owner alive for ever is detected -- and REFUSED, because granting starts a second engine.

    This is the case `owner.is_alive()` cannot see at all.
    """
    clock = Clock()
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    _held(c, clock)
    frozen = {"pages": 41}

    def probe():
        return dict(frozen)

    # Too young to judge: a crawl five minutes old is not news, and the probe is not even run.
    clock.advance(300)
    assert c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(),
                     probe=probe)["reason"] == "running"
    # Past the budget the first probe only establishes a baseline. Declaring a wedge here would
    # convict on a single sample.
    clock.advance(700)
    assert c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(),
                     probe=probe)["reason"] == "running"
    # Still frozen a full budget later -> wedged, refused, and SAYS so.
    clock.advance(901)
    r = c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(), probe=probe)
    assert not r["granted"] and r["reason"] == "wedged", r
    assert r["detail"]["stalled_s"] >= 900
    assert "force" in r["detail"]["hint"]


def test_a_wedge_is_overridable_so_the_instance_never_needs_a_restart():
    clock = Clock()
    c = CrawlClaim(wedge_after_s=10, clock=clock)
    _held(c, clock)
    probe = lambda: {"pages": 7}                                          # noqa: E731
    clock.advance(50)
    c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(), probe=probe)
    clock.advance(50)
    assert c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(),
                     probe=probe)["reason"] == "wedged"
    r = c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(), probe=probe, force=True)
    assert r["granted"] and r["reason"] == "forced" and r["detail"]["over"] == "wedged", r
    assert c.status()["forced_over"] == 1


def test_a_long_but_progressing_crawl_is_never_called_wedged():
    """The symmetric assertion. Crawls here run for hours; calling those wedged is the worse bug."""
    clock = Clock()
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    _held(c, clock)
    n = [0]

    def advancing():
        n[0] += 1
        return {"pages": n[0]}

    for _ in range(24):                       # ~6.6 hours of a healthy crawl
        clock.advance(1000)
        r = c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(), probe=advancing)
        assert not r["granted"] and r["reason"] == "running", r


def test_an_unavailable_probe_never_grants():
    """Cannot-tell is treated as running. A guard that fails open when its evidence is missing
    starts a second engine every time the history database has a bad minute."""
    clock = Clock()
    c = CrawlClaim(wedge_after_s=10, clock=clock)
    _held(c, clock)
    clock.advance(5000)

    def explode():
        raise RuntimeError("history db down")

    for probe in (explode, lambda: None):
        r = c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread(), probe=probe)
        assert not r["granted"] and r["reason"] == "running", r
        assert r["detail"]["progress"] == "unavailable", r


def test_a_background_batch_keeps_the_claim_after_its_request_thread_ends():
    """The request thread claims; the daemon thread does the work. Without set_owner the claim
    looks stale the instant the response is written and the next crawl walks straight in."""
    clock = Clock()
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    request_thread = _held(c, clock)
    daemon = FakeThread(alive=True)
    c.set_owner(daemon)
    request_thread._alive = False
    r = c.acquire(route="/v1/crawl", job_ids=["x"], owner=FakeThread())
    assert not r["granted"] and r["reason"] == "running", r


def test_status_is_json_safe():
    """A Thread in the status dict 500s /v1/batch/status, which takes the dashboard down with it."""
    clock = Clock()
    c = CrawlClaim(clock=clock)
    _held(c, clock)
    json.dumps(c.status())
    c.release()
    assert c.status()["held"] is False


# --------------------------------------------------------------------------- wiring

def test_the_production_routes_actually_go_through_the_claim():
    """A module whose own assertions pass while no production path calls it is this codebase's
    most repeated failure (`key.relation()`, `medoid()`, `cluster_authority`). These assertions
    are read off app.py itself, because the app cannot be imported without fastapi."""
    src = APP.read_text(encoding="utf-8")
    assert "_crawl_claim = CrawlClaim()" in src, "no shared claim in app.py"
    # BOTH entry points, not just the one that already had a guard.
    for route in ('route="/v1/crawl"', 'route="/v1/crawl/batch"'):
        assert "_crawl_claim.acquire(%s" % route in src, "route not claimed: %s" % route
    # Both must release, or the first crawl pins the instance for ever -- the bug, reintroduced.
    assert src.count("_crawl_claim.release()") >= 2, "a route never releases its claim"
    assert "_crawl_claim.set_owner(t)" in src, "background batch never hands over its claim"


def test_a_refused_request_must_not_disarm_a_live_crawl():
    """ORDERING, and it is the whole single-route bug: `_arm()` clears the stop FILE, so a crawl
    request that is going to be refused must not reach it. Asserted positionally because that is
    the only thing that makes it true."""
    src = APP.read_text(encoding="utf-8")
    body = src[src.index('@app.post("/v1/crawl")'):src.index("class BatchRequest")]
    assert body.index("_crawl_claim.acquire(") < body.index("_arm(["), \
        "/v1/crawl calls _arm() before claiming -- a refused request would clear the stop file"
    batch = src[src.index("def crawl_batch("):src.index("    def _run()")]
    assert batch.index("_crawl_claim.acquire(") < batch.index("_arm(_ids)"), \
        "/v1/crawl/batch calls _arm() before claiming"


def test_the_refusal_distinguishes_a_wedge_from_a_healthy_long_crawl():
    """'still going' and 'stuck for twenty minutes' must not read alike, or the operator cannot
    tell a busy instance from a broken one -- which is the state the fleet was already in."""
    src = APP.read_text(encoding="utf-8")
    ref = src[src.index("def _refusal("):src.index("def _dispatch(")]
    assert "wedged" in ref and "force=true" in ref, ref


def _suite():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


def test_a_wedged_crawl_must_not_pin_the_instance_suite():
    for fn in _suite():
        if fn is not test_a_wedged_crawl_must_not_pin_the_instance_suite:
            fn()


if __name__ == "__main__":
    fns = [f for f in _suite() if f is not test_a_wedged_crawl_must_not_pin_the_instance_suite]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d SINGLE-FLIGHT TESTS PASSED" % len(fns))
