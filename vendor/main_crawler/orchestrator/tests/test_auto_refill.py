"""An instance that finishes its share must not sit idle while it still holds work.

A batch is split across the fleet by domain, and domains are wildly unequal — one site held 16,000
pages today while most hold under a hundred. So instances finish at very different times, and the
first one done used to just stop. Measured on the live seven-instance fleet: TWO were idle holding
~12,000 pending URLs between them, because their share of the batch had ended while work remained.
Restarting them by hand took the fleet from 10.7 to 16.7 pages/s — the largest single gain of the
day, and it was entirely self-inflicted idleness.

What it must NOT do is move work between instances. A domain's frontier lives inside one
container's sqlite with no export path, so "stealing" a site from a busy instance would restart it
from the seed while the real progress sat unreachable next door.

Runnable directly:  python tests/test_auto_refill.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import refill  # noqa: E402


class _Fake:
    """A fleet: (running, [job_ids]) per instance."""

    def __init__(self, spec):
        self.spec = spec
        self.resumed: dict[int, list[str]] = {}

    def pool(self):
        return [f"http://i{i}" for i in range(len(self.spec))]

    def _idx(self, base):
        return int(str(base).rsplit("i", 1)[-1])

    def batch_status(self, base=None):
        spec = self.spec[self._idx(base)]
        return {"running": spec[0], "stop_pending": (len(spec) > 2 and spec[2])}

    def resumable(self, limit=1000, base=None):
        return {"jobs": [{"job_id": j, "pending": 5, "has_config": True} for j in self.spec[self._idx(base)][1]]}

    def resume(self, ids, background=True, forward_to_ingest=True, base=None):
        self.resumed.setdefault(self._idx(base), []).extend(ids)
        return {"resumed": list(ids)}


def _with(fake):
    orig = refill.crawler_client
    refill.crawler_client = fake
    return orig


def test_an_idle_instance_holding_work_is_restarted():
    f = _Fake([(True, ["a"]), (False, ["b1", "b2"]), (False, ["c1"])])
    orig = _with(f)
    try:
        out = refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert 0 not in f.resumed, "a BUSY instance was disturbed"
    assert f.resumed[1] == ["b1", "b2"] and f.resumed[2] == ["c1"]
    assert out["idle"] == 2 and out["resumed"] == 3, out


def test_an_idle_instance_with_nothing_left_is_left_alone():
    """Idle with no saved frontier is genuinely finished. Resuming nothing would still arm the
    crawler and clear its stop flag, which is how a stopped fleet quietly restarts itself."""
    f = _Fake([(False, []), (False, [])])
    orig = _with(f)
    try:
        out = refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert not f.resumed, "resumed a job list that was empty"
    assert out["idle"] == 0 and out["resumed"] == 0, out


def test_a_fully_busy_fleet_is_untouched():
    f = _Fake([(True, ["a"]), (True, ["b"])])
    orig = _with(f)
    try:
        out = refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert not f.resumed and out["resumed"] == 0, out


def test_one_broken_instance_does_not_stop_the_others():
    """This runs on a timer beside the scheduler; one unreachable crawler must not cost the rest
    their refill, and must not kill the loop."""
    class _Boom(_Fake):
        def batch_status(self, base=None):
            if self._idx(base) == 0:
                raise RuntimeError("connection refused")
            return super().batch_status(base=base)

    f = _Boom([(False, ["a"]), (False, ["b"])])
    orig = _with(f)
    try:
        out = refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert f.resumed.get(1) == ["b"], "a healthy instance was skipped because a sibling failed"
    assert out["errors"] and out["errors"][0]["instance"] == 0, out


def test_it_never_moves_work_between_instances():
    """The safety property. Each instance resumes ONLY the job ids it reported itself."""
    f = _Fake([(False, ["own-0"]), (False, ["own-1"])])
    orig = _with(f)
    try:
        refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert f.resumed == {0: ["own-0"], 1: ["own-1"]}, f.resumed


def test_a_stopped_instance_is_never_restarted():
    """THE one that matters. The crawler's stop signal is a FILE that outlives the batch, so an
    instance the operator just halted looks exactly like one that ran out of work. Resuming it
    would undo the stop within a minute, silently — the same 'something restarted my crawl' failure
    as the multiprocess stop-file bug. A watcher that fights the operator is worse than none."""
    f = _Fake([(False, ["a"], True),        # stopped on purpose, still holds work
               (False, ["b"], False)])      # genuinely idle
    orig = _with(f)
    try:
        out = refill.refill_once()
    finally:
        refill.crawler_client = orig
    assert 0 not in f.resumed, "auto-refill restarted a crawl the operator had stopped"
    assert f.resumed.get(1) == ["b"], "a genuinely idle instance was skipped"
    assert out["stopped"] == 1 and out["resumed"] == 1, out


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} AUTO-REFILL TESTS PASSED")


if __name__ == "__main__":
    main()
