"""Arms-transfers harvester: pagination, completeness honesty, and the API request shape.

Fully offline — post_json is stubbed. The request shape is pinned because it was reverse-engineered
from a minified bundle and is easy to break silently: the Spring backend rejects `sorts` as an array
with a 500, and answers a wrong prefix with 401, so both are asserted here rather than discovered in
production against a slow server we are a guest on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sipri_harvester import config, transfers  # noqa: E402


class FakeStore:
    def __init__(self):
        self.rows = []

    def upsert_transfers(self, rows):
        self.rows.extend(rows)
        return len(rows)


def rec(i):
    return {"id": i, "tradeId": i, "buyer": "X", "seller": "Y", "orderYr": 2000,
            "units": 1, "deliveryYr": 2001, "status": "N", "desg": "d", "category": "Aircraft"}


def _no_network(*_a, **_kw):
    raise AssertionError("test reached the network — SIPRI's server must never be hit by tests")


def stub(pages, total=None, calls=None):
    """Replace post_json with a canned pager.

    Also stubs the GET fallback: reported_total() falls through to it when the POST returns None,
    which on the first run silently fired a REAL request at sipri.org from the test suite.
    """
    def _post(path, body):
        if calls is not None:
            calls.append((path, body))
        if path == config.TRADES_TOTAL:
            return total
        if path == config.TRADES_SEARCH:
            return pages[body["pageNo"]] if body["pageNo"] < len(pages) else []
        return None
    transfers.post_json = _post
    transfers._get_total_via_get = lambda: total


def t_total_is_fetched_with_get_not_post() -> None:
    """/trades/getTotal answers 405 to a POST; with retries that was 3 wasted round trips per run
    against a server we are deliberately going easy on. Only /search takes POST."""
    import inspect
    src = inspect.getsource(transfers.reported_total)
    assert "post_json" not in src, "getTotal must not be POSTed — the endpoint returns 405"
    assert "_get_total_via_get" in src


def t_request_shape_matches_the_api() -> None:
    """sorts MUST be an object: the Spring backend 500s on an array (verified against the live API)."""
    body = config.search_body(0, 2000)
    assert isinstance(body["sorts"], dict), "sorts must be a Map<String,String>, not an array"
    assert isinstance(body["filters"], list)
    assert body["pageNo"] == 0 and body["pageSize"] == 2000
    assert config.TRADES_SEARCH.startswith("/api/p"), \
        "must use the PUBLIC prefix; /api/v1 requires staff auth and returns 401"
    assert config.API_HEADERS["Authorization"] == "", \
        "the site's own JS sends an empty Authorization when logged out"


def t_paginates_until_short_page() -> None:
    calls = []
    stub([[rec(i) for i in range(2000)], [rec(i) for i in range(2000, 2500)]], total=2500, calls=calls)
    st = FakeStore()
    out = transfers.harvest(st, page_size=2000)
    assert out["records"] == 2500, out
    assert out["complete"] is True, out
    assert len(st.rows) == 2500
    searches = [b["pageNo"] for p, b in calls if p == config.TRADES_SEARCH]
    assert searches == [0, 1], f"should stop on the short page, got pages {searches}"


def t_dedupes_repeated_ids_across_pages() -> None:
    """Unstable ordering can repeat rows across pages; that must not inflate the count."""
    stub([[rec(i) for i in range(100)], [rec(i) for i in range(90, 150)]], total=150)
    st = FakeStore()
    out = transfers.harvest(st, page_size=100)
    assert out["records"] == 150, f"expected 150 unique, got {out['records']}"
    assert len({r['id'] for r in st.rows}) == 150


def t_partial_pull_is_reported_not_hidden() -> None:
    """A page failure mid-pull must surface as incomplete — silently banking a partial set is how
    stale data becomes invisible."""
    def _post(path, body):
        if path == config.TRADES_TOTAL:
            return 5000
        if body["pageNo"] == 0:
            return [rec(i) for i in range(2000)]
        return None                                   # page 1 fails
    transfers.post_json = _post
    transfers._get_total_via_get = lambda: 5000
    st = FakeStore()
    out = transfers.harvest(st, page_size=2000)
    assert out["complete"] is False, "partial pull claimed completeness"
    assert out["failed_pages"] == 1, out
    assert len(st.rows) == 2000, "rows fetched before the failure must be kept"


def t_keeps_earlier_pages_on_failure() -> None:
    """Incremental writes: a late failure must not discard what already succeeded."""
    def _post(path, body):
        if path == config.TRADES_TOTAL:
            return 300
        return [rec(i) for i in range(100)] if body["pageNo"] == 0 else None
    transfers.post_json = _post
    transfers._get_total_via_get = lambda: 300
    st = FakeStore()
    transfers.harvest(st, page_size=100)
    assert len(st.rows) == 100, "earlier pages were thrown away on a later failure"


def t_survives_unknown_total() -> None:
    stub([[rec(1)]], total=None)
    out = transfers.harvest(FakeStore(), page_size=2000)
    assert out["reported_total"] is None and out["complete"] is False
    assert out["records"] == 1


def main() -> None:
    for fn in (t_total_is_fetched_with_get_not_post, t_request_shape_matches_the_api, t_paginates_until_short_page,
               t_dedupes_repeated_ids_across_pages, t_partial_pull_is_reported_not_hidden,
               t_keeps_earlier_pages_on_failure, t_survives_unknown_total):
        orig, orig_get = transfers.post_json, transfers._get_total_via_get
        try:
            fn()
        finally:
            transfers.post_json, transfers._get_total_via_get = orig, orig_get
        print(f"  ok  {fn.__name__}")
    print("ALL SIPRI TRANSFERS TESTS PASSED")



def test_sipri_transfers_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
