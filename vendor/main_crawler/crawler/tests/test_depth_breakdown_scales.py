"""The depth drill-down must not issue one query per depth level.

MEASURED on the live dashboard. al-monitor.com holds 80,738 pages and the rollup over them is
fast — 1.5s. But it returns 1,820 DISTINCT DEPTHS, because the crawler had followed a chain 1,826
links deep, and the old code then ran a sample query for EACH one. The drill-down fired 1,820
queries and the dialog rendered:

    could not read: timed out
    no depth data
    total 0 page(s)

over a site holding 80k pages. The per-depth loop was fine while sites had eight depths and quietly
quadratic when one did not, and nothing about a page COUNT hints at 1,820 levels — which is exactly
the blindness the depth panel exists to fix.
"""
from __future__ import annotations

import pytest

from ingest_api import pgstore


def _install(monkeypatch, n_depths: int, pages_per_depth: int = 3):
    """A source with n_depths distinct depths. Records every query issued."""
    calls: list[str] = []
    rollup = [(d, pages_per_depth, 100 * pages_per_depth, 900 * pages_per_depth)
              for d in range(n_depths)]

    def fake_query(sql, params=None):
        calls.append(" ".join(sql.split()))
        if "GROUP BY depth" in sql:
            return rollup
        # the windowed sample query: (depth, url, title, fetched_at, text_chars, document_id, pdfs)
        wanted = params[1] if params and len(params) > 1 else []
        return [(d, "https://h.test/d%d/p%d" % (d, i), "t", None, 10, "doc%d-%d" % (d, i), 0)
                for d in (wanted or []) for i in range(pages_per_depth)]

    monkeypatch.setattr(pgstore, "query", fake_query)
    return calls


def test_one_sample_query_no_matter_how_many_depths(monkeypatch):
    calls = _install(monkeypatch, n_depths=1820)
    out = pgstore.depth_breakdown("al-monitor.com")
    assert out["distinct_depths"] == 1820
    assert len(calls) <= 2, (
        "issued %d queries for 1,820 depths — this is the timeout, one query per depth" % len(calls))


def test_query_count_does_not_grow_with_depth_count(monkeypatch):
    """The property, stated directly: 8 depths and 1,820 depths must cost the same."""
    def run(n):
        calls = _install(monkeypatch, n_depths=n)
        pgstore.depth_breakdown("x.test")
        return len(calls)

    small, big = run(8), run(1820)
    assert small == big, "query count scales with depth count: %d vs %d" % (small, big)


def test_every_depth_still_reports_its_count(monkeypatch):
    """Capping the SAMPLING must not cap the data — the count per depth is what the panel is for."""
    _install(monkeypatch, n_depths=200, pages_per_depth=5)
    out = pgstore.depth_breakdown("big.test")
    assert len(out["depths"]) == 200
    assert out["total_pages"] == 1000
    assert all("pages" in d for d in out["depths"])


def test_a_ONE_PAGE_depth_still_gets_its_examples(monkeypatch):
    """The regression that reached the live dashboard. Sampling was capped to the busiest 40
    depths, so d0 — which holds exactly ONE page, the seed — fell outside it and rendered
    "no examples" over a site with 80k pages.

    The panel exists so the operator can reach the page links (html, text, PDFs), so a depth with
    no examples is a dead end in the UI. And the small depths are often the most interesting: d0 is
    the seed, d1 the top nav."""
    rollup = [(0, 1, 939, 202_000), (1, 121, 268_000, 12_000_000), (9, 9000, 1, 1)]

    def fake_query(sql, params=None):
        if "GROUP BY depth" in sql:
            return rollup
        wanted = params[1]
        assert 0 in wanted, "the 1-page seed depth was not sampled"
        assert 1 in wanted and 9 in wanted
        return [(d, "https://h.test/d%s" % d, "t", None, 10, "doc%s" % d, 0) for d in wanted]

    monkeypatch.setattr(pgstore, "query", fake_query)
    out = pgstore.depth_breakdown("h.test")
    by = {d["depth"]: d for d in out["depths"]}
    assert by[0]["sample"], "d0 holds 1 page and must still show it"
    assert by[1]["sample"] and by[9]["sample"]
    assert out["sampled_depths"] == 3, "every depth must be sampled, not a top-N subset"


def test_every_depth_is_sampled_even_at_1820_levels(monkeypatch):
    """Sampling all of them costs rows, not round trips — and it is the round trips that timed
    out. This must stay ONE query."""
    calls = _install(monkeypatch, n_depths=1820)
    out = pgstore.depth_breakdown("al-monitor.com")
    assert out["sampled_depths"] == 1820
    assert len(calls) <= 2, "sampling every depth must not cost a query per depth"


def test_a_sample_never_repeats_a_url(monkeypatch):
    """documents holds revisions, so one url can appear twice at a depth — mhi.com showed
    https://mhi.com/ twice at d0."""
    def fake_query(sql, params=None):
        if "GROUP BY depth" in sql:
            return [(0, 2, 10, 90)]
        return [(0, "https://mhi.com/", "t", None, 10, "rev2", 0),
                (0, "https://mhi.com/", "t", None, 9, "rev1", 0)]

    monkeypatch.setattr(pgstore, "query", fake_query)
    samp = pgstore.depth_breakdown("mhi.com")["depths"][0]["sample"]
    assert [s["url"] for s in samp] == ["https://mhi.com/"]


def test_it_still_uses_text_len_not_length_of_main_text():
    """main_text is TOASTed: length() on it detoasts every page just to sort. 203s vs a fraction of
    that on one 42k-page site, and the same mistake once made documents_current detoast 8 GB."""
    import inspect

    src = inspect.getsource(pgstore.depth_breakdown)
    # The docstring NAMES length(main_text) on purpose, as the warning. Only executable lines are
    # the subject — the first version of this assertion failed on that very warning.
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines() if not l.strip().startswith("#"))
    code = code.split('"""')[0] + '"""'.join(code.split('"""')[2:])
    assert "length(main_text)" not in code
    assert "text_len" in code


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
