"""A job with work still queued must not have that queue deleted, and never silently.

MEASURED, and this file exists because the investigation was nearly impossible. ~389,000 pending
dvidshub URLs disappeared from the fleet. Nothing recorded it: not the row count, not the job id,
not the caller. Hours went into ruling out a junk filter (0 of 25 URLs matched; every prune returned
409), the schedule being disabled, and database corruption — before the cause was found by reading
the code rather than any log.

Two paths delete a whole job frontier, and both were silent:

  1. END OF BATCH. `_job_end_status` returns "completed" for any batch that was not operator-stopped
     or wall-clock chopped — "completed" is the DEFAULT, not a conclusion — and "completed" drops
     the frontier so the job leaves the Resume list. A job that ended for any other reason was
     therefore declared finished and had its entire queue deleted with 349,068 URLs pending.

  2. START OF BATCH. `resume` defaults to False, so a re-dispatch that forgets to set it wipes
     whatever that job had queued before fetching a single page.

Path 1 is the contradiction: "completed" with tens of thousands of URLs pending is not completion in
any sense the word carries. The one end that legitimately leaves a large queue behind is a max_pages
budget, and _job_end_status checks that FIRST, so it never reaches the clear.
"""
from __future__ import annotations

import inspect
import logging
import tempfile
from pathlib import Path

import pytest

from crawler import async_engine as ae
from crawler.dedup import CrawlHistory


def _hist(tmp, job="j", pending=0):
    h = CrawlHistory(Path(tmp) / "h.sqlite")
    if pending:
        h.frontier_add(job, [("https://h.test/%d" % i, 1, None, False) for i in range(pending)])
    return h


def test_clearing_a_big_frontier_is_logged_loudly(caplog):
    with tempfile.TemporaryDirectory() as d:
        h = _hist(d, pending=1500)
        with caplog.at_level(logging.WARNING):
            h.frontier_clear("j")
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "1500" in msg and "PENDING" in msg, (
            "dropping 1,500 queued URLs produced no warning: %r" % msg)
        h.close()


def test_a_small_clear_is_still_recorded(caplog):
    """Routine, but it must not be invisible — invisibility is what made the original loss
    un-investigable."""
    with tempfile.TemporaryDirectory() as d:
        h = _hist(d, pending=3)
        with caplog.at_level(logging.INFO):
            h.frontier_clear("j")
        assert any("frontier_clear" in r.getMessage() for r in caplog.records)
        h.close()


def test_clear_still_actually_clears():
    with tempfile.TemporaryDirectory() as d:
        h = _hist(d, pending=20)
        assert h.frontier_counts("j")["pending"] == 20
        assert h.frontier_clear("j") == 20
        assert h.frontier_counts("j")["pending"] == 0
        h.close()


def test_the_end_of_batch_path_keeps_a_large_frontier():
    """The guard, at the site that caused the loss: a 'completed' job with a big queue keeps it."""
    src = inspect.getsource(ae.AsyncEngine.run)
    seg = src[src.index('== "completed"'):]
    seg = seg[:seg.index("frontier_clear")]
    assert "CRAWLER_KEEP_FRONTIER_ABOVE" in seg, (
        "the completed-branch clears the frontier without checking what is still pending")
    assert 'statuses[c.job.job_id] = "stopped"' in seg, (
        "a job whose frontier is kept must also be reported as stopped, or it stays invisible "
        "in the Resume list and the queue is preserved but unreachable")


def test_the_start_of_batch_path_warns_before_dropping():
    src = inspect.getsource(ae.AsyncEngine.run)
    i = src.index("WITHOUT resume")
    seg = src[max(0, i - 900):i + 900]
    assert "frontier_counts" in seg and "CRAWLER_KEEP_FRONTIER_ABOVE" in seg, (
        "a fresh dispatch drops an existing frontier without checking or warning")


def test_max_pages_is_decided_before_the_guard_can_see_it():
    """A budget-capped job SHOULD clear — it did what it was asked and a resume would stop again
    immediately. _job_end_status must keep returning 'completed' for it, so the guard never has to
    special-case the one legitimate large-queue ending."""
    class _J:
        max_pages = 100
        job_id = "j"

    class _C:
        job = _J()
        budget_used = 100
    assert ae._job_end_status(_C(), interrupted=False) == "completed"
    assert ae._job_end_status(_C(), interrupted=True) == "completed"


def test_an_interrupted_job_is_still_stopped():
    class _J:
        max_pages = 100000
        job_id = "j"

    class _C:
        job = _J()
        budget_used = 5
    assert ae._job_end_status(_C(), interrupted=True) == "stopped"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
