"""Three counters that made a healthy crawl unreadable, and one host key that made it impolite.

All four came out of the same stopped batch (2026-07-28, 18,620 fetches over ~3.5 h):

  fetched 18,620   kept 1,174   dropped_by_gate 0        <- a 94% hole with no name on it
  the missing 16,912 were `skipped_duplicate`, counted per job but absent from _STAT_KEYS
  render_gaveup 1,345 beside "3 of 64 render permits in use"  <- reads as a starved browser pool
  bhel.com 2,634 issues AND www.bhel.com 1,812  <- two politeness budgets for one server

Runnable directly:  python tests/test_crawl_accounting.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import errors  # noqa: E402
from crawler.async_engine import _STAT_KEYS, _hkey  # noqa: E402


def test_duplicate_skips_are_published_live():
    """The largest single outcome of the batch. Without this key _tick_stat drops the count on the
    floor and progress.totals can never explain the gap between fetched and kept."""
    assert "skipped_duplicate" in _STAT_KEYS


def test_host_busy_giveups_are_not_reported_as_render_giveups():
    """A host-busy give-up never asked the browser pool for anything — the deflect budget expired
    waiting for a per-host slot. Sharing render_gaveup's counter pointed every diagnosis at the
    render lane, which was 95% idle at the time."""
    assert "host_busy_gaveup" in _STAT_KEYS
    assert "render_gaveup" in _STAT_KEYS        # still there — the two must not be merged either


def test_every_stat_key_is_unique():
    assert len(_STAT_KEYS) == len(set(_STAT_KEYS))


def test_www_and_apex_share_one_politeness_budget():
    assert _hkey("www.bhel.com") == _hkey("bhel.com") == "bhel.com"
    assert _hkey("WWW.BHEL.COM") == "bhel.com"
    assert _hkey("www.bhel.com.") == "bhel.com"          # trailing dot is the same host


def test_only_a_leading_www_is_stripped():
    """`wwwx.com` and `my.www.com` are ordinary hosts. Over-stripping would merge the budgets of
    two unrelated servers, which is the same bug pointed the other way."""
    assert _hkey("wwwx.com") == "wwwx.com"
    assert _hkey("my.www.com") == "my.www.com"
    assert _hkey("www2.bhel.com") == "www2.bhel.com"
    assert _hkey("") == ""


def test_stripping_www_does_not_lose_careful_host_status():
    """The limiter now hands is_careful_host the normalised key. A ministry must stay careful."""
    for h in ("www.gov.uk", "www.mod.gov.my", "www.epa.gov", "www.army.mil"):
        assert errors.is_careful_host(_hkey(h)), f"{h} stopped being a careful host"
    assert not errors.is_careful_host(_hkey("www.bhel.com"))


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CRAWL-ACCOUNTING TESTS PASSED")


if __name__ == "__main__":
    main()
