"""Two-lane scheduling: cheap httpx work must never queue behind expensive render work.

WHY THIS EXISTS
---------------
Measured on a live batch before the split: 0.086 pages/sec with the CPU at 0%, 3 of 64 render
permits in use and 80 tabs idle — the pool was not saturated, it was starved. Two separate causes,
both fixed here and both pinned below:

  1. One FIFO frontier for both kinds of work, so a run of render items occupied every worker at
     once and httpx throughput went to zero.
  2. A host-queue deflection that parked items OUT of the frontier for 25s via call_later. It
     exists to stop a worker parking a TAB, but in tab-pool mode an httpx item holds no tab, so it
     was pure loss: 2,662 deflections against 243 fetched pages.

The tests drive the real methods on a bare AsyncEngine instance (no browser, no network) — the
scheduling logic is plain asyncio and does not need either.

Runnable directly:  python tests/test_lanes.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402


def _engine(n_workers: int = 4, lane_cap: int = 2) -> ae.AsyncEngine:
    """A bare engine with only the fields the scheduler touches. __init__ builds browsers, so it is
    deliberately bypassed — this is a unit test of the queueing, not of the pool."""
    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    e.frontier = asyncio.Queue()
    e.render_q = asyncio.Queue()
    e._render_lane_used = 0
    # Per-host clocks for the deflect budget. __init__ sets both; this fixture bypasses __init__,
    # so they have to be declared here too. `host_last_ok` is the proof-of-life half — it is what
    # tells a host that is merely SLOW (slots permanently full, but delivering) from a wedged one,
    # and without it here the worker dies with AttributeError and the give-up is misreported as
    # render_failed rather than host_busy.
    e.host_busy_since = {}
    e.host_last_ok = {}
    e.host_stalled = {}
    e.dead_hosts = set()
    e.host_cooldown = {}
    e._render_lane_cap = lane_cap
    e.n_workers = n_workers
    e._pending_retries = 0
    e._inflight_items = 0
    e._httpx_since_render = 0
    return e


def _item(url: str, needs_render: bool = False) -> ae.WorkItem:
    return ae.WorkItem(url=url, depth=1, ctx=None, needs_render=needs_render)


def t_httpx_lane_is_drained_first() -> None:
    """Render work is LAST. With both queues loaded, every httpx item must come out before the
    first render item — otherwise a batch containing a few render-heavy sites drags the cheap
    work behind it, which is the original complaint.

    Bounded by CRAWLER_RENDER_LANE_EVERY (see the starvation test below); with fewer httpx items
    than that bound the preference is absolute, which is what this asserts."""
    async def run():
        e = _engine()
        for i in range(3):
            e.render_q.put_nowait(_item(f"http://r/{i}", True))
        for i in range(3):
            e.frontier.put_nowait(_item(f"http://h/{i}"))
        order = []
        for _ in range(6):
            item, lane = await e._next_item()
            order.append(item.url)
            if lane:
                e._release_lane()
        assert order[:3] == ["http://h/0", "http://h/1", "http://h/2"], order
        assert all(u.startswith("http://r/") for u in order[3:]), order
    asyncio.run(run())


def t_render_lane_is_not_starved_by_an_endless_httpx_queue() -> None:
    """The 84-minute bug. Preferring the cheap lane was ABSOLUTE: `frontier.get_nowait()` ran first
    on every pull, so a render item was only ever reached once the frontier hit zero. Put one big
    httpx site in the batch and that never happens.

    MEASURED on a 5-site wave: tender18.com (11k pages, httpx) kept the frontier non-empty for the
    whole run while gem.gov.in and eprocure.gov.in — `.gov`, therefore careful hosts, therefore
    render-only — finished with ONE page each and 67 and 70 URLs still sitting in the render lane.
    Crawled on its own gem.gov.in does 7 pages in 50s, so nothing was slow; it never got a turn.

    Here the frontier is refilled after every pull, exactly like a site that keeps discovering
    links. Under the old code this loop returns 50 httpx items and not one render item."""
    import os

    async def run():
        os.environ["CRAWLER_RENDER_LANE_EVERY"] = "4"
        e = _engine(n_workers=4, lane_cap=2)
        for i in range(3):
            e.render_q.put_nowait(_item(f"http://gov/{i}", True))
        e.frontier.put_nowait(_item("http://big/0"))
        served_render = []
        for n in range(50):
            item, lane = await e._next_item()
            if lane:
                served_render.append(item.url)
                e._release_lane()
            else:
                e.frontier.put_nowait(_item(f"http://big/{n + 1}"))   # the site never runs dry
        assert served_render == ["http://gov/0", "http://gov/1", "http://gov/2"], served_render
        assert e.render_q.qsize() == 0, "render items were stranded behind the httpx lane"
        # Still a strong preference, not round-robin: the cheap lane keeps the large majority.
        assert len(served_render) < 50 // 4, served_render
    asyncio.run(run())


def t_render_lane_is_capped_so_the_httpx_reserve_is_never_empty() -> None:
    """The cap is the whole guarantee. With only render work queued, at most `_render_lane_cap`
    workers may be inside it — the rest stay free for httpx that may arrive at any moment."""
    async def run():
        e = _engine(n_workers=4, lane_cap=2)
        for i in range(10):
            e.render_q.put_nowait(_item(f"http://r/{i}", True))
        held = []
        for _ in range(2):
            held.append(await e._next_item())
        assert e._render_lane_used == 2
        # A third worker must NOT be able to take render work — it waits for httpx instead.
        third = asyncio.ensure_future(e._next_item())
        await asyncio.sleep(0.05)
        assert not third.done(), "render lane cap was exceeded — the httpx reserve is gone"
        # …and the moment httpx work appears, that reserved worker picks it up immediately.
        e.frontier.put_nowait(_item("http://h/urgent"))
        item, lane = await asyncio.wait_for(third, timeout=1)
        assert item.url == "http://h/urgent" and lane is False
        for _, l in held:
            if l:
                e._release_lane()
    asyncio.run(run())


def t_releasing_the_lane_lets_the_next_render_in() -> None:
    async def run():
        e = _engine(n_workers=4, lane_cap=1)
        e.render_q.put_nowait(_item("http://r/0", True))
        e.render_q.put_nowait(_item("http://r/1", True))
        i0, l0 = await e._next_item()
        assert l0 is True and e._render_lane_used == 1
        nxt = asyncio.ensure_future(e._next_item())
        await asyncio.sleep(0.05)
        assert not nxt.done(), "lane cap not enforced"
        e._release_lane()
        # The waiter is parked on frontier.get() only, so it needs a nudge to re-evaluate the lane.
        # Feed it httpx work — the point being that it is AVAILABLE, which is the reserve working.
        e.frontier.put_nowait(_item("http://h/0"))
        item, _ = await asyncio.wait_for(nxt, timeout=1)
        assert item.url == "http://h/0"
        # With the lane free again, the queued render item is served next.
        i1, l1 = await e._next_item()
        assert (i1.url, l1) == ("http://r/1", True)
    asyncio.run(run())


def t_next_item_never_loses_an_item() -> None:
    """The two-waiter race can have BOTH queues produce at once. If the loser were dropped the URL
    would vanish silently and the crawl would just be smaller — the worst kind of bug, because
    nothing reports it. Every item put in must come back out."""
    async def run():
        e = _engine(n_workers=8, lane_cap=4)
        waiters = [asyncio.ensure_future(e._next_item()) for _ in range(6)]
        await asyncio.sleep(0.05)                     # all parked on empty queues
        for i in range(3):
            e.frontier.put_nowait(_item(f"http://h/{i}"))
            e.render_q.put_nowait(_item(f"http://r/{i}", True))
        got = []
        for w in asyncio.as_completed(waiters, timeout=3):
            item, lane = await w
            got.append(item.url)
            if lane:
                e._release_lane()
            if len(got) == 6:
                break
        assert sorted(got) == sorted([f"http://h/{i}" for i in range(3)]
                                     + [f"http://r/{i}" for i in range(3)]), got
        assert e.frontier.qsize() == 0 and e.render_q.qsize() == 0
    asyncio.run(run())


def t_only_render_items_are_deflected_on_host_queue_depth() -> None:
    """The deflection exists to stop a worker parking a TAB. In tab-pool mode an httpx item holds
    no tab, so deflecting it cost a 25s park for nothing — measured at 11 deflections per fetched
    page on a live batch, with the CPU at 0%. _will_render_first is what draws that line."""
    class _Host:
        force_careful: set = set()
        def override_for(self, h): return None

    class _Job:
        render_js = False
        capture = ["html", "text"]

    e = _engine()
    e.host = _Host()
    e.host_render_always = set()

    class _Ctx:
        job = _Job()

    it = _item("https://example.com/a")
    it.ctx = _Ctx()
    assert e._will_render_first(it) is False, "a plain auto-mode page must take the httpx lane"

    e.host_render_always = {"example.com"}
    assert e._will_render_first(it) is True, "a render-always host really does hold a tab first"

    e.host_render_always = set()
    _Job.render_js = True
    assert e._will_render_first(it) is True, "render_js=true goes to the browser first"
    _Job.render_js = False

    # A .gov host is crawled carefully (browser first), so it does hold a tab.
    gov = _item("https://army.mil/x")
    gov.ctx = _Ctx()
    assert e._will_render_first(gov) is True


def t_next_item_counts_inflight_at_the_pull_not_after() -> None:
    """The race that made a solved zoopla stop after page 1: a render item was removed from
    render_q while `await asyncio.wait` resolved, but inflight was counted only in the worker AFTER
    the await. In that window the drain sampled render_q.qsize()==0 AND inflight==0, declared the
    batch done, and cancelled the workers with items in limbo — gone, no records.

    _next_item must increment _inflight_items the instant the item leaves the queue, so there is no
    yield point between 'left the queue' and 'counted in-flight'."""
    async def run():
        e = _engine(n_workers=4, lane_cap=2)
        # get_nowait path (frontier)
        e.frontier.put_nowait(_item("http://h/1"))
        assert e._inflight_items == 0
        item, lane = await e._next_item()
        assert e._inflight_items == 1, "frontier pull did not count in-flight"
        # get_nowait path (render lane)
        e.render_q.put_nowait(_item("http://r/1", True))
        await e._next_item()
        assert e._inflight_items == 2, "render pull did not count in-flight"
        e._release_lane()
        # the await-wait path: block, then feed render_q — the item must be counted before the
        # coroutine returns, i.e. the drain could never see qsize==0 with inflight uncounted.
        fut = asyncio.ensure_future(e._next_item())
        await asyncio.sleep(0.02)
        e.render_q.put_nowait(_item("http://r/2", True))
        await asyncio.wait_for(fut, timeout=1)
        assert e._inflight_items == 3, "await-path pull did not count in-flight"
    asyncio.run(run())


def t_queue_bookkeeping_balances_when_both_lanes_fire_at_once() -> None:
    """_next_item waits on both queues and puts the loser back. If that put/task_done pairing were
    off by one, asyncio.Queue.task_done() eventually raises ValueError('task_done() called too many
    times') and kills a worker mid-crawl — silently reducing the pool, which looks like "the sites
    got slower". Forces the both-ready race and asserts the counters return to zero."""
    async def run():
        e = _engine(n_workers=4, lane_cap=2)
        waiters = [asyncio.ensure_future(e._next_item()) for _ in range(2)]
        await asyncio.sleep(0.02)                    # both parked on empty queues
        e.frontier.put_nowait(_item("http://h/1"))
        e.render_q.put_nowait(_item("http://r/1", True))
        got = []
        for w in waiters:
            item, lane = await asyncio.wait_for(w, timeout=2)
            got.append(item.url)
            q = e.render_q if lane else e.frontier
            if lane:
                e._release_lane()
            q.task_done()                            # exactly what the worker's finally does
        assert sorted(got) == ["http://h/1", "http://r/1"], got
        assert e.frontier._unfinished_tasks == 0, e.frontier._unfinished_tasks
        assert e.render_q._unfinished_tasks == 0, e.render_q._unfinished_tasks
    asyncio.run(run())


class _FakeCtx:
    """Minimal JobCtx surface used by the give-up path."""
    def __init__(self):
        self.reasons = []
        self.unreserved = 0
        self.crawl_run_id = "run1"

        class _J:
            job_id = "job1"
        self.job = _J()

    def bump_reason(self, r):
        self.reasons.append(r)

    def unreserve(self):
        self.unreserved += 1


def _giveup_engine(tmp):
    from crawler import issues
    issues._LOG = issues.IssueLog(tmp / "iss.jsonl")     # isolate from the real log
    e = _engine()
    e.history = None
    return e, issues


def t_endless_render_retries_then_logs_with_a_specific_cause() -> None:
    """A page whose render never completes must be retried a bounded number of times and then
    recorded — not retried forever, and not silently dropped into a warning. The operator asked for
    "retried 3-4 times, then keep a list of what is still unresolved", and the cause has to be
    specific because the remedy differs completely between a wall and a slow render."""
    import os
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as d:
            e, issues = _giveup_engine(Path(d))
            os.environ["CRAWLER_RENDER_MAX_ATTEMPTS"] = "4"
            os.environ["CRAWLER_RENDER_RETRY_S"] = "0"
            it = _item("http://slow/1", True)
            it.ctx = _FakeCtx()
            it.last_render_error = "item watchdog (240s) expired"

            for n in (1, 2, 3):
                e._retry_or_log_render(it)
                assert it.render_attempts == n, it.render_attempts
                assert len(issues.log()) == 0, "must not log while attempts remain"
            await asyncio.sleep(0.05)                  # let the call_later requeues land
            assert e.render_q.qsize() == 3, e.render_q.qsize()

            e._retry_or_log_render(it)                 # 4th = the cap
            assert it.render_attempts == 4
            rows = issues.log().recent()
            assert len(rows) == 1, rows
            r = rows[0]
            assert r["url"] == "http://slow/1"
            assert r["cause"] == "render_timeout", r["cause"]
            assert r["attempts"] == 4, r
            assert "240s" in r["evidence"], r["evidence"]
            assert it.ctx.unreserved == 1, "the page budget must be handed back on give-up"
            assert "render_timeout" in it.ctx.reasons
    asyncio.run(run())


def t_host_deflection_gives_up_after_a_cap_instead_of_livelocking() -> None:
    """The bug behind 0.11 pages/sec. When the frontier drained to a host whose render slots never
    freed (a tarpit / hard block), the host-busy guard re-queued the page every 3s FOREVER: the
    deflect runs BEFORE _process, so render_attempts never grew and the give-up path never fired.
    Measured live: ~300 deflections per fetched page, frontier empty, the batch stuck at done 0.

    After the budget the page must leave the lane exactly ONCE, recorded with a real cause, so the
    batch can finish. Drives the real _pool_worker loop against a host that never drains — without
    the budget this test hangs (the item circulates forever); with it, one issue row and an empty
    queue. Budget 0 makes it deterministic: `waited < 0` is false on the first bounce (waited==0),
    so it gives up immediately with no dependence on the wall clock advancing."""
    import os
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as d:
            e, issues = _giveup_engine(Path(d))
            os.environ["CRAWLER_HOST_DEFLECT_BUDGET_S"] = "0"   # give up on the first bounce
            os.environ["CRAWLER_HOST_BUSY_REQUEUE_S"] = "0"     # (unused at budget 0; belt-and-braces)
            os.environ["CRAWLER_HOST_QUEUE_MAX"] = "2"

            class _Host:                        # every slot always busy → the guard always deflects
                def queue_depth(self, h):
                    return 99
            e.host = _Host()

            async def _boom(*a, **k):           # a deflected item must never reach processing
                raise AssertionError("deflected item reached _process")
            e._process = _boom

            saved_stop = ae.stop_requested
            ae.stop_requested = lambda: False
            try:
                it = _item("https://tarpit.example/1", needs_render=True)
                it.ctx = _FakeCtx()
                worker = asyncio.ensure_future(e._pool_worker())
                e.render_q.put_nowait(it)
                for _ in range(400):            # give-up lands in ~6 loop ticks; bounded so a
                    if len(issues.log()) >= 1:  # regression (livelock) fails fast instead of hanging
                        break
                    await asyncio.sleep(0.005)
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            finally:
                ae.stop_requested = saved_stop

            rows = issues.log().recent()
            assert len(rows) == 1, f"expected exactly one give-up row, got {rows}"
            assert rows[0]["cause"] == "host_busy", rows[0]
            assert rows[0]["url"] == "https://tarpit.example/1"
            # budget 0 → give up on the first bounce; the point is it is FINITE, not the count
            assert it.host_deflects == 1, it.host_deflects
            assert it.ctx.unreserved == 1, "the page budget must be handed back on give-up"
            await asyncio.sleep(0.02)           # let any stray requeue land, then prove there is none
            assert e.render_q.qsize() == 0, "item must not remain re-queued after give-up"
    asyncio.run(run())


def t_giveup_cause_names_the_real_obstacle_not_the_render() -> None:
    """When the crawl actually saw a body, the recorded cause must name what was IN it. "render
    failed" is useless to act on; "cloudflare_interactive, cannot self-clear" says stop spending
    renders, and "auth_wall" says this page needs credentials we do not have."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        e, _ = _giveup_engine(Path(d))
        cf = ("<html><head><title>Just a moment...</title></head><body><script>"
              "window._cf_chl_opt={cvId:'3',cType:'interactive',cRay:'a20'};</script>"
              "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
              "</body></html>")
        it = _item("http://cf/1", True)
        it.ctx = _FakeCtx()
        it.last_body = cf
        it.last_render_error = "timeout"
        cause, action, evidence = e._classify_unresolved(it)
        assert cause == "antibot", cause
        assert "interactive" in evidence and "cannot self-clear" in evidence, evidence

        # A login wall behind an exhausted render is an auth wall, not a render failure.
        it2 = _item("http://login/1", True)
        it2.ctx = _FakeCtx()
        it2.last_body = ('<html><body><form><input type="password" name="p">'
                         '<button>Sign in</button></form></body></html>')
        it2.last_render_error = "timeout"
        cause2, _, ev2 = e._classify_unresolved(it2)
        assert cause2 == "auth_wall", (cause2, ev2)

        # With no body at all, fall back to the render error text — still more useful than a
        # generic "render_crash", because it carries the Playwright/net:: detail.
        it3 = _item("http://x/1", True)
        it3.ctx = _FakeCtx()
        it3.last_render_error = "net::ERR_CONNECTION_RESET at https://x/1"
        cause3, _, ev3 = e._classify_unresolved(it3)
        assert cause3 == "render_failed" and "ERR_CONNECTION_RESET" in ev3, (cause3, ev3)


def t_a_later_success_clears_the_issue() -> None:
    """Without resolve(), the list only ever grows and a page that started working again would sit
    there claiming to be broken — which is how an issues list stops being read."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _, issues = _giveup_engine(Path(d))
        issues.log().record("http://a/1", "render_timeout", attempts=4)
        assert len(issues.log()) == 1
        assert issues.log().resolve("http://a/1") is True
        assert len(issues.log()) == 0


def t_shutdown_is_bounded_when_a_browser_close_hangs() -> None:
    """A wedged Chromium must not hang the batch. MEASURED: after an operator STOP on a 6.4k-page
    crawl every queue was empty, in-flight was 0 and the CPU idle, yet the dashboard sat at
    "running · (stopping…)" done 0/1 until the container was restarted — and while that flag is set
    the API refuses every later batch with "a batch is already running". The cause was an unbounded
    `await tab.close()` in shutdown: cleanup that cannot finish has to be abandoned."""
    import os

    async def run():
        e = ae.AsyncEngine.__new__(ae.AsyncEngine)
        e._asset_pool = e._io_pool = None
        e._pw = None

        class _Hangs:                     # close() never returns, like a wedged Chromium
            async def close(self):
                await asyncio.Event().wait()

        e.tabs = [_Hangs(), _Hangs()]
        e.browsers = [(_Hangs(), _Hangs())]
        os.environ["CRAWLER_CLOSE_TIMEOUT_S"] = "0.05"
        # The assertion is simply that this RETURNS. Under the old code it never did.
        await asyncio.wait_for(e.shutdown(), timeout=5)

    asyncio.run(run())


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} LANE TESTS PASSED")


def test_lanes_suite() -> None:
    main()


if __name__ == "__main__":
    main()
