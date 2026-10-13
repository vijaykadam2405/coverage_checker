"""THE control plane, in one place: run state, mode, and the auto-behaviour toggles.

Counted on the live fleet the day it took an operator an hour to stop everything: thirteen paths
could put work on an instance, ten independent holds could stop some of them, no two paths read the
same subset, and three paths CLEARED the halt as a side effect of dispatching. Every one of those
holds was a correct fix for one path. The product the operator was holding had no single answer to
"may anything crawl right now, and what".

This module is that answer. Three durable rows in `app_settings` — the same store as every flag
before it, so they survive a restart, a container recreate and a rebuild — and ONE function,
`lane_open`, that every dispatcher consults first:

  RUN STATE   `pipeline_enabled`  "true" = RUNNING; anything else (absent included) = STOPPED.
              Stop is a hard stop that STAYS stopped: it closes every lane, and no timer, teardown,
              refill, autopilot, maintenance-off or dispatch route can lift it. Only an explicit
              Start does. (The row is the old master switch's row on purpose: an existing
              deployment keeps its state across the upgrade, and `master_enabled()` keeps meaning
              exactly what it meant — is the pipeline allowed to run.)

  MODE        `crawl_mode`  "normal" | "campaign". MUTUALLY EXCLUSIVE kinds of work:
                normal    the system's own steady-state work — the daily lane (defence news +
                          competitor sites), the catalogue schedule, and their saved frontiers
                campaign  ad-hoc work the operator submits — manual batches, gap-fill — and
                          THEIR saved frontiers
              Each mode opens only its own lanes, and the resume lane is scoped by who OWNS a
              saved frontier, so switching modes cannot revive the other mode's backlog.

  TOGGLES     `auto:<name>`  every behaviour that starts work on a timer, OFF by default:
                scheduled        the 20s scheduler tick dispatching due catalogue jobs
                refill           the 60s watcher resuming saved frontiers on idle instances
                autopilot        the 15-minute health pass (traps sweep, seed re-check, invariants)
                health_dispatch  ...and whether that pass may ALSO put idle instances to work
                                 (assign: resume + rebalance + first-crawls)
                repin            the daily lane re-pinning its domains onto lane instances every
                                 tick (a rebalance elsewhere keeps undoing the pin)
              A toggle is a permission for a TIMER. Explicit buttons (Run, Assign, a pasted batch)
              are the operator asking, and are governed by the run state and the mode alone.
              `auto_on` is the EFFECTIVE value: stored AND running. Stop hard-disables all of them
              without touching the stored values, so Start restores what the operator chose.

The old `operator_halt` row is retired (`scheduler.halted()` now reads the run state), and the old
`dispatch_lanes` row is ignored: the lanes are DERIVED from state + mode, never set directly, which
is what makes "if I stop the campaign and start my normal crawl it should just work" true — there
is no third thing left behind to fight it.

Pure by design: reads and writes the Setting store, calls nothing on the fleet. The fan-outs that a
Stop or a mode switch cause live in the API route that the operator pressed.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ScheduledJob, Setting

# ── vocabulary ──────────────────────────────────────────────────────────────

MODES: dict[str, str] = {
    "normal": "the daily lane (defence news + competitor sites) and the catalogue schedule",
    "campaign": "ad-hoc batches the operator submits — manual batch, gap-fill",
}

# The LANES are kinds of work, not dispatchers: the same kind reaches the fleet by several routes.
LANES: dict[str, str] = {
    "daily":     "the daily lane (daily.py) — defence news + competitor sites in daily_urls",
    "scheduled": "catalogue jobs — the scheduler tick, POST /api/run, run-delta, Assign's "
                 "first-crawls",
    "resume":    "resuming saved frontiers — refill (timer, button, Assign, maintenance-off) and "
                 "POST /api/crawler/resume; scoped to the frontiers the current mode OWNS",
    "manual":    "ad-hoc batches — POST /api/crawler/batch, gap-fill",
}
MODE_LANES: dict[str, frozenset[str]] = {
    "normal": frozenset({"daily", "scheduled", "resume"}),
    "campaign": frozenset({"manual", "resume"}),
}

AUTOS: dict[str, str] = {
    "scheduled": "scheduled catalogue re-crawls: the 20s scheduler tick dispatches due jobs",
    "refill": "auto-refill: every 60s, resume saved frontiers on idle instances",
    "autopilot": "health autopilot: every 15 min sweep queued junk, re-check dead seeds, "
                 "run the invariants and the corpus-quality scan",
    "health_dispatch": "the autopilot may also put idle instances back to work (assign: resume "
                       "saved frontiers, rebalance finished domains, first-crawl uncrawled sites)",
    "repin": "the daily lane re-pins its domains onto lane instances every tick",
}

# Job ids the NORMAL mode produces, by construction (api.py / daily.py build them with these
# prefixes). A saved frontier with one of these ids, or whose id is enrolled in scheduled_jobs, is
# the system's own work; anything else was submitted by hand and belongs to a campaign.
NORMAL_PREFIXES: tuple[str, ...] = ("daily_", "firstcrawl_", "delta_", "cat_", "recrawl_")

_RUN_KEY = "pipeline_enabled"
_MODE_KEY = "crawl_mode"
_AUTO_KEY = "auto:"


# ── the store ───────────────────────────────────────────────────────────────

def _get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return None if row is None else row.value


def _put(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def _log(db: Session, event: str, detail: str | None = None) -> None:
    from . import scheduler          # deferred: scheduler imports THIS module at load time
    scheduler.log_event(db, event, detail=detail)


# ── run state ───────────────────────────────────────────────────────────────

def running(db: Session) -> bool:
    return _get(db, _RUN_KEY) == "true"


def stopped(db: Session) -> bool:
    return not running(db)


def stop(db: Session, reason: str | None = None) -> bool:
    """Record the hard stop. Returns whether the state CHANGED, so a route can say "already
    stopped" instead of pretending a second press did something new. Idempotent on purpose: the
    operator who presses Stop twice is expressing certainty, not asking for a different outcome."""
    changed = running(db)
    _put(db, _RUN_KEY, "false")
    _log(db, "stop", detail=reason or "operator stop — every lane closed until Start")
    db.commit()
    return changed


def start(db: Session) -> bool:
    """The ONE thing that lifts a stop. Nothing on a timer calls this."""
    changed = not running(db)
    _put(db, _RUN_KEY, "true")
    _log(db, "start", detail="operator start — mode %s" % mode(db))
    db.commit()
    return changed


# ── mode ────────────────────────────────────────────────────────────────────

def mode(db: Session) -> str:
    v = (_get(db, _MODE_KEY) or "normal").strip().lower()
    return v if v in MODES else "normal"


def set_mode(db: Session, new: str) -> bool:
    """Write the mode. Returns whether it changed. Raises ValueError on an unknown mode — a typo
    must not silently freeze the fleet by opening no lane at all.

    Does NOT touch the run state: a mode is WHAT may run, Start/Stop is WHETHER. Switching modes
    while stopped is an ordinary thing to do (set the campaign up, then press Start)."""
    new = (new or "").strip().lower()
    if new not in MODES:
        raise ValueError("unknown mode %r — known: %s" % (new, sorted(MODES)))
    old = mode(db)
    if old == new:
        return False
    _put(db, _MODE_KEY, new)
    _log(db, "mode", detail="%s -> %s" % (old, new))
    db.commit()
    return True


# ── the gate ────────────────────────────────────────────────────────────────

def lane_open(db: Session, lane: str) -> bool:
    """May work of kind `lane` go out right now? Checked FIRST by every dispatcher.

    Closed when stopped, closed under maintenance (the operator is moving frontiers and needs the
    fleet to hold still — it used to stand down the scheduler, refill and the autopilot but NOT
    the daily lane or a pasted batch), and otherwise open exactly for the current mode's lanes.
    A lane name nobody declared is closed, so a dispatcher added later cannot slip past the mode
    by inventing one.

    Same trap as every other Setting read: SessionLocal is expire_on_commit=False, so a long-lived
    session (the serial tick, a manual batch) answers from its identity map unless it calls
    db.expire_all() first. Every loop that re-reads mid-run already does.
    """
    if stopped(db):
        return False
    if _get(db, "maintenance_mode") == "true":
        return False
    return lane in MODE_LANES[mode(db)]


def open_lanes(db: Session) -> set[str]:
    return {lane for lane in LANES if lane_open(db, lane)}


def closed_reason(db: Session) -> str | None:
    """Why NOTHING may dispatch, or None when at least the mode's lanes are open."""
    if stopped(db):
        return "stopped — press Start"
    if _get(db, "maintenance_mode") == "true":
        return "maintenance mode is on"
    return None


def why_closed(db: Session, lane: str) -> str | None:
    """The sentence a held dispatcher logs, or None when the lane is open. Every hold says WHICH
    of the three things is holding it — a fleet that will not start must never be a mystery."""
    r = closed_reason(db)
    if r:
        return r
    m = mode(db)
    if lane in MODE_LANES[m]:
        return None
    return "the '%s' lane is not part of %s mode" % (lane, m)


# ── toggles ─────────────────────────────────────────────────────────────────

def auto_setting(db: Session, name: str) -> bool:
    """The STORED value of a toggle. Absent = off: every automatic behaviour is opt-in."""
    if name not in AUTOS:
        raise ValueError("unknown auto-behaviour %r — known: %s" % (name, sorted(AUTOS)))
    return _get(db, _AUTO_KEY + name) == "true"


def auto_on(db: Session, name: str) -> bool:
    """The EFFECTIVE value: the toggle is on AND the pipeline is running. Stop hard-disables every
    timer regardless of how its toggle is set, and leaves the toggle as the operator set it."""
    return running(db) and auto_setting(db, name)


def set_auto(db: Session, name: str, on: bool) -> None:
    if name not in AUTOS:
        raise ValueError("unknown auto-behaviour %r — known: %s" % (name, sorted(AUTOS)))
    _put(db, _AUTO_KEY + name, "true" if on else "false")
    _log(db, "auto_on" if on else "auto_off", detail=name)
    db.commit()


def autos(db: Session) -> dict[str, dict]:
    run = running(db)
    return {name: {"on": auto_setting(db, name), "effective": run and auto_setting(db, name),
                   "label": label}
            for name, label in AUTOS.items()}


# ── who owns a saved frontier ───────────────────────────────────────────────

def _scheduled_ids(db: Session) -> set[str]:
    out: set[str] = set()
    for job in db.scalars(select(ScheduledJob.job)).all():
        jid = (job or {}).get("job_id") if isinstance(job, dict) else None
        if jid:
            out.add(str(jid))
    return out


def job_owner(db: Session, job_id: str, scheduled: set[str] | None = None) -> str:
    """Which mode a job belongs to. `scheduled` lets a caller classifying many ids read the
    schedule once."""
    jid = str(job_id or "")
    if jid.startswith(NORMAL_PREFIXES):
        return "normal"
    if jid in (scheduled if scheduled is not None else _scheduled_ids(db)):
        return "normal"
    return "campaign"


def owned_by_mode(db: Session, job_ids: list[str]) -> list[str]:
    """The subset of `job_ids` the CURRENT mode may resume, in the order given. The rest are the
    other mode's frontiers: parked, not dropped — they come back when that mode is active again."""
    cur = mode(db)
    sched = _scheduled_ids(db)
    return [j for j in job_ids if job_owner(db, j, sched) == cur]


# ── what the panel reads ────────────────────────────────────────────────────

def state(db: Session) -> dict:
    run = running(db)
    m = mode(db)
    return {
        "run": "running" if run else "stopped",
        "mode": m,
        "modes": dict(MODES),
        "lanes": {"open": sorted(open_lanes(db)),
                  "closed": sorted(set(LANES) - open_lanes(db)),
                  "known": dict(LANES),
                  "held": closed_reason(db)},
        "auto": autos(db),
    }
