"""pgstore.search() must never detoast and its body clause must match the functional index exactly.

No DB needed: we monkeypatch pgstore.query to capture the SQL it would run, then assert the shape.
The body tier is only safe because (a) it's gated on the index existing and (b) its expression is
character-identical to the CREATE INDEX expression — if either breaks, the query seq-scans and
detoasts the whole corpus. Both are pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest_api"))
import pgstore  # noqa: E402

BANNED = ["length(main_text)", "length(html)", "main_text <> ''", "html <> ''", "octet_length("]


def _capture(monkeypatch, *, index_ready: bool):
    """Replace pgstore.query with a recorder. Returns the list of (sql, params) for SEARCH queries
    (the readiness probe is answered but not recorded)."""
    monkeypatch.setattr(pgstore, "_BODY_TSV_READY", False, raising=False)
    calls = []

    def fake_query(sql, params=()):
        if "idx_docs_tsv" in sql:                 # the _body_search_ready() probe
            return [(1,)] if index_ready else []
        calls.append((sql, tuple(params)))        # a real search query
        return []

    monkeypatch.setattr(pgstore, "query", fake_query)
    return calls


def test_search_projects_no_toasted_column(monkeypatch):
    calls = _capture(monkeypatch, index_ready=True)
    pgstore.search("power generation", limit=50)
    assert calls, "search issued no query"
    for sql, _ in calls:
        low = sql.lower()
        # the SELECT list must be scalar-only — never the TOAST columns themselves
        select = low.split("from", 1)[0]
        assert "main_text" not in select and " html" not in select, sql
        for bad in BANNED:
            assert bad not in low, "detoasting form %r in: %s" % (bad, sql)


def test_body_clause_matches_the_index_expression(monkeypatch):
    calls = _capture(monkeypatch, index_ready=True)
    res = pgstore.search("turbine", limit=10)
    assert res["body_ready"] is True
    sqls = [s for s, _ in calls]
    # exactly one metadata query (title/url) and one body query (tsvector), in that order
    assert any("title ILIKE %s OR url ILIKE %s" in s for s in sqls), sqls
    body = [s for s in sqls if "to_tsvector" in s]
    assert len(body) == 1, sqls
    # MUST be character-identical to the CREATE INDEX expression or the planner seq-scans -> detoast
    assert "to_tsvector('simple', main_text) @@ plainto_tsquery('simple', %s)" in body[0], body[0]


def test_body_tier_off_until_index_valid(monkeypatch):
    calls = _capture(monkeypatch, index_ready=False)
    res = pgstore.search("hydrogen", limit=10)
    assert res["body_ready"] is False
    assert not any("to_tsvector" in s for s, _ in calls), "body tier ran before index was valid"
    assert any("title ILIKE" in s for s, _ in calls), "metadata tier should still run"


def test_query_text_is_bound_not_formatted(monkeypatch):
    calls = _capture(monkeypatch, index_ready=True)
    pgstore.search("dan'ger; DROP", limit=5)     # quote + semicolon: must never reach SQL text
    for sql, params in calls:
        assert "dan'ger" not in sql, "q was formatted into SQL text: %s" % sql
        assert "%s" in sql
        flat = " ".join(str(p) for p in params)
        assert "dan'ger" in flat, "q not passed as a bound param: %r" % (params,)


def test_empty_query_returns_nothing_and_no_search(monkeypatch):
    calls = _capture(monkeypatch, index_ready=True)
    res = pgstore.search("   ", limit=10)
    assert res["rows"] == []
    assert not calls, "blank query should issue no search"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
