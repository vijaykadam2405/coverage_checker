"""A cadence setting that one phase ignores is not a cadence setting.

MEASURED 2026-08-11: 655 of 669 scheduled jobs were due at the same instant, so every 20s tick
dispatched another 25 of them, permanently. The cause was not the tier — 594 policies were already
`weekly`. It was `next_run_for_policy` returning `now` for the `baseline` phase, which 525 of 760
policies were in. No cadence setting could have fixed that, because baseline never consulted one.

The flood took five crawler instances away from a 217k-URL resumed backlog within seconds of the
scheduler being armed.

The fix must hold TWO properties that pull in opposite directions, which is why this file exists:
  * baseline is PACED — it fires in its assigned slot, so the census spreads across the week;
  * baseline still has PRECEDENCE — `priority_key` sorts it ahead of everything else, so within
    whatever fires on a given day the census still goes first.
Losing the second while fixing the first would silently deprioritise every never-crawled site.

Runnable directly:  python tests/test_scheduling_is_spread_not_a_flood.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import tiering  # noqa: E402

NOW = dt.datetime(2026, 8, 11, 15, 0, 0)          # a Tuesday


class _Pol:
    """The slice of SiteSchedule next_run_for_policy and priority_key read."""

    def __init__(self, phase="baseline", tier="weekly", weekday=0, minute=540, dom=1, score=0.0):
        self.phase, self.tier = phase, tier
        self.assigned_weekday, self.assigned_dom, self.slot_minute = weekday, dom, minute
        self.defence_score, self.last_success_at = score, None
        self.domain = "example.com"
        # priority_key also reaches for the cost estimate; identical on both sides of the
        # comparisons below, so it never decides them.
        self.seconds_ewma = 60.0
        self.pages_ewma = 100.0
        self.measurements = 5


def t_a_baseline_site_is_paced_not_fired_immediately() -> None:
    """THE BUG. `now` here is what made 525 sites due simultaneously."""
    nxt = tiering.next_run_for_policy(_Pol(phase="baseline", weekday=4), None, NOW)
    assert nxt is not None and nxt > NOW, "a baseline job fired immediately — this is the flood"
    assert nxt.weekday() == 4, f"must land in its ASSIGNED slot, got weekday {nxt.weekday()}"
    assert (nxt - NOW) <= dt.timedelta(days=7), "paced must still mean within the week"


def t_baseline_keeps_its_precedence() -> None:
    """Pacing must not cost the census its place in the queue. Without this, the fix above would
    quietly push every never-crawled site behind every re-crawl."""
    base = tiering.priority_key("a.com", _Pol(phase="baseline"), None, NOW)
    steady = tiering.priority_key("a.com", _Pol(phase="steady"), None, NOW)
    assert base < steady, "baseline must still sort ahead of steady on the same day"


def t_the_week_is_actually_spread() -> None:
    """Seven distinct assigned weekdays must produce seven distinct firing days — the property the
    whole change exists for. A single default weekday would pass every test above and still flood
    one day a week."""
    days = {tiering.next_run_for_policy(_Pol(phase="baseline", weekday=w), None, NOW).weekday()
            for w in range(7)}
    assert days == set(range(7)), f"assigned weekdays did not spread: {sorted(days)}"


def t_a_paused_site_still_never_fires() -> None:
    assert tiering.next_run_for_policy(_Pol(phase="paused"), None, NOW) is None


def t_steady_and_probe_are_unchanged() -> None:
    """Negative control: the fix must touch ONLY the baseline shortcut."""
    for phase in ("steady", "probe"):
        nxt = tiering.next_run_for_policy(_Pol(phase=phase, weekday=2), None, NOW)
        assert nxt is not None and nxt.weekday() == 2, (phase, nxt)


def t_monthly_lands_on_its_day_of_month() -> None:
    nxt = tiering.next_run_for_policy(_Pol(phase="baseline", tier="monthly", dom=17), None, NOW)
    assert nxt.day == 17 and nxt > NOW, nxt
    assert nxt.day <= tiering.MONTHLY_DOM_MAX, "29/30/31 vanish in February"


def t_no_policy_still_means_weekly_not_now() -> None:
    """A job whose source_id does not resolve must not become a permanent every-tick dispatch."""
    nxt = tiering.next_run_for_policy(None, None, NOW)
    assert nxt is not None and nxt > NOW and (nxt - NOW) <= dt.timedelta(days=7), nxt


def t_nothing_fires_in_the_past() -> None:
    """The flood's signature was next_run <= now on 655 rows. Whatever the slot, the answer must
    always be in the future, or the scheduler re-dispatches the same job every 20 seconds."""
    for phase in ("baseline", "probe", "steady"):
        for tier, kw in (("weekly", {"weekday": 0}), ("monthly", {"dom": 1})):
            for minute in (0, 539, 540, 541, 1439):
                p = _Pol(phase=phase, tier=tier, minute=minute, **kw)
                nxt = tiering.next_run_for_policy(p, None, NOW)
                assert nxt > NOW, f"{phase}/{tier}/minute={minute} scheduled in the past: {nxt}"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} SCHEDULING-SPREAD TESTS PASSED")


def test_scheduling_spread_suite() -> None:
    main()


if __name__ == "__main__":
    main()
