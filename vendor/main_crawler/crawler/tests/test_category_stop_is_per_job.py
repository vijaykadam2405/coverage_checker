"""A category switch must be able to stop SOME jobs. Stop used to be all-or-nothing.

THE FAULT, reported by the operator 2026-08-10: the master switch and the category switches were
both flipped off and the crawl kept going; only maintenance mode actually stopped it. For the
category switch the reason is structural — `request_stop()` sets ONE process-wide flag, so there was
no way to express "halt these jobs, leave the rest crawling". The switch could only ever gate the
next dispatch, and its live jobs ran to exhaustion hours later.

Two properties matter and neither is obvious:
  * a dropped job stops while an undropped one in the SAME batch keeps going, and
  * a dropped job ends "stopped", not "completed" — because "completed" is the status that drops the
    durable frontier (see test_frontier_is_not_silently_dropped), and an operator pausing a category
    for an afternoon must not lose its queue.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ae.config, "DATA_DIR", tmp_path)
    ae.set_dropped_jobs([])          # also resets the in-process cache
    yield tmp_path
    ae.set_dropped_jobs([])


def _ctx(job_id: str, max_pages: int = 0):
    """The two attributes reserve() and _job_end_status() actually read."""
    ctx = types.SimpleNamespace(
        job=types.SimpleNamespace(job_id=job_id, max_pages=max_pages),
        budget_used=0, done=False, was_dropped=False)
    ctx.reserve = types.MethodType(ae.JobCtx.reserve, ctx)
    return ctx


def test_dropping_one_job_leaves_the_others_crawling(data_dir):
    news, tender = _ctx("news_x"), _ctx("tender_y")
    ae.set_dropped_jobs(["news_x"])

    assert news.reserve() is False, "a job in the drop set must not be granted a page"
    assert tender.reserve() is True, (
        "STOP IS NOT ALL-OR-NOTHING ANY MORE — dropping the news job must leave the tender job in "
        "the same batch crawling, which is the entire point of a per-CATEGORY switch")
    assert news.budget_used == 0 and tender.budget_used == 1


def test_a_dropped_job_ends_stopped_so_its_frontier_survives(data_dir):
    ctx = _ctx("news_x")
    ae.set_dropped_jobs(["news_x"])
    ctx.reserve()

    assert ctx.was_dropped is True
    assert ae._job_end_status(ctx, interrupted=False) == "stopped", (
        "a job cut off by a category switch still has a live frontier. 'completed' is the status "
        "that DROPS it — that is how 389k dvidshub URLs were lost — so a switched-off category "
        "must report the same way an operator STOP does")


def test_switching_the_category_back_on_revives_the_job(data_dir):
    """The skip must NOT latch into `done`, which is a one-way gate."""
    ctx = _ctx("news_x")
    ae.set_dropped_jobs(["news_x"])
    assert ctx.reserve() is False
    assert ctx.done is False, "being dropped is reversible; `done` is not, so it must stay clear"

    ae.set_dropped_jobs([])
    assert ctx.reserve() is True, "switching the category back on must let its jobs run again"


def test_max_pages_still_wins_and_still_reports_max_pages(data_dir):
    """Guard against the drop check swallowing the budget path it sits next to."""
    ctx = _ctx("news_x", max_pages=1)
    assert ctx.reserve() is True
    assert ctx.reserve() is False
    assert ctx.done is True and ctx.was_dropped is False
    assert ae._job_end_status(ctx, interrupted=False) == "completed"


def test_the_set_survives_the_process(data_dir):
    """It is a file for the same reason the stop flag is: child workers and restarts."""
    ae.set_dropped_jobs(["a", "b"])
    ae._DROPS = (-1.0, frozenset())          # simulate a fresh process with a cold cache
    assert ae.dropped_jobs() == frozenset({"a", "b"})
    assert ae.job_dropped("a") and not ae.job_dropped("c")


def test_an_empty_set_is_not_a_stop_everything(data_dir):
    """The failure mode that would be catastrophic and silent: an empty drop set matching every
    job. `job_dropped` must be false for everything when nothing is dropped."""
    ae.set_dropped_jobs([])
    assert not ae.dropped_jobs()
    for jid in ("", "anything", "news_x"):
        assert ae.job_dropped(jid) is False
    assert _ctx("news_x").reserve() is True


def test_a_corrupt_drop_file_does_not_stop_the_crawl(data_dir):
    (data_dir / ".crawl_drop").write_text("{not json")
    ae._DROPS = (-1.0, frozenset())
    assert ae.dropped_jobs() == frozenset()
    assert _ctx("news_x").reserve() is True


def test_the_worker_loops_check_the_drop_set():
    """The choke point in reserve() is correctness; this is what makes a switched-off category stop
    within seconds instead of walking its whole queue through a tab and a host slot first.

    Pinned by source because the alternative is standing up the whole engine. Comments are stripped
    first — this suite has three times reported a green result that only matched its own prose."""
    import inspect
    import re

    src = inspect.getsource(ae)
    src = re.sub(r"#.*", "", src)
    hits = len(re.findall(r"if stop_requested\(\) or _item_dropped\(item\):", src))
    assert hits >= 2, (
        "both pool worker loops must release a dropped item at the top, next to the STOP check; "
        "found %d" % hits)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
