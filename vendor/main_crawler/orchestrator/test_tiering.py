"""Adaptive-cadence self-check — pure sync, throwaway SQLite, no HTTP.

Run:  .venv/Scripts/python.exe test_tiering.py     (from the orchestrator/ dir)
"""
import datetime as dt
import os
import pathlib
import sys

os.environ["DATABASE_URL"] = "sqlite:///./test_tier.db"
_dbfile = pathlib.Path("test_tier.db")
if _dbfile.exists():
    _dbfile.unlink()
sys.path.insert(0, "src")

from mallory_orchestrator import tiering as T  # noqa: E402
from mallory_orchestrator.db import Base, SessionLocal, engine  # noqa: E402
from mallory_orchestrator.models import SiteMeasurement, SiteSchedule, Source  # noqa: E402

Base.metadata.create_all(engine)
NOW = dt.datetime(2026, 7, 20, 12, 0, 0)


def _reset(db):
    for m in (SiteMeasurement, SiteSchedule, Source):
        db.query(m).delete()
    db.commit()


def _pol(db, domain, **kw):
    d = dict(domain=domain, source_id=domain.upper(), phase="steady", tier="weekly",
             tier_reason="bootstrap", defence_score=0.0, measurements=0, updated_at=NOW)
    d.update(kw)
    p = SiteSchedule(**d)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _src(db, domain, category="trade_press"):
    s = Source(domain=domain, source_id=domain.upper(), category=category, tier=1,
               frequency="daily", created_at=NOW)
    db.add(s)
    db.commit()
    return s


# ── priority ordering ────────────────────────────────────────────────────────

def t_priority_defence_first():
    """Higher defence_score must sort FIRST — the operator's #1 requirement."""
    hi = SiteSchedule(domain="a.com", source_id="A", phase="steady", tier="weekly",
                      defence_score=0.9, last_success_at=NOW)
    lo = SiteSchedule(domain="b.com", source_id="B", phase="steady", tier="weekly",
                      defence_score=0.1, last_success_at=NOW)
    keys = sorted([("b.com", lo), ("a.com", hi)],
                  key=lambda t: T.priority_key(t[0], t[1], None, NOW))
    assert keys[0][0] == "a.com", keys


def t_baseline_outranks_steady():
    """An un-crawled site (census) outranks everything steady-state, even higher defence."""
    base = SiteSchedule(domain="new.com", source_id="N", phase="baseline", tier="weekly",
                        defence_score=0.0)
    steady = SiteSchedule(domain="old.com", source_id="O", phase="steady", tier="weekly",
                          defence_score=1.0, last_success_at=NOW)
    order = sorted([("old.com", steady), ("new.com", base)],
                   key=lambda t: T.priority_key(t[0], t[1], None, NOW))
    assert order[0][0] == "new.com", order


# ── measurement ──────────────────────────────────────────────────────────────

def t_lambda_denominator_excludes_gate_drops():
    """REGRESSION: the crawler only counts skipped_unchanged for pages that PASS the keyword gate,
    so `fetched` is the wrong denominator — gate-drops must not read as 'unchanged'."""
    with SessionLocal() as db:
        _reset(db)
        p = _pol(db, "x.com", phase="steady", last_success_at=NOW - dt.timedelta(days=1))
        T.observe(db, p, {"fetched": 100, "kept": 2, "skipped_unchanged": 8,
                          "skipped_duplicate": 0, "not_modified_304": 0,
                          "dropped_by_gate": 90}, NOW, 10.0)
        db.commit()
        # checked = 2+8 = 10  →  change_rate 0.2, NOT 2/100 = 0.02
        assert abs(p.change_rate_ewma - 0.2) < 1e-6, p.change_rate_ewma


def t_lambda_is_per_day_not_per_run():
    """The same page-count over a longer gap must yield a LOWER rate, else tiering is
    self-reinforcing (a monthly site would always look busy and ratchet back to daily)."""
    with SessionLocal() as db:
        _reset(db)
        a = _pol(db, "a.com", phase="steady", last_success_at=NOW - dt.timedelta(days=1))
        b = _pol(db, "b.com", phase="steady", last_success_at=NOW - dt.timedelta(days=5))
        summ = {"fetched": 10, "kept": 5, "skipped_unchanged": 5, "skipped_duplicate": 0,
                "not_modified_304": 0, "dropped_by_gate": 0}
        T.observe(db, a, dict(summ), NOW, 5.0)
        T.observe(db, b, dict(summ), NOW, 5.0)
        db.commit()
        assert abs(a.lambda_ewma - 5.0) < 1e-6, a.lambda_ewma      # 5 changes / 1 day
        assert abs(b.lambda_ewma - 1.0) < 1e-6, b.lambda_ewma      # 5 changes / 5 days


def t_baseline_advances_to_probe_without_measuring():
    """A baseline crawl measures EXISTENCE, not change — every page is new, so it must not
    produce a λ."""
    with SessionLocal() as db:
        _reset(db)
        _src(db, "n.com")
        p = _pol(db, "n.com", phase="baseline")
        T.observe(db, p, {"fetched": 50, "kept": 50}, NOW, 20.0)
        db.commit()
        assert p.phase == "probe", p.phase
        assert p.lambda_ewma is None, p.lambda_ewma
        assert p.tier in ("daily", "weekly"), p.tier          # never bootstraps to monthly


# ── tier decision ────────────────────────────────────────────────────────────

def t_hysteresis_no_flap():
    """λ just past a boundary must NOT move the tier (dead band + 2 votes + dwell)."""
    with SessionLocal() as db:
        _reset(db)
        src = _src(db, "h.com")
        p = _pol(db, "h.com", phase="steady", tier="weekly", measurements=5,
                 lambda_ewma=0.40, tier_changed_at=NOW - dt.timedelta(days=60))
        tier, _ = T.retier(db, p, src, NOW)      # 0.40 is inside 0.38*1.5 dead band
        assert tier == "weekly", tier


def t_tier_moves_on_strong_evidence_after_two_votes():
    with SessionLocal() as db:
        _reset(db)
        src = _src(db, "d.com")
        p = _pol(db, "d.com", phase="steady", tier="weekly", measurements=5,
                 lambda_ewma=5.0, tier_changed_at=NOW - dt.timedelta(days=60))
        t1, _ = T.retier(db, p, src, NOW)
        assert t1 == "weekly", "first strong reading only votes"
        t2, _ = T.retier(db, p, src, NOW)
        assert t2 == "daily", t2


def t_defence_floor_blocks_monthly():
    """A high-defence site is never allowed to decay to monthly — a missed tender is catastrophic,
    a wasted crawl costs minutes."""
    with SessionLocal() as db:
        _reset(db)
        src = _src(db, "f.com", category="tender_portal")
        p = _pol(db, "f.com", phase="steady", tier="monthly", measurements=5,
                 lambda_ewma=0.0001, defence_score=0.9,
                 tier_changed_at=NOW - dt.timedelta(days=200))
        tier, reason = T.retier(db, p, src, NOW)
        assert tier == "weekly" and reason == "floor", (tier, reason)


# ── load levelling + next-run ────────────────────────────────────────────────

def t_bucket_balances_by_cost_not_count():
    """A new weekly site must land on the cheapest day measured in SECONDS (one careful .gov
    host costs 20-50x a news site, so count-balancing gives wildly uneven days)."""
    with SessionLocal() as db:
        _reset(db)
        # day 0 loaded with one very expensive site; days 1..6 empty
        _src(db, "big.com", category="gov_primary")
        _pol(db, "big.com", tier="weekly", assigned_weekday=0, seconds_ewma=5000.0)
        _src(db, "new.com")
        p = _pol(db, "new.com", tier="weekly", seconds_ewma=100.0)
        T.assign_bucket(db, p)
        db.commit()
        assert p.assigned_weekday != 0, p.assigned_weekday


def t_monthly_dom_never_exceeds_28():
    """29/30/31 silently vanish in February — clamp to 1..28."""
    p = SiteSchedule(domain="m.com", source_id="M", phase="steady", tier="monthly",
                     assigned_dom=31, slot_minute=0)
    nxt = T.next_run_for_policy(p, None, NOW)
    assert nxt is not None and nxt.day <= 28, nxt


def t_baseline_fires_immediately():
    p = SiteSchedule(domain="b2.com", source_id="B2", phase="baseline", tier="weekly")
    assert T.next_run_for_policy(p, None, NOW) == NOW


# ── forecast ─────────────────────────────────────────────────────────────────

def t_forecast_daily_site_on_every_day():
    with SessionLocal() as db:
        _reset(db)
        _src(db, "dly.com")
        _pol(db, "dly.com", tier="daily", seconds_ewma=300.0, pages_ewma=100.0)
        f = T.forecast(db, window_hours=8.0)
        per = f["steady_state"]["per_weekday"]
        assert len(per) == 7 and all(d["sites"] >= 1 for d in per), per
        assert f["sizing"]["required_parallel_workers"] >= 1
        assert f["by_tier"]["daily"] == 1, f["by_tier"]


def main():
    tests = [t_priority_defence_first, t_baseline_outranks_steady,
             t_lambda_denominator_excludes_gate_drops, t_lambda_is_per_day_not_per_run,
             t_baseline_advances_to_probe_without_measuring,
             t_hysteresis_no_flap, t_tier_moves_on_strong_evidence_after_two_votes,
             t_defence_floor_blocks_monthly,
             t_bucket_balances_by_cost_not_count, t_monthly_dom_never_exceeds_28,
             t_baseline_fires_immediately, t_forecast_daily_site_on_every_day]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"\nALL {len(tests)} TIERING TESTS PASSED")


if __name__ == "__main__":
    main()
