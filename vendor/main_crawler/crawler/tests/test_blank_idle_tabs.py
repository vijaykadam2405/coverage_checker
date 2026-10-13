"""A worker must park its tab on about:blank between items.

A tab left on the last rendered page keeps compositing that page's animations forever
(~5.8% of a core each, measured; about:blank is ~0.1%). At W*T tabs that idle burn reads
as "no CPU headroom" to the adaptive controller, which then throttles renders to the
floor — the idle tabs starve the real ones. Regress this and the pool quietly drops back
to ~4 concurrent renders out of 192 tabs.
"""
import asyncio

import pytest

from crawler.async_engine import AsyncEngine, _blank_between_items


class FakeTab:
    def __init__(self):
        self.gotos = []

    async def goto(self, url):
        self.gotos.append(url)



class FakeCtx:
    """The slice of CrawlContext that _worker touches on the paths this file exercises.

    A bare object() used to be enough. _worker now completes the frontier entry and, on the error
    path, records a reason against the context — so an attribute-less item raised inside the worker
    task and the suite hung instead of failing. Kept minimal on purpose: this file is about tab
    blanking, not context behaviour.
    """
    crawl_run_id = "test-run"
    job = type("Job", (), {"job_id": "test-job"})()

    def unreserve(self): pass
    def bump_reason(self, *_a, **_kw): pass


class FakeHost:
    """The per-host limiter, reduced to what _worker asks of it: is this host's permit queue
    already deep enough that we should requeue instead of parking a tab."""
    force_careful: set = set()

    def queue_depth(self, _host): return 0


class FakeItem:
    def __init__(self, url="https://example.com/page"):
        self.url = url
        self.ctx = FakeCtx()
        self.depth = 0
        self.parent_url = None


def _worker_once(engine, tab, item, timeout: float = 20.0):
    """Run _worker until it has consumed exactly one item, then cancel the loop.

    BOUNDED. `frontier.join()` waits forever if the worker never calls task_done() — and it will
    not if _worker raises before finishing the item (a missing attribute on this hand-built engine
    is enough). That is a silent, permanent hang: the whole test session stops here and reports
    nothing, which is strictly worse than a failure. Time it out, and re-raise whatever the worker
    task actually died of so the message names the cause.
    """
    async def go():
        engine.frontier = asyncio.Queue()
        engine.frontier.put_nowait(item)
        t = asyncio.create_task(engine._worker(tab))
        try:
            await asyncio.wait_for(engine.frontier.join(), timeout)
        except asyncio.TimeoutError:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as exc:                   # noqa: BLE001 — this is the real cause
                raise AssertionError(
                    f"_worker died before completing the item: {type(exc).__name__}: {exc}") from exc
            raise AssertionError(
                f"_worker never finished the item within {timeout}s and did not raise — "
                "the item was requeued or the worker is parked")
        t.cancel()
    asyncio.run(go())


def _engine(monkeypatch, process_impl):
    e = AsyncEngine.__new__(AsyncEngine)   # bypass __init__ (browsers/pool not needed)
    e._inflight_items = 0
    e.frontier = None
    # Whatever __init__ sets that _worker reads. Bypassing __init__ means every new engine field
    # is a fresh AttributeError in here — which used to surface as a permanent hang rather than a
    # failure, because the exception killed the worker before it could mark the item done.
    e._httpx_since_render = 0              # two-lane fairness counter (async_engine.py:1516)
    e.render_q = asyncio.Queue()           # the render lane; empty means "cheap lane only"
    e._render_lane_used = 0
    e._render_lane_cap = 0                 # 0 = never take the anti-starvation turn in this test
    e.host = FakeHost()
    e._frontier_complete = lambda *_a, **_kw: None   # durable-frontier bookkeeping, not under test
    monkeypatch.setattr(e, "_process", process_impl, raising=False)
    return e


def test_tab_is_blanked_after_each_item(monkeypatch):
    tab = FakeTab()

    async def fake_process(item, t):
        await t.goto("https://example.com/page")
        return t

    _worker_once(_engine(monkeypatch, fake_process), tab, FakeItem())
    # The render happened, and the tab did not stay parked on it.
    assert tab.gotos == ["https://example.com/page", "about:blank"]


def test_tab_is_blanked_even_when_process_raises(monkeypatch):
    # A failed page must not leave a loaded, animating tab behind — that's the exact
    # state that burns a core while the worker sits blocked on frontier.get().
    tab = FakeTab()

    async def boom(item, t):
        await t.goto("https://example.com/bad")
        raise RuntimeError("render exploded")

    _worker_once(_engine(monkeypatch, boom), tab, FakeItem())
    assert tab.gotos[-1] == "about:blank"


def test_blank_failure_never_kills_the_worker(monkeypatch):
    # A dead/closing tab makes goto() raise; the worker must still task_done() the item
    # (else frontier.join() hangs the batch forever) and let _recycle fix it next item.
    class DeadTab(FakeTab):
        async def goto(self, url):
            raise RuntimeError("target closed")

    async def fake_process(item, t):
        return t

    _worker_once(_engine(monkeypatch, fake_process), DeadTab(), FakeItem())   # must not hang/raise


def test_env_escape_hatch(monkeypatch):
    assert _blank_between_items()                                  # default ON
    monkeypatch.setenv("CRAWLER_BLANK_IDLE_TABS", "0")
    assert not _blank_between_items()
    monkeypatch.setenv("CRAWLER_BLANK_IDLE_TABS", "1")
    assert _blank_between_items()


def test_blanked_tab_is_the_one_process_returned(monkeypatch):
    # _process hands back a NEW tab after _recycle. Blanking the stale one would leave the
    # live tab loaded (the leak this fix exists to stop) and poke a closed page.
    old, new = FakeTab(), FakeTab()

    async def recycling_process(item, t):
        return new

    _worker_once(_engine(monkeypatch, recycling_process), old, FakeItem())
    assert new.gotos == ["about:blank"]
    assert old.gotos == []
