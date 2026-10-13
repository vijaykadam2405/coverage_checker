"""The sitemap check is a DIAGNOSTIC, and a diagnostic that misreports is worse than none.

The operator's rule stands: the sitemap cross-checks the crawl, it never seeds it. (Seeding
lockheedmartin.com would have pulled in 27 per-locale sitemaps — en-us, en-au, en-gb, en-il, he-il —
i.e. the same pages in six languages, straight into a corpus that had just had 48,278 duplicate URL
records removed.)

But as written the check reported ONE percentage for four unrelated situations, all measured on the
live corpus across the 60 largest sites:

  lockheedmartin.com  2,263 listed,   998 held,   1,457 missing  -> a real gap
  rtx.com               683 listed, 2,018 held                   -> we hold 3x its index
  leonardo.com        3,723 listed, 2,869 held,  563 in common    -> two URL spaces, one site
  (no sitemap)            0 listed                                -> nothing measured at all

Reading "32%" off rtx.com and treating it as a third of a site missing is the confusion this file
exists to prevent.
"""
from __future__ import annotations

import pytest

from crawler import sitemap


def _cov(monkeypatch, listed, held):
    """coverage() with the network stubbed — this is about the verdict, not about fetching."""
    monkeypatch.setattr(sitemap, "fetch_sitemap_urls", lambda h, max_urls=50000: set(listed))
    return sitemap.coverage("x.example", set(held))


def test_a_real_gap_is_called_a_gap(monkeypatch):
    """lockheedmartin.com in shape: they list far more than we hold, and we hold nothing they don't.
    Verified URL-by-URL — the missing ones are /capabilities/electronic-warfare/... pages."""
    listed = ["x.example/p%d" % i for i in range(100)]
    got = _cov(monkeypatch, listed, listed[:36])
    assert got["verdict"] == "gaps"
    assert got["coverage_pct"] == 36
    assert len(got["missed"]) == 64


def test_a_partial_sitemap_is_not_blamed_on_the_crawl(monkeypatch):
    """rtx.com: 683 listed, 2,018 held. Reporting 32% here says the crawl missed two thirds of a
    site we have three times over."""
    listed = ["x.example/p%d" % i for i in range(683)]
    held = listed[:284] + ["x.example/other%d" % i for i in range(1734)]
    got = _cov(monkeypatch, listed, held)
    assert got["verdict"] == "sitemap_partial"
    assert got["extra"] == 1734


def test_two_url_spaces_for_one_site_are_flagged_not_counted(monkeypatch):
    """leonardo.com: /en/about/history in the sitemap, /EN/focus-detail/-/detail/... in our history.
    Case-insensitive comparison does NOT rescue it (measured: 563 overlap either way), because the
    paths genuinely differ. This needs a human to say which is canonical."""
    listed = ["x.example/en/p%d" % i for i in range(3723)]
    held = listed[:563] + ["x.example/EN/focus-detail/-/detail/p%d" % i for i in range(2306)]
    got = _cov(monkeypatch, listed, held)
    assert got["verdict"] == "different_urls"


def test_no_sitemap_is_never_reported_as_zero_percent(monkeypatch):
    """THE dashboard rule: "not measured" must not look like "measured, and it is zero". 18 of the
    60 largest sites publish no usable sitemap at all."""
    got = _cov(monkeypatch, [], ["x.example/a", "x.example/b"])
    assert got["verdict"] == "no_sitemap"
    assert got["coverage_pct"] is None, "an unmeasured site rendered as 0%"


def test_a_fully_covered_site_says_so(monkeypatch):
    listed = ["x.example/p%d" % i for i in range(100)]
    got = _cov(monkeypatch, listed, listed[:95])
    assert got["verdict"] == "covered"


def test_the_check_never_feeds_the_crawler():
    """The operator's standing rule. If this module is ever wired into the frontier, a per-locale
    sitemap becomes a duplicate-page firehose."""
    import inspect

    from crawler import async_engine
    src = inspect.getsource(async_engine)
    assert "fetch_sitemap_urls" not in src, "the engine is seeding from sitemaps"
    assert "import sitemap" not in src and "from . import sitemap" not in src


def test_which_sitemap_was_compared_is_recoverable():
    """lockheedmartin declares 27 and the check compares against the first that yields URLs. That
    default is right — comparing against all 27 would report the other locales as missing — but it
    has to be visible, or the number is unexplainable."""
    assert hasattr(sitemap, "declared_sitemaps")


def test_only_one_sitemap_is_ever_compared():
    """The guardrail that makes gap-filling safe: lockheedmartin declares 28 sitemaps, one per
    locale. fetch_sitemap_urls stops at the FIRST that yields URLs, so the "missing" set — and
    therefore any seed list built from it — can only ever hold that one locale's pages."""
    import pathlib
    src = pathlib.Path(sitemap.__file__).read_text(encoding="utf-8")
    body = src.split("def fetch_sitemap_urls")[1].split("\ndef ")[0]
    assert "first sitemap" in body and "return urls" in body


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
