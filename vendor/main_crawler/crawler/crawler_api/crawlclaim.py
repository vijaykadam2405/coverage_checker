"""Single-flight for the crawl entry points — one crawl per instance, and a guard that heals.

WHY THIS IS A MODULE AND NOT SIX LINES IN THE ROUTE
---------------------------------------------------
`/v1/crawl/batch` already had a single-flight guard. `/v1/crawl` had NONE, and it calls `_arm()`,
which CLEARS THE STOP FILE — so a second single crawl could start beside a running one and pressing
Stop during the overlap halted nothing. Two engines then run in one process, each with its own
`adaptive` gate, and the render limit stops being global: three engines at `render_limit=16` admit
48 concurrent renders on a lane sized for 16.

The obvious repair — copy the batch route's guard across — is the one thing that must not be done,
and the reason is the whole point of this file. That guard is:

    if _batch_status["running"] and owner is not None and owner.is_alive(): refuse

`is_alive()` is a liveness test on a THREAD, and the failure we actually have is a thread that is
alive forever. A dispatch blocked on `.result()` for a jammed host is alive by every measure Python
can take, so the guard can never heal itself: the instance refuses all work until the container is
restarted. Copying it to the second route would double the surface of a guard that has a known way
to wedge the service it protects.

So the guard here asks a different question. Not "is the thread alive" but "is the CRAWL moving",
which is the same distinction `async_engine` already draws between a wedged host and a merely
saturated one (`host_busy_since` vs `host_last_ok`). Three outcomes, not two:

    owner gone           -> GRANT. The thread is dead; nothing of ours is still crawling in this
                            process, so there is no second engine to create. This is the heal.
    owner alive, moving  -> REFUSE "running". A crawl that runs for six hours is not a fault.
    owner alive, stuck   -> REFUSE "wedged", with a distinguishable reason and an explicit
                            override. NOT a grant -- see below.

WHY A WEDGE REFUSES INSTEAD OF HEALING
--------------------------------------
Auto-granting past a stuck owner is the tempting behaviour and it is wrong: it starts a second
engine beside a pinned one, which is precisely the render-lane over-admission that `5eb6060` exists
to prevent. A guard that heals itself into the failure the engine fix removes has not helped
anybody. So a wedge is made VISIBLE and OVERRIDABLE rather than silently concurrent — the operator
gets a reason that says which state it is in, and `force=True` if they mean it. That is strictly
better than today (where a wedge is invisible and needs a container restart) without trading it for
a worse failure.

WHY THE PROBE CAN RETURN None, AND WHY None REFUSES
---------------------------------------------------
Progress is read from outside this module through a caller-supplied `probe()`. In the API that is
the crawl frontier, which the engine writes as it goes — `_batch_status` cannot serve here, because
per-job counts land only at BATCH END (see the `/v1/metrics` docstring), so a heartbeat read off it
would never tick during a legitimate multi-hour crawl and would declare every long crawl wedged.

A probe that raises, or that cannot reach its store, returns None: "cannot tell". Cannot-tell is
treated as RUNNING, never as wedged and never as free. A guard that grants when its evidence is
missing is a guard that fails open the moment the database it depends on has a bad minute.

    python crawlclaim.py --demo
"""
from __future__ import annotations

import threading
import time

# How long a claim must sit with NO forward progress before it is called wedged. Deliberately
# generous: the cost of calling a slow crawl wedged is an operator chasing a non-problem, while the
# cost of being slow to notice a real wedge is bounded — the instance was refusing work either way.
WEDGE_AFTER_S = 900.0


class CrawlClaim:
    """One claim per process. Both crawl routes go through the same instance or neither is guarded.

    `clock` is injected so the tests can age a claim without sleeping. Nothing else in here reads
    the wall clock; a monotonic source is used because a claim's age must not move when the host's
    clock is stepped.
    """

    def __init__(self, wedge_after_s: float = WEDGE_AFTER_S, clock=time.monotonic) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._wedge_after_s = float(wedge_after_s)
        self._held = False
        self._owner: threading.Thread | None = None
        self._route = ""
        self._job_ids: list[str] = []
        self._claimed_at = 0.0
        self._probe_snapshot = None
        self._probe_at = 0.0
        self._forced_over = 0

    # ------------------------------------------------------------------ acquire / release

    def acquire(self, *, route: str, job_ids, owner: threading.Thread | None = None,
                force: bool = False, probe=None) -> dict:
        """Try to become the one crawl on this instance.

        -> {"granted": bool, "reason": str, "detail": {...}}

        `reason` is part of the contract, not decoration: the caller reports it to the operator and
        `/v1/batch/status` renders it, so "the previous batch is still going" and "the previous
        batch has been stuck for twenty minutes" stop looking identical from outside.
        """
        owner = owner or threading.current_thread()
        with self._lock:
            verdict = self._evaluate(probe)
            if not verdict["granted"] and force:
                verdict = {"granted": True, "reason": "forced",
                           "detail": dict(verdict["detail"], over=verdict["reason"])}
                self._forced_over += 1
            if verdict["granted"]:
                self._held = True
                self._owner = owner
                self._route = route
                self._job_ids = [str(j) for j in (job_ids or [])]
                self._claimed_at = self._clock()
                self._probe_snapshot = None
                self._probe_at = self._claimed_at
            return verdict

    def _evaluate(self, probe) -> dict:
        """The decision, with the lock already held."""
        if not self._held:
            return {"granted": True, "reason": "free", "detail": {}}

        held = self._detail()
        # A dead owner cannot still be crawling in this process. This is the self-heal, and it is
        # the one grant that cannot produce two live engines.
        if self._owner is None or not self._owner.is_alive():
            return {"granted": True, "reason": "stale_owner", "detail": held}

        now = self._clock()
        if now - self._claimed_at < self._wedge_after_s:
            # Too young to judge. Do not probe: the probe costs a query and a crawl that started
            # four minutes ago is not news.
            return {"granted": False, "reason": "running", "detail": held}

        snapshot = None
        if probe is not None:
            try:
                snapshot = probe()
            except Exception:
                snapshot = None                      # cannot tell -> running, never wedged
        if snapshot is None:
            return {"granted": False, "reason": "running",
                    "detail": dict(held, progress="unavailable")}

        if snapshot != self._probe_snapshot:
            self._probe_snapshot = snapshot
            self._probe_at = now
            return {"granted": False, "reason": "running",
                    "detail": dict(held, progress="moving")}

        if now - self._probe_at >= self._wedge_after_s:
            return {"granted": False, "reason": "wedged",
                    "detail": dict(held, stalled_s=round(now - self._probe_at, 1),
                                   hint="retry with force=true to take the engine over; the "
                                        "previous crawl is alive but has made no progress")}
        return {"granted": False, "reason": "running", "detail": dict(held, progress="unchanged")}

    def set_owner(self, owner: threading.Thread) -> None:
        """Hand the claim to the thread that will actually outlive the request.

        The background batch path claims on the REQUEST thread and then dispatches on a daemon
        thread. If the claim kept pointing at the request thread it would look stale the instant
        the response was written, and the next request would be granted straight past a live crawl.
        """
        with self._lock:
            if self._held:
                self._owner = owner

    def release(self) -> None:
        """Drop the claim. Call this FIRST in the `finally`, before anything that can raise.

        The batch route has already been bitten by the other order: a `results=[...]` comprehension
        evaluated as an argument raised before the running flag was cleared, the thread died, and
        the API refused every later batch until the container was restarted. Observed twice.
        """
        with self._lock:
            self._held = False
            self._owner = None
            self._route = ""
            self._job_ids = []
            self._probe_snapshot = None

    # ------------------------------------------------------------------ reporting

    def _detail(self) -> dict:
        return {"route": self._route, "job_ids": list(self._job_ids),
                "held_s": round(self._clock() - self._claimed_at, 1),
                "owner_alive": bool(self._owner is not None and self._owner.is_alive())}

    def status(self) -> dict:
        """What `/v1/batch/status` shows. Never returns the Thread — it is not JSON-serialisable."""
        with self._lock:
            if not self._held:
                return {"held": False, "forced_over": self._forced_over}
            return dict({"held": True, "forced_over": self._forced_over}, **self._detail())


def _demo() -> None:
    class FakeThread:
        """A thread whose liveness we control — including alive-forever, the case that matters."""

        def __init__(self, alive=True):
            self._alive = alive

        def is_alive(self):
            return self._alive

    t = [1000.0]
    clock = lambda: t[0]                                                        # noqa: E731

    # 1. A free claim is granted, and a second concurrent crawl is refused.
    c = CrawlClaim(wedge_after_s=900, clock=clock)
    live = FakeThread(alive=True)
    assert c.acquire(route="/v1/crawl", job_ids=["a"], owner=live)["granted"]
    r = c.acquire(route="/v1/crawl/batch", job_ids=["b"], owner=FakeThread())
    assert not r["granted"] and r["reason"] == "running", r

    # 2. The two routes share ONE claim. A guard on the batch route alone is what let a single
    #    crawl start beside a batch and clear the stop file underneath it.
    assert not c.acquire(route="/v1/crawl", job_ids=["c"], owner=FakeThread())["granted"]

    # 3. A dead owner heals on the very next request — no container restart.
    live._alive = False
    r = c.acquire(route="/v1/crawl", job_ids=["d"], owner=FakeThread())
    assert r["granted"] and r["reason"] == "stale_owner", r
    c.release()

    # 4. THE CASE THE `is_alive()` GUARD CANNOT SEE: an owner that is alive forever.
    stuck = FakeThread(alive=True)
    assert c.acquire(route="/v1/crawl/batch", job_ids=["e"], owner=stuck)["granted"]
    frozen = {"done": 7}
    probe = lambda: dict(frozen)                                                # noqa: E731
    # Too young to judge, even though nothing is moving.
    t[0] += 300
    assert c.acquire(route="/v1/crawl", job_ids=["f"], owner=FakeThread(),
                     probe=probe)["reason"] == "running"
    # Past the budget, first probe only ESTABLISHES the baseline — it cannot be wedged yet.
    t[0] += 700
    assert c.acquire(route="/v1/crawl", job_ids=["f"], owner=FakeThread(),
                     probe=probe)["reason"] == "running"
    # Still frozen a full budget later: now it is wedged, and it is REFUSED, not granted.
    t[0] += 901
    r = c.acquire(route="/v1/crawl", job_ids=["f"], owner=FakeThread(), probe=probe)
    assert not r["granted"] and r["reason"] == "wedged", r
    assert r["detail"]["stalled_s"] >= 900 and "force" in r["detail"]["hint"]

    # 5. A wedge is overridable without a restart — the whole point of not silently granting.
    r = c.acquire(route="/v1/crawl", job_ids=["g"], owner=FakeThread(), probe=probe, force=True)
    assert r["granted"] and r["reason"] == "forced" and r["detail"]["over"] == "wedged", r
    assert c.status()["forced_over"] == 1
    c.release()

    # 6. A LONG BUT PROGRESSING crawl must never be called wedged. This is the assertion that stops
    #    the guard killing the legitimate six-hour crawls this fleet actually runs — the same shape
    #    as "a saturated host must KEEP its hold".
    moving = FakeThread(alive=True)
    assert c.acquire(route="/v1/crawl/batch", job_ids=["h"], owner=moving)["granted"]
    n = [0]

    def advancing():
        n[0] += 1
        return {"done": n[0]}

    for _ in range(20):
        t[0] += 1000
        r = c.acquire(route="/v1/crawl", job_ids=["i"], owner=FakeThread(), probe=advancing)
        assert not r["granted"] and r["reason"] == "running", r
    c.release()

    # 7. A probe that fails must not grant. Failing open here would start a second engine every
    #    time the history database had a bad minute.
    boom = FakeThread(alive=True)
    assert c.acquire(route="/v1/crawl", job_ids=["j"], owner=boom)["granted"]
    t[0] += 5000

    def explode():
        raise RuntimeError("history db down")

    r = c.acquire(route="/v1/crawl", job_ids=["k"], owner=FakeThread(), probe=explode)
    assert not r["granted"] and r["reason"] == "running", r
    assert r["detail"]["progress"] == "unavailable", r
    r = c.acquire(route="/v1/crawl", job_ids=["k"], owner=FakeThread(), probe=lambda: None)
    assert not r["granted"] and r["reason"] == "running", r
    c.release()

    # 8. set_owner keeps a background batch claimed after the request thread has gone.
    req = FakeThread(alive=True)
    assert c.acquire(route="/v1/crawl/batch", job_ids=["l"], owner=req)["granted"]
    daemon = FakeThread(alive=True)
    c.set_owner(daemon)
    req._alive = False                        # the request thread ends the moment we respond
    r = c.acquire(route="/v1/crawl", job_ids=["m"], owner=FakeThread())
    assert not r["granted"] and r["reason"] == "running", r

    # 9. status() must stay JSON-safe — a Thread in the status dict is what /v1/batch/status pops
    #    by hand today, and a second place to forget.
    import json
    json.dumps(c.status())
    c.release()
    assert c.status() == {"held": False, "forced_over": 1}

    print("ok")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv or len(sys.argv) == 1:
        _demo()
