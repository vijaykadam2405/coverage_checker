"""The DOM-settle cap must bound WALL TIME, and the probe must not serialise the document.

MEASURED on live news pages. The loop was:

    while waited < cap_ms:
        size = await tab.evaluate("document.documentElement.outerHTML.length")
        await tab.wait_for_timeout(step_ms)
        waited += step_ms

`waited` counted only the naps, so the cap bounded sleep rather than elapsed time — a cap set to
4000ms produced 15.5 SECONDS of real time, and the shipped 12s cap routinely cost 20-25s. And the
probe built a string of the entire DOM every 300ms, on pages whose whole problem is being large,
purely to measure its length. Every rendered page in the corpus paid both.
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from crawler import async_engine as ae


class _Tab:
    """A tab whose DOM never settles and whose probe is slow — the shape that exposed the bug."""

    def __init__(self, probe_delay=0.25):
        self.probe_delay, self.n, self.probes = probe_delay, 0, 0

    async def evaluate(self, _js):
        self.probes += 1
        await asyncio.sleep(self.probe_delay)      # a real probe on a big DOM is not free
        self.n += 100                              # always growing → never settles
        return self.n

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(ms / 1000.0)


def test_the_cap_bounds_REAL_time_not_accumulated_sleep():
    tab = _Tab(probe_delay=0.25)
    t0 = time.monotonic()
    still = asyncio.run(
        ae._wait_dom_settle(tab, cap_ms=1000))
    took = time.monotonic() - t0
    assert still is True, "a never-settling DOM must report that it hit the cap"
    assert took < 2.0, (
        "cap was 1.0s and it took %.1fs — the cap is counting sleep, not elapsed time" % took)


def test_a_settling_page_returns_early():
    class _Stable(_Tab):
        async def evaluate(self, _js):
            self.probes += 1
            return 500                              # never changes → settles immediately
    tab = _Stable(probe_delay=0.0)
    still = asyncio.run(
        ae._wait_dom_settle(tab, cap_ms=10000))
    assert still is False
    assert tab.probes <= 4, "a stable page should exit after a couple of samples"


def test_the_probe_does_not_serialise_the_dom():
    """Checks the CODE, not the prose — the comment above the fix names the old probe on purpose."""
    src = inspect.getsource(ae._wait_dom_settle)
    probes = [l for l in src.splitlines() if "tab.evaluate(" in l]
    assert probes, "the settle probe moved"
    joined = " ".join(probes)
    assert "outerHTML" not in joined, (
        "outerHTML.length builds a string of the whole document on every sample: %r" % joined)
    assert "getElementsByTagName" in joined


def test_a_zero_cap_is_a_no_op():
    tab = _Tab()
    out = asyncio.run(
        ae._wait_dom_settle(tab, cap_ms=0))
    assert out is False and tab.probes == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
