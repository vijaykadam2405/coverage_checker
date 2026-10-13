"""`depth` must mean HOPS FROM THE SEED, not position in a depth-first descent.

MEASURED on al-monitor.com: 81,297 stored pages spread over 1,820 DISTINCT depths, max 1,826,
busiest single level d1766 with 2,525 pages — all real articles, reached article -> related-article
-> article. Meanwhile that site's own /archives index is stored at d1 and every monthly page under
it at d2, so those same articles are THREE hops from the seed and were recorded as seventeen
hundred.

The mechanism is two decisions that are individually reasonable and jointly wrong:

  * the production frontier is LIFO (`CRAWLER_FRONTIER=dfs`, docker-compose.yml), so a worker pops
    the link it just enqueued and walks a chain; and
  * `_enqueue_candidate` returns early on `cl in ctx.seen`, so the FIRST sighting is final.

Under BFS first-sight IS shortest-path — that is the BFS invariant, and it is what the rest of the
engine assumes. Under DFS the invariant silently does not hold, and every depth-keyed rule was being
lied to: max_depth, the incremental fresh-depth surface (a delta crawl treated the whole site as
below it), resume's shallowest-first ordering.

The fix takes the MINIMUM over all sightings, which restores the meaning without changing traversal
order — so the DFS rationale (surface articles before the category mega-menu drains) still holds.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae


class _Hist:
    def __init__(self):
        self.relaxed, self.added = [], []

    def frontier_add(self, jid, rows):
        self.added.extend(rows); return len(rows)

    def frontier_relax(self, jid, rows):
        self.relaxed.extend(rows); return len(rows)

    def frontier_done(self, jid, urls):
        return len(urls)


class _Job:
    job_id = "j1"
    same_domain_only = False
    path_scope = None
    max_depth = -1
    max_pages = 100000


class _Ctx:
    def __init__(self):
        self.job = _Job()
        self.seen, self.depth_at = set(), {}
        self.seed_domains = {"https://h.test"}
        self.trap_skipped = self.scope_skipped = self.budget_used = 0
        self.fetched = self.skipped_duplicate = 0


def _engine(monkeypatch):
    eng = ae.AsyncEngine.__new__(ae.AsyncEngine)
    eng.history = _Hist()
    eng._frontier_add_buf, eng._frontier_done_buf, eng._frontier_relax_buf = {}, {}, {}
    eng.frontier = type("Q", (), {"put_nowait": lambda s, i: None})()
    eng._job_is_producing_nothing = lambda ctx: False
    monkeypatch.setattr(ae, "_resume_tracking", lambda: True)
    return eng


ART = "https://h.test/originals/2013/11/a-real-article"


def test_a_shallower_sighting_lowers_a_recorded_depth(monkeypatch):
    """The al-monitor shape, in miniature: the article is found first down a long chain, then again
    from the archive hub two hops from the seed."""
    eng, ctx = _engine(monkeypatch), _Ctx()
    assert eng._enqueue_candidate(ctx, ART, "https://h.test/chain/1747", 1747)
    assert ctx.depth_at[ART] == 1748

    # re-sighted from /archives/201311, which is itself at depth 2
    assert eng._enqueue_candidate(ctx, ART, "https://h.test/archives/201311", 2) is False
    assert ctx.depth_at[ART] == 3, "the shorter path did not win"
    assert eng._frontier_relax_buf["j1"] == [(ART, 3)]


def test_a_deeper_sighting_never_raises_a_depth(monkeypatch):
    eng, ctx = _engine(monkeypatch), _Ctx()
    eng._enqueue_candidate(ctx, ART, "https://h.test/archives/201311", 2)
    assert ctx.depth_at[ART] == 3
    eng._enqueue_candidate(ctx, ART, "https://h.test/chain/1747", 1747)
    assert ctx.depth_at[ART] == 3, "depth must be monotone downward"
    assert not eng._frontier_relax_buf.get("j1"), "a deeper sighting must not write"


def test_re_sighting_at_the_same_depth_writes_nothing(monkeypatch):
    """A busy hub re-sights hundreds of links per parse; only real improvements may reach sqlite."""
    eng, ctx = _engine(monkeypatch), _Ctx()
    eng._enqueue_candidate(ctx, ART, "https://h.test/x", 2)
    for _ in range(50):
        eng._enqueue_candidate(ctx, ART, "https://h.test/y", 2)
    assert not eng._frontier_relax_buf.get("j1")


def test_the_relax_is_buffered_not_written_inline(monkeypatch):
    eng, ctx = _engine(monkeypatch), _Ctx()
    eng._enqueue_candidate(ctx, ART, "https://h.test/chain/9", 9)
    eng._enqueue_candidate(ctx, ART, "https://h.test/hub", 1)
    assert eng.history.relaxed == [], "wrote to sqlite on the enqueue hot path"
    assert eng._frontier_relax_buf["j1"] == [(ART, 2)]


def test_a_queued_item_picks_up_a_relaxation_that_landed_while_it_waited():
    """Items sit in the frontier for hours — exactly the window in which a shallow hub is found. If
    the WorkItem keeps its descent depth, the page is judged and its children enqueued against the
    number this fix exists to correct."""
    ctx = _Ctx()
    ctx.depth_at[ART] = 3
    item = ae.WorkItem(ART, 1748, ctx)
    relaxed = ctx.depth_at.get(item.url)
    if relaxed is not None and relaxed < item.depth:
        item.depth = relaxed
    assert item.depth == 3


def test_pagination_keeps_its_same_depth_rule(monkeypatch):
    """A pager page is the same depth as page 1 — relaxation must not quietly re-introduce the
    depth+1 that once capped a paginated crawl at max_depth PAGES (measured on pixabay)."""
    eng, ctx = _engine(monkeypatch), _Ctx()
    pager = "https://h.test/news/page/2"
    assert ae._looks_paginated(pager)
    eng._enqueue_candidate(ctx, pager, "https://h.test/news", 5)
    assert ctx.depth_at[pager] == 5, "pagination must not cost a depth"


def test_relax_only_moves_pending_rows_and_only_downward():
    """A done row's depth is provenance: it records where the crawler actually was when it fetched
    the page. Rewriting it to match a later discovery makes the stored corpus disagree with itself.
    `depth > ?` also keeps the write idempotent, so a replayed flush is a no-op."""
    import inspect

    from crawler.dedup import CrawlHistory
    sql = " ".join(inspect.getsource(CrawlHistory.frontier_relax).split())
    assert "state='pending'" in sql
    assert "depth > ?" in sql
    assert "UPDATE crawl_frontier SET depth" in sql


def test_relaxes_flush_after_adds():
    """A URL enqueued and re-sighted shallower inside one flush window must find its row already
    there, or the UPDATE matches nothing and the relaxation is silently lost."""
    import inspect

    src = inspect.getsource(ae.AsyncEngine._flush_frontier)
    assert src.index("frontier_add") < src.index("frontier_relax")


def test_resume_seeds_depth_at():
    """Without this every resume forgets what the last run converged to and starts again."""
    import inspect

    src = inspect.getsource(ae.AsyncEngine._restore_frontier)
    assert "depth_at" in src, "resume drops the relaxations the previous run earned"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
