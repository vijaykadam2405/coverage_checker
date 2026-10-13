"""Crawl resume: an interrupted crawl must pick up where it stopped, not re-walk the site.

WHY THIS EXISTS
---------------
Content-hash dedup already guarantees an interrupted-and-restarted crawl produces correct DATA —
the duplicates never enter the corpus. What it does not save is TIME: every page is fetched a
second time to discover the links that lead to the pages we actually still need. On a large site
that is hours of re-fetching for zero new content, and it happens on every operator stop, container
restart or dropped connection.

So the thing to test is not "does the data survive" (it always did) but "does the SECOND run avoid
re-fetching what the first run already did".

Runnable directly:  python tests/test_resume.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.dedup import CrawlHistory  # noqa: E402


def _h(tmp: Path) -> CrawlHistory:
    return CrawlHistory(tmp / "h.sqlite")


def t_pending_and_done_round_trip() -> None:
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        h.frontier_add("j", [("http://a/1", 0, None, False),
                             ("http://a/2", 1, "http://a/1", False),
                             ("http://a/3", 1, "http://a/1", True)])
        h.frontier_done("j", ["http://a/1"])
        assert h.frontier_counts("j") == {"pending": 2, "done": 1, "total": 3}
        pend = h.frontier_pending("j")
        assert [p[0] for p in pend] == ["http://a/2", "http://a/3"]
        assert pend[1][3] is True, "needs_render must survive the round trip, or a render-lane " \
                                   "page comes back on the httpx lane and is fetched twice"
        assert h.frontier_seen("j") == {"http://a/1", "http://a/2", "http://a/3"}
        h.close()


def t_done_urls_are_never_resurrected_as_pending() -> None:
    """The crawl re-discovers links constantly — the same URL is offered for enqueue many times. If
    a re-offer flipped a done row back to pending, resume would re-fetch pages it already had, i.e.
    exactly the cost this feature removes."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        h.frontier_add("j", [("http://a/1", 0, None, False)])
        h.frontier_done("j", ["http://a/1"])
        h.frontier_add("j", [("http://a/1", 0, None, False)])       # re-discovered
        assert h.frontier_counts("j")["pending"] == 0
        assert h.frontier_counts("j")["done"] == 1
        h.close()


def t_a_fresh_run_does_not_inherit_a_stale_frontier() -> None:
    """A normal re-crawl must be a real re-crawl. If it silently inherited the previous attempt's
    `done` set it would skip most of the site and report a suspiciously fast, mostly-empty run."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        h.frontier_add("j", [("http://a/1", 0, None, False)])
        h.job_state_save("j", pages_done=5, status="stopped", job_json='{"job_id":"j"}')
        assert h.frontier_counts("j")["total"] == 1
        h.frontier_clear("j")
        assert h.frontier_counts("j")["total"] == 0
        assert h.job_state("j") is None
        h.close()


def t_job_config_is_stored_so_resume_needs_only_a_job_id() -> None:
    """Resume must not depend on the operator re-supplying seed_urls/max_depth/capture — any
    difference would quietly make it a different crawl rather than a continuation."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        cfg = '{"job_id":"j","job_type":"news","seed_urls":["https://a.com/"],"max_pages":500}'
        h.job_state_save("j", pages_done=0, status="running", job_json=cfg)
        # A later progress tick carries no config; it must not erase the stored one.
        h.job_state_save("j", pages_done=42, status="stopped")
        st = h.job_state("j")
        assert st["job_json"] == cfg, "progress tick erased the job config"
        assert st["pages_done"] == 42
        h.close()


def t_resumable_list_shows_only_unfinished_jobs() -> None:
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        h.frontier_add("unfinished", [("http://a/1", 0, None, False)])
        h.job_state_save("unfinished", pages_done=3, status="stopped", job_json="{}")
        h.frontier_add("finished", [("http://b/1", 0, None, False)])
        h.frontier_done("finished", ["http://b/1"])
        h.job_state_save("finished", pages_done=1, status="completed", job_json="{}")
        ids = {r["job_id"] for r in h.resumable_jobs()}
        assert ids == {"unfinished"}, ids
        row = [r for r in h.resumable_jobs() if r["job_id"] == "unfinished"][0]
        assert row["pending"] == 1 and row["pages_done"] == 3 and row["has_config"]
        h.close()


def t_resume_restores_frontier_seen_and_budget_into_the_engine() -> None:
    """End-to-end on the engine's own restore path: the pending URLs must land back on the right
    LANE, every known URL must be in `seen`, and the spent budget must be restored — otherwise a
    resumed job gets a fresh max_pages allowance and overshoots by the amount already crawled."""
    import asyncio

    from crawler import async_engine as ae
    from crawler.models import Job

    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        h.frontier_add("j", [("http://a/1", 0, None, False), ("http://a/2", 1, "http://a/1", False),
                             ("http://a/3", 1, "http://a/1", True), ("http://a/4", 2, None, False)])
        h.frontier_done("j", ["http://a/1", "http://a/2"])
        h.job_state_save("j", pages_done=2, status="stopped", job_json="{}")

        async def run():
            e = ae.AsyncEngine.__new__(ae.AsyncEngine)
            e.frontier = asyncio.Queue()
            e.render_q = asyncio.Queue()
            e.history = h

            job = Job(job_id="j", job_type="news", seed_urls=["http://a/1"], resume=True,
                      max_pages=10)

            class _Ctx:
                def __init__(self):
                    self.job = job
                    self.seen: set = set()
                    self.budget_used = 0
            ctx = _Ctx()
            n = e._restore_frontier(ctx)
            assert n == 2, f"expected 2 pending, got {n}"
            # Already-fetched URLs are in `seen`, so re-discovering them enqueues nothing.
            assert ctx.seen == {"http://a/1", "http://a/2", "http://a/3", "http://a/4"}, ctx.seen
            assert ctx.budget_used == 2, ctx.budget_used
            # The render-lane URL must come back on the RENDER lane, not the httpx one.
            assert e.render_q.qsize() == 1 and e.frontier.qsize() == 1
            assert (await e.render_q.get()).url == "http://a/3"
            assert (await e.frontier.get()).url == "http://a/4"
        asyncio.run(run())
        h.close()


def t_buffered_tracking_survives_a_flush_and_feeds_the_next_resume() -> None:
    """The engine does NOT write the frontier inline — it buffers and flushes in batches, because a
    sqlite round trip per enqueued URL would sit on the frontier's hot path. This walks that real
    path (track -> flush -> restore) rather than the DB directly, since a buffer that never flushed
    would pass every other test here and still lose the entire resume."""
    import asyncio

    from crawler import async_engine as ae
    from crawler.models import Job

    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))

        async def run():
            e = ae.AsyncEngine.__new__(ae.AsyncEngine)
            e.frontier = asyncio.Queue()
            e.render_q = asyncio.Queue()
            e.history = h
            e._frontier_add_buf, e._frontier_done_buf, e._frontier_relax_buf = {}, {}, {}

            job = Job(job_id="buf", job_type="news", seed_urls=["http://a/1"], resume=True)

            class _Ctx:
                def __init__(self):
                    self.job = job
                    self.seen: set = set()
                    self.budget_used = 0
            ctx = _Ctx()

            for i in range(5):
                e._frontier_track(ctx, f"http://a/{i}", i, None, needs_render=(i == 4))
            e._frontier_complete(ctx, "http://a/0")
            e._frontier_complete(ctx, "http://a/1")
            # Nothing is durable yet — that is the point of buffering.
            assert h.frontier_counts("buf")["total"] == 0, "buffer wrote through unexpectedly"
            await e._flush_frontier(force=True)
            assert h.frontier_counts("buf") == {"pending": 3, "done": 2, "total": 5}, \
                h.frontier_counts("buf")

            # Now resume into a fresh context, as a restarted process would.
            ctx2 = _Ctx()
            e2 = ae.AsyncEngine.__new__(ae.AsyncEngine)
            e2.frontier, e2.render_q, e2.history = asyncio.Queue(), asyncio.Queue(), h
            assert e2._restore_frontier(ctx2) == 3
            assert len(ctx2.seen) == 5, "all known URLs must be seen, or they get re-fetched"
            assert e2.render_q.qsize() == 1, "the needs_render URL must return to the render lane"
        asyncio.run(run())
        h.close()


def t_a_stopped_item_is_not_recorded_as_done() -> None:
    """The bug a live interrupted crawl exposed, and that no unit test had caught.

    On STOP the worker does `continue` to drop the item unprocessed — but `continue` still runs the
    `finally` block, so marking the frontier done there recorded every dropped item as if it had
    been crawled. MEASURED on a real interrupted run: 608 URLs persisted, **0 pending**, and the
    resume had nothing to resume. The whole feature was silently a no-op in the one situation it
    exists for.

    Both worker loops must therefore mark done only when `_process` actually returned. This asserts
    that at the source level, because reproducing a full stop-mid-crawl in a unit test would need a
    browser pool and a live site."""
    import inspect
    import re

    from crawler import async_engine as ae

    for fn in (ae.AsyncEngine._worker, ae.AsyncEngine._pool_worker):
        src = inspect.getsource(fn)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        name = fn.__name__
        assert "processed = False" in code, f"{name}: no processed flag — a dropped item marks done"
        assert "processed = True" in code, f"{name}: processed is never set"
        assert re.search(r"if processed:\s*\n\s*self\._frontier_complete", code), \
            f"{name}: _frontier_complete is not gated on processed"
        # The flag must be initialised BEFORE the try, or the `continue` path raises NameError.
        assert code.index("processed = False") < code.index("try:"), \
            f"{name}: processed initialised after the try — the STOP path would NameError"
        # …and set only after _process returned, never before it.
        assert code.index("self._process(") < code.index("processed = True"), \
            f"{name}: processed set before _process ran"


def t_a_finished_batch_always_clears_its_running_flag() -> None:
    """`running` is a process global that gates every future batch — a stale True means the API
    answers "a batch is already running" forever, and only a container restart clears it.

    It used to be cleared by the same `update()` whose `results=[...]` comprehension was evaluated
    as an ARGUMENT, so one result missing `summary["fetched"]` raised before the flag was cleared
    and killed the thread silently. Observed twice on live crawls: no crawl traffic in the logs for
    minutes while the status still said running. The flag must be cleared first, alone, by code
    that cannot raise."""
    import os
    import tempfile
    import types

    os.environ.setdefault("CRAWLER_ISSUES_PATH", tempfile.mkdtemp() + "/i.jsonl")
    import crawler_api.app as A

    malformed = [{"job_id": "a", "summary": {}},   # summary present but empty
                 {"job_id": "b"},                  # no summary at all
                 None]                             # not even a dict
    orig = A._dispatch
    A._dispatch = lambda jobs, forward=False: malformed
    try:
        A._batch_status.update(running=True, total=1, done=0)
        A.crawl_batch(A.BatchRequest(jobs=[], forward_to_ingest=False, background=False))
    finally:
        A._dispatch = orig
    assert A._batch_status["running"] is False, \
        "stale running flag — every subsequent batch would be refused until a restart"
    assert len(A._batch_status.get("results") or []) == 3, "rows must still be built defensively"
    _ = types  # (kept: the shim above mirrors how the endpoint is invoked in production)


def t_restore_is_a_no_op_when_nothing_was_stored() -> None:
    """Asking to resume a job that never ran must start it normally, not fail."""
    import asyncio

    from crawler import async_engine as ae
    from crawler.models import Job

    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))

        async def run():
            e = ae.AsyncEngine.__new__(ae.AsyncEngine)
            e.frontier = asyncio.Queue()
            e.render_q = asyncio.Queue()
            e.history = h

            class _Ctx:
                job = Job(job_id="never-ran", job_type="news", seed_urls=["http://a/"],
                          resume=True)
                seen: set = set()
                budget_used = 0
            assert e._restore_frontier(_Ctx()) == 0
        asyncio.run(run())
        h.close()


def t_budget_capped_job_counts_as_finished_not_interrupted() -> None:
    """A job that spent its max_pages budget leaves URLs pending — but it is NOT resumable.

    Resume restores the spent budget, so such a job would start and stop again immediately. Listing
    it hands the operator a button that does nothing, and buries the crawls that really were cut off.
    Only an operator STOP or the engine wall-clock chop makes a job resumable."""
    from crawler.async_engine import _job_end_status
    from crawler.models import Job

    class _Ctx:
        def __init__(self, max_pages: int, used: int):
            self.job = Job(job_id="j", job_type="news", seed_urls=["http://a/"],
                           max_pages=max_pages)
            self.budget_used = used

    assert _job_end_status(_Ctx(50, 50), interrupted=True) == "completed", \
        "hit the cap → finished, even though STOP was pressed for the batch"
    assert _job_end_status(_Ctx(50, 12), interrupted=True) == "stopped", \
        "cut off well under the cap → genuinely resumable"
    assert _job_end_status(_Ctx(50, 12), interrupted=False) == "completed", \
        "ran its frontier dry → nothing left to resume"
    assert _job_end_status(_Ctx(0, 9_999), interrupted=True) == "stopped", \
        "max_pages<=0 is unlimited — no budget to reach, so a stop is always resumable"


def t_stop_reason_says_stopped_when_the_run_was_cut_short() -> None:
    """The run log's badge is driven by summary()["stop_reason"], and it used to be a two-way choice:
    "max_pages", else "completed" as a catch-all `else`. So a job the operator STOPPED reported
    success. MEASURED: gem.gov.in was cut off after ONE page of a 68-URL frontier and the Job
    generator table showed it with a green "completed" badge, indistinguishable from a site that had
    genuinely been crawled out — which is exactly the signal needed to notice the crawl went wrong.

    Same three outcomes as _job_end_status, for the same reason: reaching the cap IS finishing."""
    from crawler import keywords as kwmod
    from crawler.async_engine import JobCtx
    from crawler.models import Job
    from crawler.seed import load_seed

    seed = load_seed()

    def _reason(max_pages: int, used: int, interrupted: bool) -> str:
        ctx = JobCtx(Job(job_id="j", job_type="news", seed_urls=["http://a/"], max_pages=max_pages),
                     seed, kwmod.from_list([]), forward=False)
        ctx.budget_used = used
        ctx.interrupted = interrupted
        return ctx.summary()["stop_reason"]

    assert _reason(0, 12, True) == "stopped", "cut short → the badge must not claim completion"
    assert _reason(0, 12, False) == "completed", "frontier drained → genuinely finished"
    assert _reason(50, 50, True) == "max_pages", "spent its budget → finished at the cap"
    assert _reason(50, 12, True) == "stopped", "cut off well under the cap"
    # The default must never be silently optimistic: a ctx nobody stamped reads as completed only
    # because it really did run to exhaustion, and JobCtx sets interrupted=False at construction.
    ctx = JobCtx(Job(job_id="j", job_type="news", seed_urls=["http://a/"]),
                 seed, kwmod.from_list([]), forward=False)
    assert ctx.interrupted is False


def t_completed_jobs_are_hidden_from_the_resume_list() -> None:
    """Belt to the engine's braces: the engine drops a completed job's frontier, but rows written
    before that behaviour existed are still on disk. The query must not resurrect them."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        for jid, status in (("capped", "completed"), ("halted", "stopped"), ("died", "running")):
            h.frontier_add(jid, [(f"http://a/{jid}", 0, None, False)])
            h.job_state_save(jid, pages_done=1, status=status, job_json="{}")
        listed = {r["job_id"] for r in h.resumable_jobs()}
        assert listed == {"halted", "died"}, listed
        h.close()


def t_delete_all_forgets_every_saved_frontier() -> None:
    with tempfile.TemporaryDirectory() as d:
        h = _h(Path(d))
        for jid in ("a", "b"):
            h.frontier_add(jid, [(f"http://x/{jid}", 0, None, False)])
            h.job_state_save(jid, pages_done=0, status="stopped", job_json="{}")
        assert h.forget_resumable(["a"]) == {"a": 1}
        assert {r["job_id"] for r in h.resumable_jobs()} == {"b"}
        h.forget_resumable()                       # no ids = all of them
        assert h.resumable_jobs() == []
        assert h.job_state("b") is None, "the stored job definition must go with the frontier"
        h.close()


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} RESUME TESTS PASSED")


def test_resume_suite() -> None:
    main()


if __name__ == "__main__":
    main()
