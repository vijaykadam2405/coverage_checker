"""A stop must outlive its teardown — including in the SCHEDULER TICK.

Measured live on 2026-08-02: stop set halted=True, 12/12 instances acked, every batch ended — and
twenty seconds later this tick dispatched a fresh site to every one of them. The crawler arms
itself on each incoming batch (clear_stop on the way in), so `stopping` read False and the fleet
looked like it had never stopped at all. Ten minutes of teardown-plus-fresh-dispatch took the box
to load 292 on 40 cores while every switch read exactly as the operator had set it.

The identical fault is recorded twice already (auto-refill resuming a halted crawl; the manual
batch loop starting its next chunk after the master switch went off). Both were fixed at the site
they were found. This is the third, and these tests exist so there is not a fourth.
"""
from __future__ import annotations

import pytest

from mallory_orchestrator import scheduler


class _DB:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def spy(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(scheduler, "run_due", lambda db, **k: calls.append("dispatched"))
    return calls


def _switches(monkeypatch, *, halted=False, maintenance=False, master=True):
    monkeypatch.setattr(scheduler, "halted", lambda db: halted)
    monkeypatch.setattr(scheduler, "maintenance", lambda db: maintenance)
    monkeypatch.setattr(scheduler, "master_enabled", lambda db: master)
    # The `scheduled` toggle (test_control_plane.py): ON here, these cases are about the halt.
    monkeypatch.setattr(scheduler, "auto_on", lambda db, name: True)


def test_a_halted_fleet_is_not_dispatched_to(spy, monkeypatch):
    """THE bug. halted=True was invisible to this tick, so Stop dispatched again 20s later."""
    _switches(monkeypatch, halted=True)
    scheduler._tick_once()
    assert spy == [], "an operator halt must stop the scheduler, not just the batch in flight"


def test_maintenance_mode_is_not_dispatched_to(spy, monkeypatch):
    """Maintenance means 'hold still while I move things' — frontier moves need the fleet quiet."""
    _switches(monkeypatch, maintenance=True)
    scheduler._tick_once()
    assert spy == []


def test_master_off_is_not_dispatched_to(spy, monkeypatch):
    """The one switch that already worked — kept so a refactor cannot drop it."""
    _switches(monkeypatch, master=False)
    scheduler._tick_once()
    assert spy == []


def test_all_clear_still_dispatches(spy, monkeypatch):
    """The guard must not be so eager that a healthy fleet never gets work."""
    _switches(monkeypatch)
    scheduler._tick_once()
    assert spy == ["dispatched"]


@pytest.mark.parametrize("h,m,ma", [(True, True, True), (True, False, False), (False, True, False)])
def test_any_switch_alone_is_enough_to_hold(spy, monkeypatch, h, m, ma):
    _switches(monkeypatch, halted=h, maintenance=m, master=ma)
    scheduler._tick_once()
    assert spy == []
