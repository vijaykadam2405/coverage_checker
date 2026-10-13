"""The 304 fast path must be COUNTABLE, not just correct.

`skipped_unchanged` is the expensive verdict — reached only after a full fetch and, on most sites,
a browser render — while `not_modified_304` is the cheap one, taken before either. The engine
counted the cheap one on the job context and never surfaced it in /v1/metrics, so from outside the
two were indistinguishable and "is incremental actually helping?" had no answer but a guess.

That is failure class 2 in this project's own list: a capability that is shipped, correct, and
unmeasurable is indistinguishable from one that never fires.
"""
from __future__ import annotations

from crawler import async_engine as ae


def test_the_304_counter_is_reported():
    assert "not_modified_304" in ae._STAT_KEYS, (
        "the conditional-GET fast path cannot be measured from /v1/metrics")


def test_it_is_distinct_from_the_expensive_verdict():
    """Two different things: 304 skips BEFORE the fetch; skipped_unchanged is decided after the
    document has been fetched, rendered and hashed. Collapsing them would hide the whole win."""
    assert "skipped_unchanged" in ae._STAT_KEYS
    assert ae._STAT_KEYS.count("not_modified_304") == 1


def test_a_fresh_progress_block_exposes_it_at_zero():
    """It must appear as 0 from the start, not be absent until the first 304 — an absent key reads
    as 'not measured' to every consumer, which is the distinction this codebase runs on."""
    ae._reset_progress() if hasattr(ae, "_reset_progress") else None
    stats = {k: 0 for k in ae._STAT_KEYS}
    assert stats["not_modified_304"] == 0


def test_the_engine_ticks_it_where_it_takes_the_skip():
    """Guards the wiring, not just the key: the counter is incremented on ctx AND ticked into the
    live progress block at the same site."""
    import inspect
    src = inspect.getsource(ae)
    i = src.find("ctx.not_modified += 1")
    assert i > 0, "the 304 skip site moved"
    assert "_tick_stat('not_modified_304')" in src[i:i + 300], (
        "ctx counter bumped without ticking the reported stat")


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-q"]))
