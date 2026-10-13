"""Run-to-exhaustion crawling: no wall-clock chop, cooling hosts heartbeat the idle timer,
and batches dispatch in the background so an hours-long crawl doesn't block the HTTP request.

Regress any of these and long crawls silently stop short again:
- wall-clock default 0 must disable the asyncio.wait_for backstop (timeout=None),
- a host parked on a 429/503 cooldown must mark progress or the 300s idle timer kills a live crawl,
- POST /v1/crawl/batch {background:true} must return at once and enforce single-flight.
"""
import time

import asyncio
import pytest
from fastapi.testclient import TestClient

import crawler_api.app as app_module
from crawler_api.app import app
from crawler.async_engine import HostLimiter, _env_int


# ── 1. wall-clock off ────────────────────────────────────────────────────────
def test_wall_clock_off_means_no_backstop(monkeypatch):
    # The engine passes `timeout=(wall or None)` to wait_for; 0/unset must become None (no chop).
    monkeypatch.delenv("CRAWLER_ENGINE_WALL_CLOCK_S", raising=False)
    assert (_env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 0) or None) is None
    monkeypatch.setenv("CRAWLER_ENGINE_WALL_CLOCK_S", "0")
    assert (_env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 0) or None) is None
    # A set backstop is still honoured (re-arm for a known-runaway site).
    monkeypatch.setenv("CRAWLER_ENGINE_WALL_CLOCK_S", "120")
    assert (_env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 0) or None) == 120


# ── 2. slot cooldown heartbeats the idle timer ───────────────────────────────
def test_slot_heartbeat_fires_during_cooldown(monkeypatch):
    monkeypatch.setenv("CRAWLER_SLOT_HEARTBEAT_S", "0.1")
    calls: list[int] = []

    async def go():
        h = HostLimiter(4, 0.0)            # min_delay 0, no robots
        h._progress = lambda: calls.append(1)
        await h._ensure("example.com")     # create the host entry (slot() re-ensures = no-op)
        loop = asyncio.get_running_loop()
        h._next_ok["example.com"] = loop.time() + 0.6   # 0.6s cooldown ahead
        async with h.slot("https://example.com/x"):
            pass

    asyncio.run(go())
    assert len(calls) >= 1                 # a cooling host counted as progress, not a wedge


def test_slot_no_heartbeat_when_not_cooling(monkeypatch):
    monkeypatch.setenv("CRAWLER_SLOT_HEARTBEAT_S", "0.1")
    calls: list[int] = []

    async def go():
        h = HostLimiter(4, 0.0)
        h._progress = lambda: calls.append(1)
        async with h.slot("https://example.com/x"):   # _next_ok=0 -> no wait -> no heartbeat
            pass

    asyncio.run(go())
    assert calls == []


# ── 3. background dispatch + single-flight ───────────────────────────────────
_BG_JOB = {"job_id": "bg_test", "job_type": "profile", "seed_urls": ["https://example.com/"],
           "keywords": [], "max_pages": 1, "max_depth": 0}


def _wait_idle(client, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not client.get("/v1/batch/status").json().get("running"):
            return
        time.sleep(0.05)
    raise AssertionError("batch never went idle")


def test_background_batch_returns_immediately(monkeypatch):
    def slow_dispatch(jobs, *, forward):
        time.sleep(2.0)
        return [{"job_id": "bg_test",
                 "summary": {"fetched": 0, "kept": 0, "sent": 0, "accepted": 0}}]

    monkeypatch.setattr(app_module, "_dispatch", slow_dispatch)
    client = TestClient(app)
    try:
        t0 = time.monotonic()
        r = client.post("/v1/crawl/batch", json={"jobs": [_BG_JOB], "background": True})
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert r.json().get("started") is True
        assert elapsed < 1.0                       # returned without waiting on the 2s crawl
        assert client.get("/v1/batch/status").json()["running"] is True

        # single-flight: a second background batch is refused while one runs
        r2 = client.post("/v1/crawl/batch", json={"jobs": [_BG_JOB], "background": True})
        assert r2.json().get("started") is False
    finally:
        _wait_idle(client)
    assert client.get("/v1/batch/status").json()["running"] is False
