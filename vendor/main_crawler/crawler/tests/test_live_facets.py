"""Live Data facets: the by-type / by-tier breakdowns must not lie about the store.

Two failure modes are being pinned here, both of which have burned this dashboard before. First,
a dead dependency rendering as zeros — an operator cannot tell "we crawled nothing" from "Postgres
is down" if both print 0, so the no-DB path must carry an error instead. Second, the source filter
reaching SQL as text: it comes straight off a URL query string, so it is asserted to arrive as a
bound parameter.

Runs with no Postgres — the query layer is stubbed, and any attempt at a real connection is
recorded and refused (see NO_CONNECT: pgstore.query swallows exceptions, so a raise alone would
disappear; the flag is what the tests actually assert on).
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.pop("DATABASE_URL", None)      # before the import: nothing here may find a real DB

NO_CONNECT = {"attempted": False}


def _refuse(*_a, **_kw):
    NO_CONNECT["attempted"] = True
    raise AssertionError("a live Postgres connection was attempted")


# Plain assignment, not setdefault: a machine that HAS psycopg installed must get the guard too,
# otherwise this test would quietly become a connection test on a developer laptop.
_pg = types.ModuleType("psycopg")
_pg.connect = _refuse
sys.modules["psycopg"] = _pg

from ingest_api import live, pgstore  # noqa: E402

_REAL_QUERY = pgstore.query
CALLS: list[tuple] = []                   # (sql, params) of every query the facets issued

EVIL = "'; DROP TABLE documents; --"      # what the filter box gets fed on a bad day


def stub(*results) -> None:
    """Answer each query() with the next canned result; record what was asked."""
    CALLS.clear()
    queue = list(results)

    def fake(sql, params=()):
        CALLS.append((sql, params))
        return queue.pop(0) if queue else []

    pgstore.query = fake


def no_db() -> None:
    """The real query layer with DATABASE_URL unset — the genuine unconfigured path."""
    CALLS.clear()
    pgstore.query = _REAL_QUERY
    os.environ.pop("DATABASE_URL", None)
    # has_db() latches True for the life of the process once any DB has answered. Run after a test
    # that reached the real Postgres, this whole "without a DB" premise silently evaporated and the
    # assertions below failed with no hint why. Clear the latch, not just the env var.
    pgstore._HAS_DB = False
    # Likewise this flag: it records "someone dialled a database" for the whole PROCESS, so a
    # connection made by an earlier test file failed these assertions here. Both are process-wide
    # state that only looked local because the file used to run alone.
    NO_CONNECT["attempted"] = False


# ── an absent store must be reported, never rendered as an empty one ─────────
def t_by_type_without_db_reports_the_error() -> None:
    no_db()
    out = pgstore.by_type()
    assert out == {"error": "no-db"}, out
    assert "documents" not in out, "zeros returned for a missing DB — an empty store looks the same"
    assert not NO_CONNECT["attempted"]


def t_by_served_by_without_db_is_empty() -> None:
    """A list has no room for an error field; the router's has_db() gate is what reports it."""
    no_db()
    assert pgstore.by_served_by() == []
    assert not NO_CONNECT["attempted"]


def t_types_endpoint_without_db_carries_the_error() -> None:
    old = pgstore.has_db
    pgstore.has_db = lambda: False
    try:
        assert live.types() == {"types": {"error": "no-db"}, "served_by": []}
    finally:
        pgstore.has_db = old


# ── the aggregates ───────────────────────────────────────────────────────────
def t_by_type_maps_every_facet() -> None:
    stub([(75785, 75700, 74010, 1203, 3480, 15327433805, 257983118)])
    assert pgstore.by_type() == {
        "documents": 75785, "with_html": 75700, "with_text": 74010,
        "with_pdf": 1203, "pdf_attachments": 3480,
        "html_bytes": 15327433805, "text_chars": 257983118}


def t_by_type_scans_the_view_once() -> None:
    """with_pdf and pdf_attachments come off one LATERAL count, not a second pass — at 75k rows
    with TOASTed html a duplicate scan is seconds of dashboard latency."""
    stub([(1, 1, 1, 1, 2, 10, 20)])
    pgstore.by_type()
    assert len(CALLS) == 1, f"{len(CALLS)} queries issued for one breakdown"
    sql = CALLS[0][0]
    assert sql.count("documents_current") == 1, "the view is scanned more than once"
    assert "documents_current" in sql, "reads the raw table — will not reconcile with the summary"


def t_null_served_by_becomes_unknown() -> None:
    """Rows predating the column have served_by NULL. That is a bucket to show, not one to drop."""
    stub([("c1_live", 70123, 14000000000), (None, 4102, 900000000)])
    assert pgstore.by_served_by() == [
        {"served_by": "c1_live", "pages": 70123, "html_bytes": 14000000000},
        {"served_by": "(unknown)", "pages": 4102, "html_bytes": 900000000}]


def t_served_by_reads_the_current_view() -> None:
    stub([("c1_live", 1, 1)])
    pgstore.by_served_by()
    assert "FROM documents_current" in CALLS[0][0], CALLS[0][0]


# ── the filter must never become SQL ─────────────────────────────────────────
def t_source_filter_is_a_bound_parameter() -> None:
    stub([])
    pgstore.by_source(50, EVIL)
    sql, params = CALLS[0]
    assert EVIL not in sql, "the filter value was interpolated into the statement"
    assert "DROP" not in sql.upper(), sql
    assert "ILIKE %s" in sql, "no placeholder for the filter"
    assert f"%{EVIL}%" in params, params
    assert 50 in params, "the limit stopped being bound"


def t_empty_filter_leaves_the_query_unfiltered() -> None:
    """Default "" must not degrade into ILIKE '%%' — the unfiltered list is the common call."""
    stub([])
    pgstore.by_source(200)
    sql, params = CALLS[0]
    assert "ILIKE" not in sql, sql
    assert params == (200,), params


def t_sources_endpoint_passes_the_filter_through() -> None:
    old = pgstore.has_db
    pgstore.has_db = lambda: True
    stub([("sipri.org", 12, "2026-07-21T00:00:00Z", 12, 900, 3)])  # +pdfs
    try:
        rows = live.sources(limit=200, q=EVIL)
    finally:
        pgstore.has_db = old
    assert rows == [{"source_id": "sipri.org", "pages": 12, "last_at": "2026-07-21T00:00:00Z",
                     "with_html": 12, "html_bytes": 900, "pdfs": 3}], rows
    assert f"%{EVIL}%" in CALLS[0][1], "the router dropped or inlined the filter"


def t_types_endpoint_shape() -> None:
    old = pgstore.has_db
    pgstore.has_db = lambda: True
    stub([(3, 3, 2, 1, 4, 100, 50)], [("c3_camofox", 3, 100)])
    try:
        out = live.types()
    finally:
        pgstore.has_db = old
    assert set(out) == {"types", "served_by"}, out
    assert out["types"]["with_pdf"] == 1 and out["types"]["pdf_attachments"] == 4
    assert out["served_by"] == [{"served_by": "c3_camofox", "pages": 3, "html_bytes": 100}]


def main() -> None:
    # Process-wide flags, reset at the START of the run: pytest runs this file inside a session
    # where earlier files legitimately connect to a database, and inheriting their state made this
    # suite fail in a way that looked like a facet bug.
    NO_CONNECT["attempted"] = False
    pgstore._HAS_DB = False
    for fn in (t_by_type_without_db_reports_the_error, t_by_served_by_without_db_is_empty,
               t_types_endpoint_without_db_carries_the_error, t_by_type_maps_every_facet,
               t_by_type_scans_the_view_once, t_null_served_by_becomes_unknown,
               t_served_by_reads_the_current_view, t_source_filter_is_a_bound_parameter,
               t_empty_filter_leaves_the_query_unfiltered,
               t_sources_endpoint_passes_the_filter_through, t_types_endpoint_shape):
        fn()
        print(f"  ok  {fn.__name__}")
    assert not NO_CONNECT["attempted"], "the suite reached for a real database"
    pgstore.query = _REAL_QUERY
    print("ALL LIVE FACET TESTS PASSED")



def test_live_facets_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
