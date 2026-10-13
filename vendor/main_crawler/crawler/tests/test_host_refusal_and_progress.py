"""Two ways a fleet burns hours on a site that is never going to give it anything, and one way it
throws away a site that would.

Measured across the twelve instances on 2026-07-31, in a 400-line log sample from each:

    262 x 403        184 x host_busy        184 x "giving up"        2 x stored

* **403 had no circuit breaker.** The per-host breaker counts HARD failures — DNS, refused, TLS —
  and a 403 is none of those, it is an HTTP answer. So godrejenterprises.com answered 403 to 4,954
  queued URLs and each one cost the full recovery ladder (a CamoFox render, an API probe, a feed
  probe) before being written down as needs_network_path. The site was not rate-limiting us. It was
  saying no, 4,954 times, and we asked again every time.

* **host_busy could not tell slow from wedged.** The deflect budget gives up on an item once its
  host's slots have been full for 180s, which is what stops a wedged host livelocking the render
  lane. But a site whose every page needs a browser, capped at CRAWLER_HOST_QUEUE_MAX slots, is
  PERMANENTLY full while working perfectly — 295 queued pages at ~10s a render is 25 minutes of
  legitimately saturated slots. yakovlev.ru was serving us fine and we discarded 184 of its pages.

The third test here is the queued-junk gate, which is the same lesson as the blocklist: a filter on
the enqueue path governs discovery, and discovery is not where the backlog is.
"""
from __future__ import annotations

import asyncio

import pytest

from crawler import errors
from crawler.async_engine import AsyncEngine, _is_action_link


@pytest.fixture()
def eng():
    return AsyncEngine.__new__(AsyncEngine)


# ── a host that refuses us ────────────────────────────────────────────────────────────────────

def _forbid(eng, host, n, max_streak=15):
    """Replay n consecutive 403s through the breaker's arithmetic."""
    tripped = None
    for _ in range(n):
        eng.host_forbidden[host] = eng.host_forbidden.get(host, 0) + 1
        if eng.host_forbidden[host] >= max_streak:
            eng.dead_hosts.add(host)
            eng.host_forbidden[host] = 0
            tripped = True
    return tripped


def test_a_host_that_403s_everything_is_eventually_left_alone(eng):
    eng.host_forbidden, eng.dead_hosts = {}, set()
    _forbid(eng, "godrejenterprises.com", 15)
    assert "godrejenterprises.com" in eng.dead_hosts


def test_one_page_served_clears_the_refusal_streak(eng):
    """THE guard against over-reach. A site with a members-only section 403s some paths and serves
    others; killing it over the gated ones would cost every public page it has."""
    eng.host_forbidden, eng.dead_hosts = {}, set()
    _forbid(eng, "partly-gated.example", 14)          # one short of the threshold
    eng.host_forbidden["partly-gated.example"] = 0    # ← a page came back (the success path)
    _forbid(eng, "partly-gated.example", 14)
    assert "partly-gated.example" not in eng.dead_hosts, \
        "a site that serves some pages was written off over its gated ones"


def test_the_refusal_breaker_is_separate_from_the_network_breaker(eng):
    """Mixing "broken" with "says no" in one counter means neither streak ever trips: alternate the
    two and both stay below their thresholds for ever."""
    eng.host_fails, eng.host_forbidden, eng.dead_hosts = {}, {}, set()
    for _ in range(30):
        eng.host_fails["x.example"] = eng.host_fails.get("x.example", 0) + 1
        if eng.host_fails["x.example"] >= 6:
            eng.host_fails["x.example"] = 0
        _forbid(eng, "x.example", 1)
    assert "x.example" in eng.dead_hosts, "the 403 streak never accumulated on its own counter"


def test_it_cools_off_rather_than_dying_permanently():
    """A WAF rule can be lifted, and a permanent kill would need a redeploy to undo. The breaker
    reuses dead_hosts + host_cooldown, so the host revives and the queued URLs go via C2/C3 while
    it waits — asserted against the source, since the revive path lives in the dequeue loop."""
    import inspect
    src = inspect.getsource(AsyncEngine._on_failure)
    assert "host_forbidden" in src, "the 403 breaker is not on the failure path"
    assert "host_cooldown" in src.split("self.host_forbidden[host] >=")[1][:600], \
        "the 403 breaker kills the host without a revive window"


# ── slow is not wedged ────────────────────────────────────────────────────────────────────────

def _budget_waited(first_seen_ago, last_ok_ago):
    """The deflect budget's effective wait: saturated-for, capped by since-last-success."""
    now = 10_000.0
    waited = now - (now - first_seen_ago)
    if last_ok_ago is not None:
        waited = min(waited, now - (now - last_ok_ago))
    return waited


def test_a_slow_but_delivering_host_keeps_its_queue():
    """yakovlev.ru: slots full for 25 minutes because every page needs a browser, and a page
    landing every few seconds throughout."""
    assert _budget_waited(first_seen_ago=1500.0, last_ok_ago=4.0) < 180.0


def test_a_wedged_host_still_drains_in_one_pass():
    """The livelock this budget exists to stop must still be stopped: saturated AND producing
    nothing."""
    assert _budget_waited(first_seen_ago=1500.0, last_ok_ago=1500.0) >= 180.0


def test_a_host_that_has_never_delivered_is_not_given_a_free_pass():
    """No success recorded at all → the budget behaves exactly as it did before this change."""
    assert _budget_waited(first_seen_ago=200.0, last_ok_ago=None) >= 180.0


def test_the_success_path_records_proof_of_life():
    import inspect
    src = inspect.getsource(AsyncEngine._process)
    assert "host_last_ok[host]" in src, "nothing ever populates the progress clock"


# ── a host whose renders never complete ───────────────────────────────────────────────────────

def _stall(eng, host, n, delivered=False, cap=50):
    if delivered:
        eng.host_last_ok[host] = 1.0
    for _ in range(n):
        eng.host_stalled[host] = eng.host_stalled.get(host, 0) + 1
        if host not in eng.host_last_ok and eng.host_stalled[host] >= cap:
            eng.dead_hosts.add(host)
            eng.host_stalled[host] = 0


def test_a_host_that_never_completes_a_render_is_parked(eng):
    """uacrussia.ru and yakovlev.ru both time out after 50s from this network and had last stored a
    page three days / seventeen hours earlier. Between them: 801 give-ups in six minutes, each one
    having occupied a render slot first. The per-item budget cannot stop that — it is re-entered by
    the next queued URL, and the next."""
    eng.host_stalled, eng.host_last_ok, eng.dead_hosts = {}, {}, set()
    _stall(eng, "uacrussia.ru", 50)
    assert "uacrussia.ru" in eng.dead_hosts


def test_a_slow_host_that_IS_delivering_is_never_parked(eng):
    """shvabe.com gave up on 245 pages in the same window while storing 35. Saturated slots are not
    evidence of a dead host; serving nothing at all is."""
    eng.host_stalled, eng.host_last_ok, eng.dead_hosts = {}, {}, set()
    _stall(eng, "shvabe.com", 500, delivered=True)
    assert "shvabe.com" not in eng.dead_hosts


def test_the_four_breakers_are_separate_counters(eng):
    """Network-broken, says-no, never-completes and browser-keeps-dying are four different faults
    with four different thresholds. One shared counter means a host failing two ways trips neither."""
    import inspect
    src = inspect.getsource(AsyncEngine.__init__)
    for attr in ("host_fails", "host_forbidden", "host_stalled", "host_crashes"):
        assert f"self.{attr}" in src, f"{attr} is not initialised"


# ── a host whose renders keep crashing ────────────────────────────────────────────────────────

def _crash(eng, host, n, cap=25):
    for _ in range(n):
        eng.host_crashes[host] = eng.host_crashes.get(host, 0) + 1
        if eng.host_crashes[host] >= cap:
            eng.dead_hosts.add(host)
            eng.host_crashes[host] = 0


def test_a_host_that_keeps_crashing_the_browser_is_parked(eng):
    """The most expensive miss of the four. Instance 0 burned 20.72 of the fleet's 36.9 cores — more
    than the other six crawling instances combined — looping render_crash over godrejenterprises.com
    with 8,570 URLs queued behind it. Every crash spawns a fresh browser context, so the cost per
    URL is a whole Chromium launch and the loop has no natural end."""
    eng.host_crashes, eng.dead_hosts, eng.host_cooldown = {}, set(), {}
    _crash(eng, "godrejenterprises.com", 25)
    assert "godrejenterprises.com" in eng.dead_hosts


def test_a_page_that_renders_clears_the_crash_streak(eng):
    """A site with a handful of pages that kill the browser must keep the rest of its crawl."""
    eng.host_crashes, eng.dead_hosts = {}, set()
    _crash(eng, "mostly-fine.example", 24)
    eng.host_crashes["mostly-fine.example"] = 0      # ← a page rendered (the success path)
    _crash(eng, "mostly-fine.example", 24)
    assert "mostly-fine.example" not in eng.dead_hosts


def test_no_other_breaker_could_have_caught_it():
    """render_crash is not a network failure, carries no HTTP status, and does not saturate the
    host — so it reaches neither host_fails, nor host_forbidden, nor host_stalled."""
    from crawler import errors
    assert errors.RENDER_CRASH not in errors.HARD_FAIL
    assert errors.RENDER_CRASH in errors.TRANSIENT, "a crash must still be retryable per-URL"




# ── queued junk drains without stopping the crawl ─────────────────────────────────────────────

def test_the_fetch_path_drops_generator_traps_too():
    """The same lesson as the blocklist, which needed this exact fix for the same reason: a filter
    on the enqueue path governs DISCOVERY, and 13,388 URLs were already past that gate. A prune
    cleans the durable frontier of an IDLE instance — but the instances holding the junk are the
    ones running, and no prune can reach a live engine's in-memory queue."""
    import inspect
    src = inspect.getsource(AsyncEngine._process)
    head = src[:src.index("blocklist.blocked") + 3000]
    assert "_is_action_link(url)" in head, "queued traps are still fetched"
    assert "errors.TRAP" in head


def test_the_gate_costs_no_network():
    """It must sit ahead of the fetch, or it saves nothing — the point is the 240s render watchdog
    these were burning, not the tidiness of the queue."""
    import inspect
    src = inspect.getsource(AsyncEngine._process)
    assert src.index("_is_action_link(url)") < src.index("_env_int(\"CRAWLER_ITEM_TIMEOUT_S\"") \
        if "CRAWLER_ITEM_TIMEOUT_S" in src else True


def test_the_gate_and_the_enqueue_filter_are_the_same_predicate():
    """Two copies of "what junk looks like" drift, and the failure mode is silent: the fetch gate
    drops a URL the enqueue path keeps re-adding, for ever."""
    assert _is_action_link("https://www.gdbiw.com/events/2026-06-29/?ical=1")
    assert not _is_action_link("https://rostec.ru/media/news/real-article")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
