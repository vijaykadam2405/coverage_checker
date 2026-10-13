"""A STOP must outlive the teardown it causes.

THE BUG THIS EXISTS FOR, measured on the live fleet: the operator stopped a 12-instance crawl; all
twelve acknowledged; teardown finished ~110s later leaving 578 saved frontiers. About a minute after
that, the auto-refill watcher resumed the whole thing (`POST /v1/crawl/resume` in the crawler log)
and the crawl an operator had deliberately halted was running again, with nothing on screen saying
why.

The existing guard was not wrong, it was too SHORT-LIVED. `stop_pending` comes from the crawler's
own stop flag, which the engine consumes as it winds down — so it is true only *during* the stop.
Once teardown completes, a halted instance and an instance that merely ran out of work are
byte-for-byte identical, and refill cannot tell them apart from the crawler alone. The intent has to
be recorded where it survives the teardown.

Runnable directly:  python tests/test_operator_halt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import refill  # noqa: E402


class _FakeScheduler:
    # Mirrors the real module's fleet concurrency cap. 0 = uncapped, which is what every case in
    # this file assumes: they are about the operator halt, not about how many instances may run.
    MAX_BUSY_INSTANCES = 0

    def __init__(self, halted: bool, maintenance: bool = False):
        self._halted = halted
        self._maintenance = maintenance
        self.set_calls = []

    def halted(self, _db):
        return self._halted

    def maintenance(self, _db):
        # A separate hold, checked by refill alongside the halt: it pauses the fleet for a frontier
        # move WITHOUT dropping anything. Default off, so every existing case here is unchanged.
        return self._maintenance

    def lane_open(self, _db, _lane):
        # The dispatch policy (see test_dispatch_policy.py). Open here: these cases are about the
        # halt, and a closed lane would hold refill before the halt is even reached.
        return True

    def why_closed(self, _db, _lane):
        return None

    def owned_by_mode(self, _db, ids):
        # The mode scoping (test_control_plane.py): every frontier is the current mode's here,
        # because these cases are about the halt, not about who owns what.
        return list(ids)

    def set_halted(self, _db, on):
        self._halted = on
        self.set_calls.append(on)


def _install(monkey_halted: bool):
    """Point refill at a fake scheduler + session, and record what it asks the crawler to do."""
    fake = _FakeScheduler(monkey_halted)
    sys.modules["mallory_orchestrator"].scheduler = fake       # refill imports it lazily
    resumed = []

    class FakeClient:
        @staticmethod
        def pool():
            return ["http://crawler-api:8099", "http://crawler-api-2:8099"]

        @staticmethod
        def batch_status(base=None):
            # The state AFTER a completed stop: idle, and the crawler's own flag already cleared.
            return {"running": False, "stop_pending": False}

        @staticmethod
        def resumable(base=None):
            # has_config is required since refill learned to skip jobs with no saved definition
            # (an unresumable leftover) — a real /v1/resumable row carries it.
            return {"jobs": [{"job_id": "j1", "pending": 500, "has_config": True}]}

        @staticmethod
        def resume(ids, background=True, forward_to_ingest=True, base=None):
            resumed.append((base, list(ids)))
            return {"resumed": list(ids)}

    refill.crawler_client = FakeClient
    return fake, resumed


class _FakeSession:
    def close(self):
        pass


def _patch_db():
    import mallory_orchestrator.db as db
    db.SessionLocal = lambda: _FakeSession()


def test_a_completed_operator_stop_is_not_resumed() -> None:
    """The regression. Idle + stop_pending False is exactly what a finished teardown looks like."""
    _patch_db()
    fake, resumed = _install(monkey_halted=True)
    out = refill.refill_once()
    assert out.get("halted") is True, out
    assert resumed == [], f"refill resumed a crawl the operator halted: {resumed}"
    assert out["resumed"] == 0


def test_an_instance_that_merely_ran_out_of_work_is_still_refilled() -> None:
    """The guard must not disable the watcher outright — an idle instance holding pending URLs is
    pure waste, which is the whole reason refill exists."""
    _patch_db()
    fake, resumed = _install(monkey_halted=False)
    out = refill.refill_once()
    assert out["resumed"] == 2, out          # one per instance in the fake pool
    assert len(resumed) == 2, resumed
    # each instance resumes its OWN frontier, on its own base — never moved between containers
    assert {b for b, _ in resumed} == {"http://crawler-api:8099", "http://crawler-api-2:8099"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all good")
