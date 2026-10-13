"""C2 is EXCLUSIVE and STRUCTURAL: C1/C3 have no path to the archive, and an archive job has no
path to a browser.

This is the property the whole refactor exists to create. A flag can be left on by accident; a
missing code path cannot.
"""
import inspect

import pytest

from crawler import async_engine
from crawler.models import Job


def _job(**kw):
    base = dict(job_id="a", job_type="profile", seed_urls=["https://sipri.org/"])
    return Job(**{**base, **kw})


# --- the live engine can no longer reach the archive ----------------------
def test_engine_has_no_archive_fallback_left():
    for gone in ("_try_wayback_fallback", "_wayback_snapshot", "_WB_TOOLBAR"):
        assert not hasattr(async_engine, gone) and not hasattr(async_engine.AsyncEngine, gone), \
            f"{gone} survived — C2 is still reachable from the live ladder"


def test_engine_takes_no_wayback_flag():
    # The flag is gone end-to-end: exclusivity is structural, so there is nothing left to leave on.
    for fn in (async_engine.AsyncEngine.__init__, async_engine.run_batch_async,
               async_engine.run_batch_multiprocess):
        assert "wayback" not in inspect.signature(fn).parameters, f"{fn.__qualname__} still takes wayback"


def _deadhost(ctx, camo_result=True):
    """Drive _recover_deadhost with a fake C3. Behavioural, not a source grep — a grep fires on the
    docstring that EXPLAINS the deletion."""
    import asyncio
    from types import SimpleNamespace
    eng = async_engine.AsyncEngine.__new__(async_engine.AsyncEngine)
    calls = []

    async def fake_camo(item, host, solve_captchas=False):
        calls.append(host)
        return camo_result

    eng._try_camofox_fallback = fake_camo
    got = asyncio.run(eng._recover_deadhost(SimpleNamespace(ctx=ctx), "x.com"))
    return got, calls


def _ctx(max_pages=10, used=0):
    from crawler.async_engine import JobCtx
    c = JobCtx.__new__(JobCtx)
    c.job, c.budget_used, c.done = _job(max_pages=max_pages), used, False
    c.recovered, c.errors_by_reason = 0, {}
    return c


def test_dead_env_var_no_longer_gates_recovery(monkeypatch):
    # "0" used to mean "no recovery at all". The var is deleted, so it must have NO effect —
    # a stale value in someone's .env cannot silently disable C3.
    monkeypatch.setenv("CRAWLER_DEADHOST_RECOVER", "0")
    got, calls = _deadhost(_ctx())
    assert got is True and calls == ["x.com"]


def test_deadhost_recovers_via_c3():
    got, calls = _deadhost(_ctx())
    assert got is True and calls == ["x.com"]


def test_deadhost_does_not_refund_budget_it_never_took():
    # THE BUG: JobCtx.reserve() returns False BEFORE `budget_used += 1`, so the old unreserve() on
    # that branch handed back budget never taken — inflating the page budget of every job that hit
    # a cooling host.
    ctx = _ctx(max_pages=1, used=1)               # budget already exhausted -> reserve() fails
    got, calls = _deadhost(ctx)
    assert got is False and calls == []           # never even tried C3
    assert ctx.budget_used == 1, "refunded budget that was never reserved"


def test_deadhost_refunds_budget_when_nothing_recovered():
    ctx = _ctx(max_pages=10, used=0)
    got, _ = _deadhost(ctx, camo_result=False)    # C3 found nothing -> give the budget back
    assert got is False and ctx.budget_used == 0


# --- the archive tier costs no browser ------------------------------------
def test_c2_module_never_imports_playwright_or_the_pool():
    import crawler.c2 as c2
    src = inspect.getsource(c2)
    for banned in ("playwright", "chromium", "new_page", "AsyncEngine("):
        assert banned not in src, f"c2.py references {banned} — the archive tier must cost no browser"


def _spy(monkeypatch):
    """Record which tier each job was routed to; return the shape _dispatch expects."""
    from crawler_api import app
    import crawler.c2 as c2
    seen = {}

    def make(key):
        def run(jobs, **k):
            seen[key] = [j.job_id for j in jobs]
            return [{"job_id": j.job_id, "summary": {}} for j in jobs]
        return run

    monkeypatch.setattr(app, "run_batch_multiprocess", make("live"))
    monkeypatch.setattr(c2, "run_c2_batch", make("c2"))
    return app, seen


def test_c2_routes_away_from_the_live_pool(monkeypatch):
    app, seen = _spy(monkeypatch)
    app._dispatch([_job(job_id="arch", archive_date="2024-07-01", max_pages=5)],
                  forward=False)
    assert seen.get("c2") == ["arch"]
    assert "live" not in seen, "an archive job reached the browser pool"


def test_live_job_still_routes_to_the_pool(monkeypatch):
    app, seen = _spy(monkeypatch)
    app._dispatch([_job(job_id="live")], forward=False)
    assert seen.get("live") == ["live"]
    assert "c2" not in seen


def test_mixed_batch_splits_by_tier_and_keeps_request_order(monkeypatch):
    # Splitting by tier reorders results; callers line them up against the jobs they sent.
    app, seen = _spy(monkeypatch)
    jobs = [_job(job_id="l1"), _job(job_id="a1", archive_date="2024-07-01", max_pages=5),
            _job(job_id="l2"), _job(job_id="a2", archive_date="2024-07-01", max_pages=5)]
    out = app._dispatch(jobs, forward=False)
    assert [r["job_id"] for r in out] == ["l1", "a1", "l2", "a2"]
    assert seen == {"c2": ["a1", "a2"], "live": ["l1", "l2"]}
