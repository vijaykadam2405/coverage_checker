"""Paid-proxy access control: every gate must FAIL CLOSED.

This is the only code path in the repo that spends money per request, so the tests are written from
the attacker's side: each one asserts that a specific way of accidentally spending is refused. A
false negative here is a bill, not a bug report.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import paid_proxy  # noqa: E402


def fresh(**env):
    """Reset the gate to a known state with a private usage file."""
    d = tempfile.mkdtemp()
    paid_proxy._USAGE_PATH = os.path.join(d, "usage.json")
    os.environ["PAID_PROXY_USAGE_FILE"] = paid_proxy._USAGE_PATH
    for k in ("BRIGHTDATA_ENABLED", "BRIGHTDATA_HOSTS", "BRIGHTDATA_DAILY_REQUESTS"):
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})


def t_disabled_by_default() -> None:
    """Deploying the code must never enable spend."""
    fresh(BRIGHTDATA_HOSTS="sipri.org")
    assert paid_proxy.enabled() is False
    assert not paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=True)


def t_empty_allowlist_grants_nothing() -> None:
    """The dangerous default: switch on, forget the list. Must grant nothing, not everything."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="")
    assert paid_proxy.allowed_hosts() == set()
    for url in ("https://www.sipri.org/", "https://example.com/", "https://anything.gov/"):
        assert not paid_proxy.allow(url, free_tiers_tried=True), f"empty allowlist permitted {url}"


def t_free_tiers_must_be_tried_first() -> None:
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org")
    assert not paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=False), \
        "spent before the free tiers were attempted"
    assert paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=True)


def t_subdomain_matches_but_lookalike_does_not() -> None:
    """`notsipri.org` endswith `sipri.org` as a STRING — a naive suffix check spends on a lookalike."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org")
    assert paid_proxy.allow("https://www.sipri.org/x", free_tiers_tried=True)
    assert paid_proxy.allow("https://sipri.org/x", free_tiers_tried=True)
    for bad in ("https://notsipri.org/", "https://sipri.org.evil.com/", "https://xsipri.org/"):
        assert not paid_proxy.allow(bad, free_tiers_tried=True), f"lookalike allowed: {bad}"


def t_daily_cap_is_hard() -> None:
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org", BRIGHTDATA_DAILY_REQUESTS=3)
    for _ in range(3):
        assert paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=True)
        paid_proxy.note_use("https://www.sipri.org/")
    assert paid_proxy.remaining_today() == 0
    assert not paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=True), "spent past the cap"


def t_cap_survives_restart() -> None:
    """The counter is on disk precisely so a container restart cannot reset the budget."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org", BRIGHTDATA_DAILY_REQUESTS=2)
    paid_proxy.note_use("https://www.sipri.org/")
    path = paid_proxy._USAGE_PATH
    paid_proxy._USAGE_PATH = path                       # simulate a fresh process, same volume
    assert paid_proxy.usage_today() == 1, "usage did not persist"
    assert paid_proxy.remaining_today() == 1


def t_corrupt_counter_denies_rather_than_resets() -> None:
    """A damaged usage file must not read as 'zero spent so far'."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org", BRIGHTDATA_DAILY_REQUESTS=100)
    with open(paid_proxy._USAGE_PATH, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert paid_proxy.remaining_today() == 0, "corrupt counter reset the budget"
    assert not paid_proxy.allow("https://www.sipri.org/", free_tiers_tried=True)


def t_new_day_resets_budget() -> None:
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org", BRIGHTDATA_DAILY_REQUESTS=5)
    with open(paid_proxy._USAGE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"date": "2000-01-01", "requests": 5}, fh)
    assert paid_proxy.remaining_today() == 5, "yesterday's spend still blocking today"


def t_status_reports_the_real_switches() -> None:
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="sipri.org, example.com", BRIGHTDATA_DAILY_REQUESTS=7)
    st = paid_proxy.status()
    assert st["enabled"] is True
    assert st["hosts"] == ["example.com", "sipri.org"]
    assert st["daily_cap"] == 7 and st["remaining_today"] == 7
    fresh(BRIGHTDATA_HOSTS="sipri.org")
    assert paid_proxy.status()["endpoint"] is None, "endpoint exposed while disabled"


def t_wildcard_allows_any_host_but_cap_still_binds() -> None:
    """"*" is the automatic-escalation mode: any host may spend, but the daily cap is then the ONLY
    ceiling, so it must still be enforced exactly."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="*", BRIGHTDATA_DAILY_REQUESTS=2)
    for u in ("https://anything.example.com/", "https://another.gov/x"):
        assert paid_proxy.allow(u, free_tiers_tried=True), f"wildcard did not permit {u}"
    assert not paid_proxy.allow("https://x.com/", free_tiers_tried=False),         "wildcard bypassed the free-tiers-first rule"
    paid_proxy.note_use("https://a.com/"); paid_proxy.note_use("https://b.com/")
    assert not paid_proxy.allow("https://c.com/", free_tiers_tried=True),         "wildcard spent past the daily cap - with no allowlist the cap is the only guard"


def t_wildcard_must_be_explicit() -> None:
    """An empty list is still empty: the wildcard is opt-in, never a default."""
    fresh(BRIGHTDATA_ENABLED=1, BRIGHTDATA_HOSTS="")
    assert not paid_proxy.allow("https://anything.com/", free_tiers_tried=True)


def main() -> None:
    for fn in (t_disabled_by_default, t_empty_allowlist_grants_nothing,
               t_free_tiers_must_be_tried_first, t_subdomain_matches_but_lookalike_does_not,
               t_daily_cap_is_hard, t_cap_survives_restart,
               t_corrupt_counter_denies_rather_than_resets, t_new_day_resets_budget,
               t_status_reports_the_real_switches, t_wildcard_allows_any_host_but_cap_still_binds,
               t_wildcard_must_be_explicit):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL PAID-PROXY ACCESS CONTROL TESTS PASSED")



def test_paid_proxy_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
