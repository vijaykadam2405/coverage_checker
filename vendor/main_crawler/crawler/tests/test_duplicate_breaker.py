"""Stop a job whose fetches are all duplicates — the guard that no new URL shape can dodge.

Every other trap guard matches a URL SHAPE, and shapes are endless. cyientdlm.com proved that
twice in one day:

  shape 1   /about-us/about-us/capabilities/capabilities/industries/…   (repeated segments)
  shape 2   /com/cyient/www/prlisting/about-us/industries/capabilities/prlisting/…

The repeated-segment rule was written against shape 1 and shipped; the very next run produced
shape 2 — the site's own host written backwards into the path, nine segments deep, with only ONE
segment repeating, so the rule missed all 156 of them. By then that job had fetched 17,265 pages
and stored 48 documents (0.3%).

This guard reads the OUTCOME instead. A site serving one page under an unbounded set of URLs
produces duplicates, whatever the URLs look like.

Runnable directly:  python tests/test_duplicate_breaker.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.async_engine import AsyncEngine  # noqa: E402


class _Job:
    job_id = "cat_all_example"


class _Ctx:
    """Only the four attributes the breaker reads."""

    def __init__(self, fetched: int, dupes: int):
        self.job = _Job()
        self.fetched = fetched
        self.skipped_duplicate = dupes


def _engine():
    # __new__: the breaker touches no engine state, and building a real engine would start a
    # browser pool.
    return AsyncEngine.__new__(AsyncEngine)


def _check(fetched: int, dupes: int) -> bool:
    return _engine()._job_is_producing_nothing(_Ctx(fetched, dupes))


def test_the_real_case_trips_it():
    """cyientdlm as measured: 17,265 fetched, ~99.7% duplicates."""
    assert _check(17265, 17217) is True


def test_a_healthy_site_never_trips_it():
    """The number that must never be a false positive. A site where most pages are new is the
    normal case, and refusing to enqueue there would silently truncate a good crawl."""
    assert _check(5000, 100) is False        # 2% duplicates
    assert _check(5000, 2500) is False       # 50% — high, but half the pages are real
    assert _check(5000, 4000) is False       # 80% — still under the bar


def test_it_waits_for_enough_evidence():
    """A small job can be 100% duplicates by luck — a re-crawl of a site that has not changed hits
    exactly that. The floor is what stops a 3-page run from being called a trap."""
    assert _check(3, 3) is False
    assert _check(299, 299) is False
    assert _check(300, 300) is True


def test_a_zero_page_job_does_not_divide_by_zero():
    assert _check(0, 0) is False


def test_it_can_be_turned_off():
    """An operator crawling a deliberately duplicate-heavy source needs an escape hatch, and a
    guard with no off switch is one that gets deleted instead of tuned."""
    os.environ["CRAWLER_DUP_BREAKER_MIN"] = "0"
    try:
        assert _check(17265, 17217) is False
    finally:
        del os.environ["CRAWLER_DUP_BREAKER_MIN"]


def test_the_threshold_is_tunable():
    os.environ["CRAWLER_DUP_BREAKER_RATIO"] = "0.5"
    try:
        assert _check(5000, 2600) is True     # 52% now trips
    finally:
        del os.environ["CRAWLER_DUP_BREAKER_RATIO"]


def test_it_only_warns_once_per_job():
    """This is called for every candidate link on every page. Logging each one would bury the log
    under the very trap it is reporting."""
    eng, ctx = _engine(), _Ctx(17265, 17217)
    assert eng._job_is_producing_nothing(ctx) is True
    assert getattr(ctx, "_dup_breaker_logged", False) is True
    assert eng._job_is_producing_nothing(ctx) is True      # still refuses, just does not re-log


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} DUPLICATE-BREAKER TESTS PASSED")


if __name__ == "__main__":
    main()
