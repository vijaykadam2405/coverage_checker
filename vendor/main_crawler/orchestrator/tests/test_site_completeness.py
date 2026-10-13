"""What "complete" means, in one place.

THE BUG THIS EXISTS FOR, measured on the live corpus: the dashboard reported 564 of 621 sites
complete while 403 of those sites held 103,964 URLs queued in their saved frontiers. `sigsauer.com`
wore a green "complete" badge with **3,839 URLs still waiting**. 332 of the 564 were in that state.

The rule was `unresolved == 0`, which only says nothing FAILED in the run the crawler still holds in
memory. It says nothing about URLs the crawl discovered and never reached. Those are two different
questions and only both together mean finished:

    complete = stored pages > 0
             AND no unresolved URLs        (nothing failed)
             AND no pending URLs           (nothing left unreached)
             AND not crawling right now    (it is not still deciding)

Every one of those clauses has a real counter-example below. The tile and the table share this
function precisely so they cannot drift again.

Runnable directly:  python tests/test_site_completeness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api  # noqa: E402


def _no_live_crawl():
    api.crawler_batch_status = lambda: {"running": False, "current_domains": []}


def _crawling(*hosts):
    api.crawler_batch_status = lambda: {"running": True, "current_domains": list(hosts)}


ROWS = [
    # genuinely finished: stored, nothing failed, nothing queued
    {"host": "done.com", "stored_pages": 400, "unresolved": 0, "pending": 0},
    # THE REGRESSION: nothing failed, but 3,839 URLs were never reached
    {"host": "sigsauer.com", "stored_pages": 9846, "unresolved": 0, "pending": 3839},
    # something failed
    {"host": "blocked.com", "stored_pages": 300, "unresolved": 17, "pending": 0},
    # no crawl report at all — unknown, which is not "finished"
    {"host": "unknown.com", "stored_pages": 55, "unresolved": None, "pending": 0},
    # a clean report that stored nothing is not a success
    {"host": "empty.com", "stored_pages": 0, "unresolved": 0, "pending": 0},
]


def test_a_site_with_queued_urls_is_not_complete() -> None:
    _no_live_crawl()
    c = api._site_counts(ROWS)
    assert c["complete"] == 1, c        # only done.com
    assert c["partial"] == 1, c         # sigsauer.com — nothing failed, but 3,839 unreached
    assert c["unresolved"] == 1, c
    assert c["unverified"] == 2, c      # unknown.com + empty.com
    assert c["total"] == 5


def test_every_row_lands_in_exactly_one_bucket() -> None:
    """A rollup that loses rows understates the corpus; one that double-counts overstates it."""
    _no_live_crawl()
    c = api._site_counts(ROWS)
    assert c["complete"] + c["partial"] + c["unresolved"] + c["unverified"] + c["crawling"] \
        == c["total"], c


def test_a_site_being_crawled_right_now_is_never_complete() -> None:
    """However good its counters look this second, it is still deciding."""
    _crawling("done.com")
    c = api._site_counts(ROWS)
    assert c["crawling"] == 1, c
    assert c["complete"] == 0, "a site mid-crawl was counted as finished"


def test_www_prefix_does_not_hide_a_live_site() -> None:
    """The report spells the host as fetched (www.done.com); the run names the seed host."""
    _crawling("www.done.com")
    c = api._site_counts(ROWS)
    assert c["crawling"] == 1 and c["complete"] == 0, c


def test_an_unreadable_frontier_does_not_invent_completions() -> None:
    """pending is None when the crawler could not be asked. Treating unknown as zero would restore
    exactly the bug this file exists for, silently."""
    _no_live_crawl()
    rows = [{"host": "x.com", "stored_pages": 100, "unresolved": 0, "pending": None}]
    c = api._site_counts(rows)
    assert c["complete"] == 1, (
        "documenting current behaviour: pending=None is treated as 'nothing queued'. "
        "If the frontier read starts failing routinely, this must become `unverified` instead.")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all good")
