"""Incremental recrawl (ETag/304) — the depth gate + conditional-header round-trip.

The 304 unchanged-skip must NEVER fire on the shallow discovery surface (seeds + top listing layer),
or a new article behind an unchanged-LOOKING hub is never re-discovered (the skip returns before
link-enqueue). Deep leaf pages still skip. See async_engine._incremental_skip_eligible."""
from crawler.async_engine import _incremental_skip_eligible
from crawler.dedup import CrawlHistory


# --- depth gate -----------------------------------------------------------
def test_incremental_off_never_skips():
    # flag off → no page is ever 304-skipped, regardless of depth
    assert not _incremental_skip_eligible(False, 5, 1)
    assert not _incremental_skip_eligible(False, 0, 1)


def test_fresh_depth_keeps_discovery_surface():
    # default fresh_depth=1: seeds (0) and top layer (1) always re-parse; depth>=2 may skip
    assert not _incremental_skip_eligible(True, 0, 1)   # seed — always fresh
    assert not _incremental_skip_eligible(True, 1, 1)   # category hub — always fresh
    assert _incremental_skip_eligible(True, 2, 1)       # article — eligible for 304-skip
    assert _incremental_skip_eligible(True, 9, 1)


def test_fresh_depth_zero_only_seeds_fresh():
    # flat sites (articles at depth 1) → fresh_depth=0 lets depth-1 leaves skip, seeds stay fresh
    assert not _incremental_skip_eligible(True, 0, 0)
    assert _incremental_skip_eligible(True, 1, 0)


# --- conditional-header round-trip ---------------------------------------
def test_conditional_headers_round_trip():
    h = CrawlHistory(":memory:")
    h.upsert("https://x.test/a", content_hash="abc",
             etag='"strong-123"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
             status=200, fetched_at="2025-01-01T00:00:00Z")
    cond = h.conditional_headers("https://x.test/a")
    assert cond["If-None-Match"] == '"strong-123"'
    assert cond["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    # a URL never seen → nothing to be conditional about
    assert h.conditional_headers("https://x.test/unseen") == {}
    h.close()
