"""The render lane must not admit more workers than there are render permits.

MEASURED LIVE, and it is the answer to "why is the re-crawl 2-3 pages/s":

    fetched 4,075 · deferred_render 4 · deflected_render 140,158

Thirty-four bounces per fetch, for FOUR pages that actually needed a browser. One instance sat at
render_q=6,100 with active=4 and 32 tabs idle.

Cause: two limits that were never reconciled. `_render_lane_cap` (128) is a WORKER-MIX limit — how
many workers may do render work so the httpx reserve stays non-empty. `adaptive.limit` (4 under
load) is how many renders may actually RUN. Admission read only the first, so 124 workers pulled
render items they could never get a permit for, raised RenderBusy, and requeued on a flat 3s timer
forever. The spin is self-feeding: the CPU it burns holds the governor at its floor.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae


class _Gate:
    def __init__(self, limit):
        self.limit = limit


class _Eng:
    """Only what _render_admit_cap touches."""
    def __init__(self, lane_cap, limit):
        self._render_lane_cap = lane_cap
        self.adaptive = _Gate(limit) if limit is not None else None

    _render_admit_cap = ae.AsyncEngine._render_admit_cap


def test_the_governor_limit_wins_when_it_is_lower():
    """THE fix. 128-wide lane, 4 permits -> admit 4, not 128."""
    assert _Eng(128, 4)._render_admit_cap() == 4


def test_the_lane_cap_wins_when_IT_is_lower():
    """The governor must not be able to push render work past the httpx reserve, or a big render
    site starves the cheap lane — the opposite failure, also already seen here."""
    assert _Eng(4, 128)._render_admit_cap() == 4


def test_it_never_returns_zero():
    """A zero admit cap starves the render lane outright: a render-only .gov site crawls exactly
    one page. That regression is on the record too, so the floor is not optional."""
    assert _Eng(128, 0)._render_admit_cap() == 128     # 0/negative = governor not usable
    assert _Eng(0, 0)._render_admit_cap() >= 0
    assert _Eng(128, -5)._render_admit_cap() == 128


def test_a_missing_governor_falls_back_to_the_lane_cap():
    """CRAWLER_ADAPTIVE_CONCURRENCY=0 leaves no usable limit; behaviour must be exactly as before."""
    assert _Eng(64, None)._render_admit_cap() == 64


@pytest.mark.parametrize("cap,lim,want", [(128, 1, 1), (128, 32, 32), (12, 32, 12), (1, 1, 1)])
def test_it_is_the_min_of_the_two(cap, lim, want):
    assert _Eng(cap, lim)._render_admit_cap() == want


def test_every_admission_gate_uses_it():
    """Guards the wiring. Three gates in _next_item decide whether to pull a render item; one left
    on the raw lane cap reopens the livelock on its own."""
    import inspect
    src = inspect.getsource(ae.AsyncEngine._next_item)
    assert "_render_lane_used < self._render_lane_cap" not in src, (
        "an admission gate still reads the raw lane cap")
    assert src.count("_render_admit_cap()") >= 3


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
