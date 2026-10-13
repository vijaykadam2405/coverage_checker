"""A host-blocked item must back OFF, not respin on a flat timer.

MEASURED across the live fleet, and the correlation is the whole argument:

    inst 10  deflected_host 409,311  fetched   247   (1,657 deflections PER fetched page)
    inst  7  deflected_host 357,423  fetched   166
    inst  9  deflected_host 349,655  fetched   151
    inst  5  deflected_host   3,772  fetched 1,471   <- the FASTEST instance
    inst  8  deflected_host      20  fetched 1,143

Deflections track throughput inversely almost perfectly: the re-examination is what eats the CPU
that would otherwise be fetching. Every blocked item was returning on a fixed 3s timer forever,
because the give-up budget deliberately does not fire for a host that is merely slow — so nothing
bounded the spin.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae


def test_the_first_deflect_waits_about_the_base_delay():
    """An item turned away once should come back promptly — the slot may free immediately."""
    d = ae._host_backoff_delay(1)
    assert 2.0 <= d <= 4.5, d


def test_it_grows_with_repeated_deflections():
    a = [ae._host_backoff_delay(n) for n in (1, 2, 3, 4, 5)]
    # jitter means individual samples can cross; compare medians of repeated draws
    med = lambda n: sorted(ae._host_backoff_delay(n) for _ in range(21))[10]
    assert med(1) < med(3) < med(6), [med(1), med(3), med(6)]


def test_it_is_capped():
    """A slot that frees late must still be picked up promptly — unbounded backoff would strand
    work behind a host that recovered."""
    for n in (12, 50, 5000):
        assert ae._host_backoff_delay(n) <= 60.0 * 1.25 + 0.01, n


def test_it_is_jittered():
    """Thousands of items deflected in the same instant would otherwise all return in the same
    instant — the thundering herd this exists to break."""
    vals = {round(ae._host_backoff_delay(4), 4) for _ in range(30)}
    assert len(vals) > 5, "delays are identical — no jitter"


def test_zero_and_negative_are_safe():
    for n in (0, -1):
        d = ae._host_backoff_delay(n)
        assert d > 0 and d <= 60.0 * 1.25


def test_the_deflect_site_uses_it():
    """Guards the wiring: a flat _env_float delay left at the deflect site reopens the spin."""
    import inspect
    src = inspect.getsource(ae)
    i = src.find("item.host_deflects += 1")
    assert i > 0, "the deflect site moved"
    window = src[i:i + 8000]
    assert "_host_backoff_delay(item.host_deflects)" in window
