"""JS pages must be captured when they are FINISHED, not after a fixed sleep.

Measured on the live corpus with the old budget (networkidle 1500ms + 500ms settle):

    renault-trucks.com   snapshotted at 25% of its final DOM   (44K of 174K)
    zf.com               70%
    scrapingcourse.com   83%
    rohde-schwarz.com    98%

All four were stored as complete pages. A networkidle timeout is swallowed, so nothing downstream
could tell a finished render from one caught mid-hydration — and renault-trucks was simultaneously
the worst render_crash host in the batch (378).

Runnable directly:  python tests/test_render_settle.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402
from crawler.dedup import CrawlHistory  # noqa: E402


class _Tab:
    """A fake page whose DOM size follows a script, one entry per poll."""

    def __init__(self, sizes, explode_at=None):
        self.sizes, self.i, self.waits = list(sizes), 0, 0
        self.explode_at = explode_at

    async def evaluate(self, _expr):
        if self.explode_at is not None and self.i >= self.explode_at:
            raise RuntimeError("Target closed")
        v = self.sizes[min(self.i, len(self.sizes) - 1)]
        self.i += 1
        return v

    async def wait_for_timeout(self, ms):
        self.waits += ms


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def t_a_settled_page_returns_immediately() -> None:
    """A static page is stable on the first samples and must not pay the full cap — this path is
    hit by every page in the corpus, so a lazy 12s sleep here would be catastrophic."""
    tab = _Tab([5000, 5000, 5000, 5000])
    grew = _run(ae._wait_dom_settle(tab))
    assert grew is False, "a stable DOM was reported as still growing"
    assert tab.waits <= 1200, f"waited {tab.waits}ms on an already-settled page"


def t_a_growing_page_is_waited_for() -> None:
    """The renault-trucks case: DOM keeps growing well past the old 2s budget, then stops."""
    tab = _Tab([44_000, 60_000, 90_000, 174_000, 174_000, 174_000])
    grew = _run(ae._wait_dom_settle(tab))
    assert grew is False, "the page settled but was reported as incomplete"
    # It must have kept sampling until the size repeated, not stopped at the first reading.
    assert tab.i >= 5, f"stopped after {tab.i} samples — it would have captured the 44K version"


def t_a_page_that_never_settles_is_reported_not_silently_truncated() -> None:
    """Carousels/tickers never stop changing. Capping is correct; hiding it is not."""
    import itertools
    tab = _Tab(list(itertools.islice(itertools.count(1000, 137), 500)))
    grew = _run(ae._wait_dom_settle(tab))
    assert grew is True, "hit the cap while still growing but reported the page as complete"


def t_the_cap_is_actually_enforced() -> None:
    import os
    os.environ["CRAWLER_DOM_SETTLE_MAX_MS"] = "900"
    os.environ["CRAWLER_DOM_SETTLE_STEP_MS"] = "300"
    try:
        import itertools
        tab = _Tab(list(itertools.islice(itertools.count(1, 7), 500)))
        _run(ae._wait_dom_settle(tab))
        assert tab.waits <= 900, f"ran {tab.waits}ms past a 900ms cap"
    finally:
        for k in ("CRAWLER_DOM_SETTLE_MAX_MS", "CRAWLER_DOM_SETTLE_STEP_MS"):
            os.environ.pop(k, None)


def t_settle_can_be_switched_off() -> None:
    import os
    os.environ["CRAWLER_DOM_SETTLE_MAX_MS"] = "0"
    try:
        tab = _Tab([1, 2, 3])
        assert _run(ae._wait_dom_settle(tab)) is False
        assert tab.i == 0 and tab.waits == 0, "did work despite being disabled"
    finally:
        os.environ.pop("CRAWLER_DOM_SETTLE_MAX_MS", None)


def t_a_dead_tab_does_not_raise_out_of_the_settle_loop() -> None:
    """The tab can navigate away or crash mid-wait. That must reach the caller's own handler as a
    normal render failure, not blow up inside the settle helper."""
    tab = _Tab([100, 200, 300], explode_at=1)
    assert _run(ae._wait_dom_settle(tab)) is False


def t_failures_persist_the_error_text() -> None:
    """`render_crash` is a catch-all for every browser exception. Without the raw text, 2,616 URLs
    carried that label with no way to tell a nav timeout from a crashed tab."""
    import tempfile
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h.sqlite"))
    url = "https://x.test/a"
    h.record_failure(url, status=None, category="render_crash", failed_at="2026-07-22T00:00:00Z",
                     detail="render:Timeout 30000ms exceeded while waiting for load state")
    row = h._conn.execute(
        "SELECT error_category, error_detail FROM crawl_pages WHERE canonical_url=?",
        (url,)).fetchone()
    assert row[0] == "render_crash"
    assert row[1] and "Timeout 30000ms" in row[1], f"error text was not persisted: {row[1]!r}"
    # Long browser tracebacks must not bloat the row.
    h.record_failure(url, status=None, category="render_crash", failed_at="x", detail="z" * 5000)
    row = h._conn.execute("SELECT length(error_detail) FROM crawl_pages WHERE canonical_url=?",
                          (url,)).fetchone()
    assert row[0] <= 300, f"stored {row[0]} chars of error text"


def t_detail_is_optional_so_old_callers_still_work() -> None:
    import tempfile
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h2.sqlite"))
    h.record_failure("https://x.test/b", status=404, category="http_404", failed_at="t")
    assert h.get("https://x.test/b") is not None


def main() -> None:
    for fn in (t_a_settled_page_returns_immediately,
               t_a_growing_page_is_waited_for,
               t_a_page_that_never_settles_is_reported_not_silently_truncated,
               t_the_cap_is_actually_enforced,
               t_settle_can_be_switched_off,
               t_a_dead_tab_does_not_raise_out_of_the_settle_loop,
               t_failures_persist_the_error_text,
               t_detail_is_optional_so_old_callers_still_work):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL RENDER-SETTLE TESTS PASSED")



def test_render_settle_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
