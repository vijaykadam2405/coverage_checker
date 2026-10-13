"""Adaptive crawl cadence — the "how often should we re-crawl each site?" brain.

Lifecycle per site:  baseline → probe → steady   (paused is a manual/no-signal sink)

  baseline  never crawled. One census crawl, `incremental=False`. Measures EXISTENCE, not change.
  probe     crawled at least once. Cadence is a category-based guess, CLAMPED to ≤ weekly, while we
            gather real change signal. Nothing bootstraps to monthly — a wrong monthly guess means
            60 days blind before the 2nd data point. Measure fast, settle slow.
  steady    ≥2 measurements and ≥10 days of history ⇒ cadence is chosen from measured λ.

Why λ is CHANGES PER DAY (not per run) — the single most important decision here:
a per-run change rate is self-reinforcing. Put a site on monthly and each crawl finds a month of
accumulated changes, so it always looks "very active" and ratchets back to daily; put it on daily
and each crawl finds almost nothing, so it decays to monthly. Only a time-normalised rate
(changes ÷ days since last crawl) is invariant to the cadence you happen to be running.

Why the denominator excludes gate-drops: the crawler only counts `skipped_unchanged` for pages that
PASS the keyword gate (it returns before `_finalize` otherwise). Using `fetched` would score every
gate-dropped page as "unchanged" and systematically under-rate any site with a narrow gate. So
checked = kept + skipped_unchanged + skipped_duplicate + not_modified_304. This also means we
measure *relevant* change: a paper posting 80 sports stories and no defence news correctly reads low.

Thresholds are derived, not guessed. A site producing λ changes/day wants a period P ≈ 1/λ. Snapping
P to {1, 7, 30} days on a LOG scale puts the split points at the geometric midpoints:
    √(1·7)  = 2.65 d → λ = 0.377   (faster than this ⇒ daily)
    √(7·30) = 14.5 d → λ = 0.069   (slower than this ⇒ monthly)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import SiteMeasurement, SiteSchedule, Source

# ── tunables ────────────────────────────────────────────────────────────────
DAILY_MIN_LAMBDA = 0.38        # ≥ ~2.6 changed pages/week ⇒ daily
MONTHLY_MAX_LAMBDA = 0.069     # < ~2 changed pages/month ⇒ monthly
HYSTERESIS = 1.5               # must cross a boundary by 1.5x to even vote to move
VOTES_TO_MOVE = 2              # ...and win 2 consecutive votes
ALPHA_UP, ALPHA_DOWN = 0.5, 0.2  # asymmetric EWMA: react fast to activity, slowly to quiet
COST_CAP_S = 1800.0            # a "daily" site costing more than this becomes sentinel+deep
WEEKLY_SPREAD_DAYS = 7         # 7 buckets not 5: ~30% lower peak-day load = less hardware
MONTHLY_DOM_MAX = 28           # never 29/30/31 — they vanish in February
PROBE_MIN_MEASUREMENTS = 2
PROBE_MIN_DAYS = 10.0
CAREFUL_AFFINITY_PENALTY = 600.0

TIER_SECONDS = {"daily": 86400.0, "weekly": 7 * 86400.0, "monthly": 30 * 86400.0}

# Day-1 guess when there is no data. NOTHING bootstraps to monthly (see module docstring).
BOOTSTRAP_TIER = {
    "trade_press": "daily", "business_press": "daily", "aggregator": "daily",
    "gov_primary": "weekly", "tender_portal": "weekly", "defence_org": "weekly",
    "think_tank": "weekly", "manufacturer_ir": "weekly",
    "blog_forum_social": "weekly", "unknown": "weekly",
    # Mainstream news publishes continuously, so daily - same as the rest of the press. The
    # tiering engine re-tiers from the measured change rate afterwards.
    "general": "daily",
}
# Urgency rank for ordering. NOT Source.tier — that is a trust weight, not relevance.
CAT_RANK = {
    "tender_portal": 0, "gov_primary": 0, "defence_org": 1, "manufacturer_ir": 1,
    "trade_press": 2, "think_tank": 2, "business_press": 3,
    "aggregator": 4, "blog_forum_social": 4, "unknown": 4,
    # Below the defence trade press (2) on purpose: mainstream outlets mention defence
    # occasionally, so when the queue is contended the specialists go first.
    "general": 3,
}
CAT_COST_DEFAULT = {"gov_primary": 1200.0, "tender_portal": 900.0, "defence_org": 400.0,
                    "trade_press": 180.0, "business_press": 180.0, "aggregator": 180.0}
CAT_PAGES_DEFAULT = {"gov_primary": 150.0, "tender_portal": 120.0, "trade_press": 200.0}


def utcnow() -> dt.datetime:
    """Naive UTC — the whole control plane stores naive datetimes (SQLite hands them back naive)."""
    return dt.datetime.utcnow().replace(microsecond=0)


def _stable_hash(s: str) -> int:
    """sha1-based, NOT Python's hash() — the latter is salted per process and would reshuffle
    every site's day bucket on each restart."""
    return int(hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8], 16)


def _ewma(prev: float | None, obs: float, alpha: float) -> float:
    return obs if prev is None else alpha * obs + (1 - alpha) * prev


def _ewma_asym(prev: float | None, obs: float) -> float:
    if prev is None:
        return obs
    return _ewma(prev, obs, ALPHA_UP if obs > prev else ALPHA_DOWN)


def bootstrap_tier(category: str | None) -> str:
    return BOOTSTRAP_TIER.get((category or "unknown"), "weekly")


def est_seconds(pol: SiteSchedule | None, src: Source | None) -> float:
    if pol is not None and pol.seconds_ewma:
        return float(pol.seconds_ewma)
    return CAT_COST_DEFAULT.get((src.category if src else "unknown"), 300.0)


def est_pages(pol: SiteSchedule | None, src: Source | None) -> float:
    if pol is not None and pol.pages_ewma:
        return float(pol.pages_ewma)
    return CAT_PAGES_DEFAULT.get((src.category if src else "unknown"), 80.0)


# ── priority ordering (requirement: defence sites crawl FIRST) ───────────────

def _staleness_ratio(pol: SiteSchedule | None, now: dt.datetime) -> float:
    """How overdue, as a MULTIPLE of the site's own period — so a 2-day-late daily site outranks
    a 5-day-late monthly one. Never crawled ⇒ infinitely overdue."""
    if pol is None or pol.last_success_at is None:
        return float("inf")
    period = TIER_SECONDS.get(pol.tier or "weekly", TIER_SECONDS["weekly"])
    return (now - pol.last_success_at).total_seconds() / period


def priority_key(domain: str | None, pol: SiteSchedule | None, src: Source | None,
                 now: dt.datetime) -> tuple:
    """Sort key used by BOTH the scheduler loop and the Run button. Lower sorts first."""
    ds = float(pol.defence_score) if (pol and pol.defence_score) else 0.0
    stale = _staleness_ratio(pol, now)
    return (
        0 if (pol and pol.phase == "baseline") else 1,   # finish the census first
        -round(ds, 1),                                   # defence first (bucketed to 0.1)
        CAT_RANK.get((src.category if src else "unknown"), 4),
        -(1e9 if math.isinf(stale) else stale),          # most overdue first
        est_seconds(pol, src),                           # then cheapest first (shortest-job-first)
        domain or "",                                    # deterministic tiebreak
    )


# ── measurement ─────────────────────────────────────────────────────────────

def observe(db: Session, pol: SiteSchedule | None, summary: dict,
            now: dt.datetime, duration_s: float) -> None:
    """Fold one completed run into the site's policy. Baseline runs advance the phase but produce
    NO change signal (every page is new by definition)."""
    if pol is None:
        return
    s = summary or {}
    g = lambda k: int(s.get(k) or 0)  # noqa: E731
    fetched = g("fetched")
    kept = g("kept")
    checked = kept + g("skipped_unchanged") + g("skipped_duplicate") + g("not_modified_304")

    interval_days = 1.0
    if pol.last_success_at is not None:
        interval_days = max(0.5, (now - pol.last_success_at).total_seconds() / 86400.0)

    pol.pages_ewma = _ewma(pol.pages_ewma, float(fetched), 0.3)
    # Welford for a real variance → honest p90 forecast band
    prev_mean = pol.seconds_ewma
    if prev_mean is None:
        pol.seconds_ewma, pol.seconds_m2 = float(duration_s), 0.0
    else:
        n = max(2, int(pol.measurements) + 1)
        delta = duration_s - prev_mean
        pol.seconds_ewma = prev_mean + delta / n
        pol.seconds_m2 = float(pol.seconds_m2 or 0.0) + delta * (duration_s - pol.seconds_ewma)
    pol.last_success_at = now
    pol.updated_at = now

    if pol.phase == "baseline":
        pol.baseline_done_at = now
        pol.phase = "probe"
        pol.tier = _clamp_max_weekly(bootstrap_tier(_category_of(db, pol)))
        pol.tier_reason = "bootstrap"
        pol.tier_changed_at = now
        assign_bucket(db, pol)
        return

    if checked == 0:
        # Nothing to compare — a broken job, a blocked host, or an all-gate-dropped crawl.
        pol.consecutive_zero_checked = int(pol.consecutive_zero_checked or 0) + 1
        return
    pol.consecutive_zero_checked = 0

    lam_obs = kept / interval_days
    pol.lambda_ewma = _ewma_asym(pol.lambda_ewma, lam_obs)
    pol.change_rate_ewma = _ewma(pol.change_rate_ewma, kept / max(1, checked), 0.3)
    pol.measurements = int(pol.measurements or 0) + 1
    pol.last_measured_at = now

    db.add(SiteMeasurement(
        domain=pol.domain, job_id=None, at=now, interval_days=interval_days,
        tier_at_run=pol.tier, is_sentinel=bool(pol.sentinel),
        fetched=fetched, kept=kept, skipped_unchanged=g("skipped_unchanged"),
        skipped_duplicate=g("skipped_duplicate"), not_modified_304=g("not_modified_304"),
        dropped_by_gate=g("dropped_by_gate"), errors=g("errors"), recovered=g("recovered"),
        checked=checked, change_rate=kept / max(1, checked),
        changes_per_day=lam_obs, duration_s=float(duration_s),
    ))


def _category_of(db: Session, pol: SiteSchedule) -> str | None:
    src = db.get(Source, pol.domain)
    return src.category if src else None


def _clamp_max_weekly(tier: str) -> str:
    return "weekly" if tier == "monthly" else tier


# ── tier decision ───────────────────────────────────────────────────────────

def _in_dead_band(lam: float, tier: str) -> bool:
    """True while λ sits within HYSTERESIS× of the boundary the current tier lives against —
    i.e. the evidence isn't strong enough to justify moving."""
    if tier == "daily":
        return lam >= DAILY_MIN_LAMBDA / HYSTERESIS
    if tier == "monthly":
        return lam <= MONTHLY_MAX_LAMBDA * HYSTERESIS
    return (lam <= DAILY_MIN_LAMBDA * HYSTERESIS) and (lam >= MONTHLY_MAX_LAMBDA / HYSTERESIS)


def retier(db: Session, pol: SiteSchedule, src: Source | None, now: dt.datetime) -> tuple[str, str]:
    """Decide the cadence tier. Returns (tier, reason)."""
    if int(pol.consecutive_zero_checked or 0) >= 3:
        return "monthly", "no_signal"
    if pol.phase == "probe" or int(pol.measurements or 0) < PROBE_MIN_MEASUREMENTS:
        return _clamp_max_weekly(bootstrap_tier(src.category if src else None)), "bootstrap"

    lam = float(pol.lambda_ewma or 0.0)
    want = "daily" if lam >= DAILY_MIN_LAMBDA else ("monthly" if lam < MONTHLY_MAX_LAMBDA else "weekly")

    if _in_dead_band(lam, pol.tier):
        want = pol.tier
    if pol.tier_changed_at and (now - pol.tier_changed_at).total_seconds() < TIER_SECONDS.get(pol.tier, 0):
        want = pol.tier          # minimum dwell: one full period since the last move

    if want != pol.tier:
        pol.pending_votes = (int(pol.pending_votes or 0) + 1) if pol.pending_tier == want else 1
        pol.pending_tier = want
        if pol.pending_votes < VOTES_TO_MOVE:
            want = pol.tier
    else:
        pol.pending_tier, pol.pending_votes = None, 0

    reason = "measured"
    # Mission floors: a missed tender deadline is catastrophic, a wasted crawl costs minutes.
    ds = float(pol.defence_score or 0.0)
    cat = src.category if src else None
    if want == "monthly" and (ds >= 0.7 or cat == "tender_portal" or (cat == "gov_primary" and ds >= 0.5)):
        want, reason = "weekly", "floor"
    # Cost cap: an expensive site still gets DAILY change detection via a cheap depth-0 sentinel.
    if want == "daily" and est_seconds(pol, src) > COST_CAP_S:
        pol.sentinel = True
        want, reason = "weekly", "cost_cap"
    elif want != "daily":
        pol.sentinel = False
    return want, reason


def advance_phase(db: Session, pol: SiteSchedule, now: dt.datetime) -> None:
    """probe → steady once there is enough history; then re-tier and re-bucket if the tier moved."""
    if pol.phase == "probe":
        old_enough = pol.baseline_done_at and (now - pol.baseline_done_at).total_seconds() >= PROBE_MIN_DAYS * 86400
        if int(pol.measurements or 0) >= PROBE_MIN_MEASUREMENTS and old_enough:
            pol.phase = "steady"
    if pol.phase not in ("probe", "steady"):
        return
    src = db.get(Source, pol.domain)
    new_tier, reason = retier(db, pol, src, now)
    if new_tier != pol.tier:
        pol.tier, pol.tier_reason, pol.tier_changed_at = new_tier, reason, now
        pol.pending_tier, pol.pending_votes = None, 0
        assign_bucket(db, pol)          # tier moved ⇒ new bucket; otherwise the day is STABLE
    else:
        pol.tier_reason = reason
    pol.updated_at = now


# ── load levelling ──────────────────────────────────────────────────────────

def assign_bucket(db: Session, pol: SiteSchedule) -> None:
    """Place the site in the least-loaded day bucket, balancing by ESTIMATED CRAWL SECONDS (not by
    count — a careful .gov host costs 20-50x a news site, so count-balanced buckets can be wildly
    cost-imbalanced, defeating the whole point of forecasting hardware)."""
    pol.slot_minute = _stable_hash(pol.domain) % 1440
    if pol.tier == "daily":
        pol.assigned_weekday, pol.assigned_dom = None, None
        return
    n = WEEKLY_SPREAD_DAYS if pol.tier == "weekly" else MONTHLY_DOM_MAX
    field = "assigned_weekday" if pol.tier == "weekly" else "assigned_dom"
    offset = 0 if pol.tier == "weekly" else 1          # weekday 0..6 | dom 1..28

    load = [0.0] * n
    others = db.scalars(select(SiteSchedule).where(SiteSchedule.tier == pol.tier)).all()
    srcs = {s.domain: s for s in db.scalars(select(Source)).all()}
    for o in others:
        if o.domain == pol.domain:
            continue
        b = getattr(o, field)
        if b is None:
            continue
        idx = b - offset
        if 0 <= idx < n:
            load[idx] += est_seconds(o, srcs.get(o.domain))
            # keep same-parent careful hosts off the same day (they serialise at concurrency 1)
            if o.careful and pol.careful:
                load[idx] += CAREFUL_AFFINITY_PENALTY

    best = min(range(n), key=lambda i: (load[i], (i + _stable_hash(pol.domain)) % n))
    setattr(pol, field, best + offset)
    if pol.tier == "weekly":
        pol.assigned_dom = None
    else:
        pol.assigned_weekday = None


# ── next-run for a tiered job ───────────────────────────────────────────────

def _at_minute(day: dt.datetime, minute: int) -> dt.datetime:
    return day.replace(hour=(minute // 60) % 24, minute=minute % 60, second=0, microsecond=0)


def next_run_for_policy(pol: SiteSchedule | None, prev: dt.datetime | None,
                        now: dt.datetime) -> dt.datetime | None:
    """Tiered scheduling. Deliberately does NOT phase-preserve like the legacy frequencies: for a
    tiered job the assigned day/minute IS the phase, and that is what makes load levelling real."""
    if pol is None:
        return now + dt.timedelta(seconds=TIER_SECONDS["weekly"])
    if pol.phase == "paused":
        return None
    # BASELINE USED TO RETURN `now` — "fire ASAP; priority_key does the ordering". That reasoning
    # holds for a handful of new sites and collapses at census scale: 525 of 760 policies were in
    # baseline, so 655 of 669 scheduled jobs were due AT THE SAME INSTANT and every 20s tick
    # dispatched another 25 of them, permanently. Measured 2026-08-11 — the flood took five crawler
    # instances away from a 217k-URL resumed backlog within seconds of the scheduler being armed,
    # and no cadence setting could stop it because baseline ignored the tier entirely.
    #
    # A baseline site now fires in ITS ASSIGNED SLOT like everything else, so the census is spread
    # across the week instead of arriving at once. It does NOT lose its priority: priority_key still
    # sorts baseline ahead of every other job, so within whatever fires that day the census goes
    # first. Pacing and precedence are different things, and only precedence belonged here.
    t = _at_minute(now, int(pol.slot_minute or 0))
    if pol.tier == "daily":
        return t if t > now else t + dt.timedelta(days=1)
    if pol.tier == "weekly":
        target = int(pol.assigned_weekday if pol.assigned_weekday is not None else 0)
        days = (target - t.weekday()) % 7
        cand = t + dt.timedelta(days=days)
        return cand if cand > now else cand + dt.timedelta(days=7)
    # monthly — days 1..28 only
    target = int(pol.assigned_dom if pol.assigned_dom is not None else 1)
    cand = t.replace(day=min(max(target, 1), MONTHLY_DOM_MAX))
    if cand <= now:
        cand = (cand.replace(day=1) + dt.timedelta(days=32)).replace(
            day=min(max(target, 1), MONTHLY_DOM_MAX))
        cand = _at_minute(cand, int(pol.slot_minute or 0))
    return cand


# ── reconciler (no-Alembic migration path) ──────────────────────────────────

def ensure_site_schedules(db: Session) -> int:
    """Create a policy row for every Source that lacks one. Idempotent; safe on every startup.
    An already-crawled source starts in `steady`, so an existing deployment does NOT redo a census."""
    have = {p.domain for p in db.scalars(select(SiteSchedule)).all()}
    now = utcnow()
    made = 0
    for src in db.scalars(select(Source)).all():
        if src.domain in have:
            continue
        careful = (src.category == "gov_primary") or any(
            src.domain.endswith(sfx) for sfx in (".gov", ".mil", ".gov.uk", ".gov.in", ".gov.my"))
        pol = SiteSchedule(
            domain=src.domain, source_id=src.source_id,
            phase="steady" if src.last_crawled else "baseline",
            baseline_done_at=(src.last_crawled.replace(tzinfo=None) if src.last_crawled else None),
            last_success_at=(src.last_crawled.replace(tzinfo=None) if src.last_crawled else None),
            tier=_clamp_max_weekly(bootstrap_tier(src.category)),
            tier_reason="bootstrap", tier_changed_at=now,
            careful=careful, updated_at=now,
        )
        db.add(pol)
        db.flush()
        assign_bucket(db, pol)
        made += 1
    if made:
        db.commit()
    return made


# ── forecast (what the operator sizes hardware from) ─────────────────────────

def forecast(db: Session, window_hours: float = 8.0) -> dict:
    """Steady-state load per weekday, in CRAWL-SECONDS (not site counts — a site count can't size
    hardware when one .gov host costs 50x a news site). p90 is summed in quadrature because per-site
    variances are near-independent; summing per-site p90s would badly overstate the day."""
    pols = db.scalars(select(SiteSchedule)).all()
    srcs = {s.domain: s for s in db.scalars(select(Source)).all()}

    days = [{"weekday": i, "label": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i],
             "sites": 0, "careful_sites": 0, "pages": 0.0, "seconds": 0.0, "var": 0.0}
            for i in range(7)]
    tiers = {"daily": 0, "weekly": 0, "monthly": 0}
    phases = {"baseline": 0, "probe": 0, "steady": 0, "paused": 0}
    measured = estimated = 0
    monthly_seconds = 0.0

    for p in pols:
        src = srcs.get(p.domain)
        secs, pgs = est_seconds(p, src), est_pages(p, src)
        var = float(p.seconds_m2 or 0.0)
        tiers[p.tier] = tiers.get(p.tier, 0) + 1
        phases[p.phase] = phases.get(p.phase, 0) + 1
        if p.seconds_ewma:
            measured += 1
        else:
            estimated += 1
        if p.phase == "paused":
            continue
        if p.tier == "daily":
            for d in days:
                d["sites"] += 1; d["pages"] += pgs; d["seconds"] += secs; d["var"] += var
                d["careful_sites"] += 1 if p.careful else 0
        elif p.tier == "weekly":
            d = days[int(p.assigned_weekday or 0) % 7]
            d["sites"] += 1; d["pages"] += pgs; d["seconds"] += secs; d["var"] += var
            d["careful_sites"] += 1 if p.careful else 0
        else:
            monthly_seconds += secs      # amortised across the month, shown as an overlay

    per_weekday = []
    for d in days:
        p50 = d["seconds"] + monthly_seconds / 28.0 * 7.0 / 7.0
        p90 = p50 + 1.28 * math.sqrt(max(0.0, d["var"]))
        per_weekday.append({
            "weekday": d["weekday"], "label": d["label"], "sites": d["sites"],
            "careful_sites": d["careful_sites"], "pages_p50": round(d["pages"]),
            "crawl_seconds_p50": round(p50), "crawl_hours_p50": round(p50 / 3600, 2),
            "crawl_hours_p90": round(p90 / 3600, 2),
        })
    peak = max(per_weekday, key=lambda x: x["crawl_hours_p90"]) if per_weekday else None
    workers = max(1, math.ceil((peak["crawl_hours_p90"] / window_hours))) if peak else 1
    return {
        "generated_at": utcnow().isoformat() + "Z",
        "confidence": {"sites_with_measurements": measured, "sites_estimated": estimated},
        "phase_counts": phases, "by_tier": tiers,
        "steady_state": {
            "per_weekday": per_weekday,
            "peak_day": peak,
            "mean_day_hours_p50": round(sum(d["crawl_hours_p50"] for d in per_weekday) / 7, 2) if per_weekday else 0,
            "monthly_overlay_hours_p50": round(monthly_seconds / 28.0 / 3600, 2),
        },
        "sizing": {"target_window_hours": window_hours,
                   "required_parallel_workers": workers,
                   "basis": "peak_day.crawl_hours_p90"},
    }
