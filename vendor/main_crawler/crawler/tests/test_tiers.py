"""GET /v1/tiers — the dashboard's C1/C2/C3 up/down strip, exercised OFFLINE.

This endpoint's whole job is to report a broken tier, so the tests are written from the failure
side: every one of them asserts that something being DOWN comes back as data with a 200 shape
rather than as an exception. An operator who loses the strip because one tier is unreachable has
lost the other two tiers as well, which is the opposite of what a status light is for.

Nothing here touches the network. The three probe helpers are replaced with stubs and httpx is
booby-trapped underneath them, because a test that really fetched example.com or archive.org would
be slow, would spend our archive.org rate budget, and would go red when someone else's site is
down — a test that lies about our code is worse than no test.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from crawler_api import app as api  # noqa: E402


def _no_network(*a, **kw):
    raise AssertionError("a tier probe reached the network — probes must be stubbed in this suite")


_REAL_SEND = httpx.Client.send
_REAL_ASEND = httpx.AsyncClient.send


# Backstop under the stubs: httpx.Client.send is the single funnel every httpx call arrives at
# (get/stream/httpx.get all land here), so a forgotten stub fails loudly instead of quietly fetching.
#
# MODULE-SCOPED, not import-time. pytest runs the whole suite in ONE process, so arming this at
# import poisoned every later test too: starlette's TestClient sends through httpx.Client.send, and
# test_crawler_api.py::test_health went red with "a tier probe reached the network" while making no
# probe at all. A trap that outlives its own module tests other people's code.
def setup_module(module=None) -> None:
    httpx.Client.send = _no_network
    httpx.AsyncClient.send = _no_network


def teardown_module(module=None) -> None:
    httpx.Client.send = _REAL_SEND
    httpx.AsyncClient.send = _REAL_ASEND

# The three shapes a probe helper can return, named so the assertions read as intent.
UP = {"enabled": True, "up": True, "detail": "HTTP 200"}
DOWN = {"enabled": True, "up": False, "detail": "connect refused"}
OFF = {"enabled": False, "up": False, "detail": "disabled"}


def fresh() -> dict:
    """Cold cache + a per-tier call counter, so "did it re-probe?" is an answerable question."""
    api._tier_cache.update(at=0.0, payload=None)
    return {"c1": 0, "c2": 0, "c3": 0, "c4": 0}


def stub(counts: dict, tier: str, result) -> None:
    """Point one tier at a fake probe. `result` is either the dict to return or an exception to
    raise — the two things a real probe does."""
    def _probe() -> dict:
        counts[tier] += 1
        if isinstance(result, BaseException):
            raise result
        return result

    setattr(api, f"_probe_{tier}", _probe)


def stub_all(counts: dict, c1=UP, c2=UP, c3=UP, c4=UP) -> None:
    stub(counts, "c1", c1)
    stub(counts, "c2", c2)
    stub(counts, "c3", c3)
    stub(counts, "c4", c4)


def call() -> dict:
    """One request to the endpoint. It is `async def`, and these are plain functions."""
    return asyncio.run(api.tiers())


def t_shape_is_the_wire_contract() -> None:
    """The dashboard is written against these exact keys — a rename here is a blank tile there."""
    counts = fresh()
    stub_all(counts)
    body = call()
    assert set(body) == {"checked_at", "cached", "c1", "c2", "c3", "c4"}, body
    assert body["cached"] is False and body["checked_at"].endswith("Z")
    for tier, label in (("c1", "C1 live"), ("c2", "C2 Wayback"), ("c3", "C3 CamoFox"),
                        ("c4", "C4 host solver")):
        t = body[tier]
        assert set(t) == {"label", "up", "latency_ms", "detail", "enabled"}, t
        assert t["label"] == label
        assert t["up"] is True and t["enabled"] is True
        assert isinstance(t["latency_ms"], int) and t["latency_ms"] >= 0


def t_dead_tier_reports_down_and_does_not_raise() -> None:
    """The core case. A dead tier is data; the live tiers must still be reported alongside it."""
    counts = fresh()
    stub_all(counts, c2=DOWN)
    body = call()
    assert body["c2"]["up"] is False
    assert body["c2"]["enabled"] is True, "unreachable is not the same as switched off"
    assert body["c2"]["detail"] == "connect refused"
    assert body["c1"]["up"] is True and body["c3"]["up"] is True, "one dead tier poisoned the rest"


def t_disabled_tier_never_claims_up() -> None:
    """enabled:false with up:true would send the operator debugging a tier that is simply off."""
    counts = fresh()
    stub_all(counts, c3=OFF)
    body = call()
    assert body["c3"]["enabled"] is False and body["c3"]["up"] is False
    assert body["c3"]["detail"] == "disabled"


def t_every_tier_down_still_answers() -> None:
    """The worst real state — no egress at all — is the one the operator most needs to see."""
    counts = fresh()
    stub_all(counts, c1=DOWN, c2=DOWN, c3=OFF, c4=OFF)
    body = call()
    assert [body[t]["up"] for t in ("c1", "c2", "c3", "c4")] == [False, False, False, False]
    assert body["checked_at"], "a total outage must still be a timestamped answer"


def t_probe_exception_becomes_detail_not_a_500() -> None:
    counts = fresh()
    stub_all(counts, c1=RuntimeError("boom " * 60))
    body = call()                                   # must not propagate
    assert body["c1"]["up"] is False
    assert body["c1"]["detail"].startswith("boom")
    assert len(body["c1"]["detail"]) <= 80, "unbounded exception text on a one-line operator tile"


def t_probe_exception_with_no_message_still_names_itself() -> None:
    """str(exc) is empty for plenty of transport errors; "" on a red tile explains nothing."""
    counts = fresh()
    stub_all(counts, c2=ConnectionResetError())
    body = call()
    assert body["c2"]["up"] is False and body["c2"]["detail"] == "ConnectionResetError"


def t_hung_probe_is_bounded_not_a_hang() -> None:
    """A probe that never returns must not hold the dashboard open — the deadline turns it red."""
    import time as _time

    counts = fresh()
    stub_all(counts)

    def _hang() -> dict:
        counts["c1"] += 1
        _time.sleep(0.4)
        return UP

    api._probe_c1 = _hang
    original = api._TIER_DEADLINE_S
    api._TIER_DEADLINE_S = 0.05                     # 10s is the production ceiling, not a test's
    try:
        t0 = _time.monotonic()
        body = call()
        elapsed = _time.monotonic() - t0
    finally:
        api._TIER_DEADLINE_S = original
    # Prove the slow probe actually ran before judging the deadline. Run after some other suites
    # this returned in 5ms with c1 up — the hung probe was never reached — and the bare assertion
    # below then read "assert True is False", which says nothing about why. Fail on the real cause.
    assert elapsed >= 0.04 or body["c1"]["detail"] == "timeout", (
        f"the hung probe never ran (call returned in {elapsed*1000:.0f}ms, c1={body['c1']}) — "
        "the deadline was not exercised, so this result proves nothing")
    assert body["c1"]["up"] is False and body["c1"]["detail"] == "timeout"
    assert body["c2"]["up"] is True, "a hung tier stalled the others — gather is not concurrent"


def t_second_call_is_served_from_cache() -> None:
    """Why the cache exists: the dashboard polls, and each uncached call is four live requests
    against hosts we do not own — archive.org answers abuse with a doubling IP ban."""
    counts = fresh()
    stub_all(counts)
    first = call()
    second = call()
    assert first["cached"] is False and second["cached"] is True
    assert counts == {"c1": 1, "c2": 1, "c3": 1, "c4": 1}, f"cache re-probed the tiers: {counts}"
    assert second["checked_at"] == first["checked_at"], \
        "cached payload was re-timestamped — it would read as a fresh probe"
    assert second["c1"] == first["c1"], "cached tiers differ from the ones actually probed"


def t_cache_expires_and_reprobes() -> None:
    """The other half of the contract: 30s stale, not permanent. A status light that never
    refreshes is a picture of the past."""
    counts = fresh()
    stub_all(counts)
    call()
    # Age the entry past the TTL rather than sleeping 30s inside a test run.
    api._tier_cache["at"] -= api._TIER_TTL_S + 1
    body = call()
    assert body["cached"] is False
    assert counts == {"c1": 2, "c2": 2, "c3": 2, "c4": 2}, f"stale entry was still served: {counts}"


def t_cache_hit_does_not_hide_a_tier_going_down() -> None:
    """A cached UP must not survive the TTL — the operator would be looking at a green tile for a
    tier that has been dead for minutes."""
    counts = fresh()
    stub_all(counts)
    assert call()["c1"]["up"] is True
    stub(counts, "c1", DOWN)
    api._tier_cache["at"] -= api._TIER_TTL_S + 1
    assert call()["c1"]["up"] is False


# ── the C4 probe itself ───────────────────────────────────────────────────────────────────────
# Unlike C1-C3 this one is not "did a request succeed" — the failure it exists to catch is a
# request that succeeds against the WRONG SERVICE. So its branches get their own tests.

# Captured at IMPORT, before any test runs. stub()/stub_all() overwrite api._probe_c4 and never put
# it back, so by the time these run `api._probe_c4` is whatever the last cache test left there —
# reading the module attribute here would silently test the stub instead of the code. (The same
# never-restored-monkeypatch trap has already bitten this repo once, in the orchestrator suite.)
_REAL_PROBE_C4 = api._probe_c4


def _c4(monkey_health, enabled=True):
    """Run the real _probe_c4 against a stubbed hostsolver."""
    from crawler import hostsolver
    real_en, real_h = hostsolver.enabled, hostsolver.health
    hostsolver.enabled = lambda: enabled
    hostsolver.health = lambda: monkey_health
    try:
        return _REAL_PROBE_C4()
    finally:
        hostsolver.enabled, hostsolver.health = real_en, real_h


def t_c4_off_is_disabled_not_down() -> None:
    """No solver configured is a deployment choice — painting it red would train the operator to
    ignore a red dot."""
    r = _c4(None, enabled=False)
    assert r["enabled"] is False and r["up"] is False and r["detail"] == "disabled"


def t_c4_unreachable_is_down() -> None:
    """The tunnel died. hostsolver.health() swallows the exception and returns None."""
    r = _c4(None)
    assert r["enabled"] is True and r["up"] is False and r["detail"] == "unreachable"


def t_c4_wrong_service_answering_is_DOWN_not_up() -> None:
    """THE case this probe exists for. A dead ssh tunnel once let requests reach a different local
    service holding the same port, which answered 200 happily and cost most of a day. A reply is
    not an identity: host_solver's /health uniquely carries browser_open and solved_hosts."""
    r = _c4({"ok": True, "status": "healthy"})
    assert r["up"] is False and "NOT the host solver" in r["detail"]


def t_c4_real_solver_is_up() -> None:
    r = _c4({"ok": True, "browser_open": True, "host": "zoopla.co.uk",
             "solved_hosts": ["zoopla.co.uk"]})
    assert r["enabled"] is True and r["up"] is True
    assert "browser open" in r["detail"] and "1 host(s) solved" in r["detail"]


def main() -> None:
    # pytest calls setup_module/teardown_module for us; standalone has to do it by hand.
    setup_module()
    try:
        for fn in (t_shape_is_the_wire_contract, t_dead_tier_reports_down_and_does_not_raise,
                   t_disabled_tier_never_claims_up, t_every_tier_down_still_answers,
                   t_probe_exception_becomes_detail_not_a_500,
                   t_probe_exception_with_no_message_still_names_itself,
                   t_hung_probe_is_bounded_not_a_hang, t_second_call_is_served_from_cache,
                   t_cache_expires_and_reprobes, t_cache_hit_does_not_hide_a_tier_going_down,
                   t_c4_off_is_disabled_not_down, t_c4_unreachable_is_down,
                   t_c4_wrong_service_answering_is_DOWN_not_up, t_c4_real_solver_is_up):
            fn()
            print(f"  ok  {fn.__name__}")
    finally:
        teardown_module()
    print("ALL TIER-STATUS TESTS PASSED")



def test_tiers_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
