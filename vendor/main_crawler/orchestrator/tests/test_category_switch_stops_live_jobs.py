"""A switch that only gates the NEXT dispatch is not a switch. And "is a crawl running" is a
question about the FLEET, not about instance 0.

REPORTED 2026-08-10: "yesterday when i tried stopping crawling through master switch and category
switch both didn't work, so i clicked maintenance mode — i think that stopped everything properly".

Three separate defects produced that one experience, and this file covers two of them (the master
switch is in test_master_switch.py):

  * `toggle_category` wrote a Setting and stopped. The Setting is only read when the scheduler
    decides what to DISPATCH, so a category switched off mid-crawl kept crawling to exhaustion.
  * `control_state` asked `batch_status()` with no base — instance 0 of twelve. So the panel could
    report "no crawl running" over eleven crawling instances, and the master-switch dialog could
    not warn that a crawl was in flight. The health view could not see what the operator could.

Runnable directly:  python tests/test_category_switch_stops_live_jobs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api  # noqa: E402


class _Fleet:
    """crawler_client for N instances. `running` = which instances report a live batch."""

    def __init__(self, n=3, running=(), down=(), dropped=()):
        self.n, self.running, self.down = n, set(running), set(down)
        self.dropped = list(dropped)
        self.drop_calls: list[list[str]] = []

    def fanout(self, fn, *a, **kw):
        rows = []
        for i in range(self.n):
            if i in self.down:
                rows.append({"instance": i, "ok": False, "error": "connection refused"})
                continue
            rows.append({"instance": i, "ok": True, "data": fn(i, *a, **kw)})
        return rows

    # fanout passes the instance index as the first arg to these stubs
    def batch_status(self, i):
        return {"running": i in self.running, "dropped_jobs": list(self.dropped)}

    def drop_jobs(self, i, job_ids):
        self.drop_calls.append(list(job_ids))
        self.dropped = list(job_ids)
        return {"dropped": sorted(job_ids), "count": len(job_ids)}


def _with_fleet(fleet, fn):
    saved = api.crawler_client
    api.crawler_client = fleet
    try:
        return fn()
    finally:
        api.crawler_client = saved


# ── health: the fleet is the unit ────────────────────────────────────────────

def t_a_crawl_on_any_instance_counts_as_running() -> None:
    """THE HEALTH BUG. Instance 0 idle while 1 and 2 crawl used to read as "nothing running"."""
    out = _with_fleet(_Fleet(n=3, running={1, 2}), api._fleet_running)
    assert out["batch_running"] is True, out
    assert out["running_instances"] == [1, 2], out
    assert out["instances"] == {"up": 3, "down": 0, "total": 3}, out


def t_an_idle_fleet_reads_idle() -> None:
    """Negative control: without it, a check that always said "running" would pass the test above
    and permanently refuse the flush guard that depends on this answer."""
    out = _with_fleet(_Fleet(n=3), api._fleet_running)
    assert out["batch_running"] is False and out["running_instances"] == [], out


def t_a_down_instance_is_reported_not_subtracted() -> None:
    """A quieter fleet and a smaller fleet must not look the same — the one rule this dashboard
    has. A down instance may well still be crawling; it simply is not answering."""
    out = _with_fleet(_Fleet(n=4, running={1}, down={3}), api._fleet_running)
    assert out["instances"] == {"up": 3, "down": 1, "total": 4}, out
    assert out["batch_running"] is True, out


def t_a_whole_fleet_down_is_unreachable_not_idle() -> None:
    out = _with_fleet(_Fleet(n=2, down={0, 1}), api._fleet_running)
    assert out["reachable"] is False and out["batch_running"] is False, out


def t_held_jobs_make_a_partial_stop_visible() -> None:
    """A crawl running with half its jobs switched off is a real state and the panel has to be able
    to say so — otherwise the category switch again looks like it did nothing."""
    out = _with_fleet(_Fleet(n=2, running={0}, dropped=["news_a", "news_b"]), api._fleet_running)
    assert out["batch_running"] is True and out["held_jobs"] == 2, out


# ── the category switch must reach the running crawl ─────────────────────────

class _SJ:
    def __init__(self, job_id, bucket):
        self.job = {"job_id": job_id}
        self.bucket = bucket


class _DB:
    def __init__(self, jobs):
        self._jobs = jobs

    def scalars(self, _q):
        return type("R", (), {"all": lambda _s: list(self._jobs)})()


def _push_with(fleet, jobs, disabled):
    """_push_drop_set with the two module collaborators it reads stubbed."""
    saved = (api.crawler_client, api.scheduler, api.categories, api.select)
    api.crawler_client = fleet
    api.scheduler = type("S", (), {"disabled_categories": staticmethod(lambda _db: set(disabled))})()
    api.categories = type("C", (), {
        "catalog_buckets": staticmethod(lambda _db: {}),
        "bucket_of_job": staticmethod(
            lambda job, _bs: next(j.bucket for j in jobs if j.job["job_id"] == job["job_id"])),
    })()
    api.select = lambda _m: None
    try:
        return api._push_drop_set(_DB(jobs))
    finally:
        api.crawler_client, api.scheduler, api.categories, api.select = saved


def t_switching_a_category_off_pushes_exactly_its_jobs() -> None:
    """THE CATEGORY BUG. Only the disabled bucket's jobs are held; everything else keeps crawling —
    which is the one thing neither the master switch nor maintenance mode can do."""
    jobs = [_SJ("news_a", "news"), _SJ("news_b", "news"), _SJ("mfg_a", "manufacture")]
    fleet = _Fleet(n=3, running={0, 1, 2})
    out = _push_with(fleet, jobs, disabled={"news"})
    assert out["held_jobs"] == 2 and out["instances_acked"] == 3, out
    assert fleet.drop_calls == [["news_a", "news_b"]] * 3, fleet.drop_calls


def t_re_enabling_pushes_an_empty_set_not_nothing() -> None:
    """Switching back ON has to actively clear the set. Sending nothing would leave the crawler
    holding a stale drop set forever — a category that could never be restarted."""
    jobs = [_SJ("news_a", "news")]
    fleet = _Fleet(n=2, dropped=["news_a"])
    out = _push_with(fleet, jobs, disabled=set())
    assert out["held_jobs"] == 0, out
    assert fleet.drop_calls == [[], []], fleet.drop_calls


def t_the_full_set_is_sent_every_time_not_a_delta() -> None:
    """Two categories off means BOTH sets go out together. A delta would let an instance that
    missed one call stay permanently out of step."""
    jobs = [_SJ("news_a", "news"), _SJ("t_a", "tender"), _SJ("mfg_a", "manufacture")]
    fleet = _Fleet(n=1)
    out = _push_with(fleet, jobs, disabled={"news", "tender"})
    assert fleet.drop_calls == [["news_a", "t_a"]], fleet.drop_calls
    assert out["held_jobs"] == 2, out


def t_an_unreachable_instance_is_named_not_swallowed() -> None:
    """An instance that never got the message keeps crawling the category. Saying so is the whole
    difference between "the switch worked" and "the switch worked on 11 of 12"."""
    jobs = [_SJ("news_a", "news")]
    fleet = _Fleet(n=3, down={2})
    out = _push_with(fleet, jobs, disabled={"news"})
    assert out["instances_acked"] == 2 and out["instances_total"] == 3, out
    assert out["unreachable"] == [2], out


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CATEGORY-SWITCH / FLEET-HEALTH TESTS PASSED")


def test_category_switch_suite() -> None:
    main()


if __name__ == "__main__":
    main()
