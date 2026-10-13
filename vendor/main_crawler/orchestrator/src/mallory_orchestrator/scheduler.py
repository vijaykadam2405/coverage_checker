"""In-process crawl scheduler.

# ponytail: one asyncio task + a next_run column in SQLite. No APScheduler/Celery/Redis.
# The whole loop is a 20s tick that dispatches due jobs. State lives in the DB, so it's
# restart-safe and debuggable. It runs INSIDE the web process → the orchestrator must run
# with a SINGLE uvicorn worker (two workers = two schedulers = double dispatch). Graduate to
# APScheduler / an external worker only when you need multiple workers or cron expressions.

Design: every decision function is SYNC and takes an explicit `now`/`db` (so tests drive it
with an injected dispatch stub, no HTTP, no asyncio); only `loop()` is async.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor as _TPE

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crawler_client, orchestrate, policy, tiering
from .config import get_settings
from .db import SessionLocal
from .models import JobRun, ScheduledJob, ScheduleLog, Setting, SiteSchedule, Source

log = logging.getLogger("mallory.scheduler")

FREQ_SECONDS = {**orchestrate.FREQ_SECONDS, "monthly": 30 * 86400}

# How many jobs one tick may dispatch. Without a cap a 500-site baseline blocks a single tick for
# days and never re-evaluates priority, so a freshly-generated high-defence job would queue behind
# 400 low-priority ones. Capping re-runs the priority sort every tick.
MAX_DISPATCH_PER_TICK = 25


def utcnow() -> dt.datetime:
    """Naive UTC — matches how SQLite hands datetimes back, so next_run comparisons never
    hit an aware/naive TypeError."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ── master switch = THE RUN STATE (policy.py) ─────────────────────────────────
# `pipeline_enabled` is the one row that says whether anything may crawl. It used to gate the
# scheduler tick only; now it is the hard stop every dispatcher reads through policy.lane_open.
# Kept under these names so every existing caller — and every existing test that arms the master
# to make a dispatch test mean something — keeps working unchanged.

def master_enabled(db: Session) -> bool:
    return policy.running(db)


def set_master(db: Session, on: bool) -> None:
    if on:
        policy.start(db)
    else:
        policy.stop(db, reason="master switch off")


# ── operator halt ─────────────────────────────────────────────────────────────
# A STOP MUST OUTLIVE THE TEARDOWN IT CAUSES. The crawler's own stop signal is consumed by the
# engine while it winds down, so `stop_pending` is true only DURING the stop — the moment teardown
# finishes the instance is indistinguishable from one that merely ran out of work. The auto-refill
# watcher then resumed it from its saved frontier, and the crawl an operator had deliberately halted
# came back roughly a minute later with nothing on screen to say why. Measured: stopped at 09:30
# with 578 saved jobs, crawling again by 10:05 (POST /v1/crawl/resume from the refill loop).
#
# The `operator_halt` row that recorded this is RETIRED: it was a second copy of "stopped" that
# three dispatch routes cleared as a side effect, which is how a placer script re-armed the whole
# fleet. `halted` now reads the run state itself — there is one stop, and only Start lifts it.

def halted(db: Session) -> bool:
    return policy.stopped(db)


def set_halted(db: Session, on: bool) -> None:
    if on:
        policy.stop(db, reason="operator stop")
    else:
        policy.start(db)


# The rest of the control plane, re-exported here because every loop in this package reaches its
# flags through `scheduler.*` (and every test that stubs the flags stubs this module).

def auto_on(db: Session, name: str) -> bool:
    return policy.auto_on(db, name)


def owned_by_mode(db: Session, job_ids: list[str]) -> list[str]:
    return policy.owned_by_mode(db, job_ids)


def why_closed(db: Session, lane: str) -> str | None:
    return policy.why_closed(db, lane)


# ── maintenance mode ──────────────────────────────────────────────────────────
# A THIRD state, and it exists because the other two cannot express it. The master switch STOPS the
# pipeline and drops the frontier; the operator halt means "stay stopped until I say otherwise".
# Neither says "I am about to move your frontiers around, hold still for ten minutes".
#
# Without it, every maintenance action loses a race it cannot see: auto-refill resumes an idle
# instance within a minute, and both the frontier prune and the frontier move REFUSE to touch a
# crawling instance (409). Measured 2026-07-31 — a fleet-wide stop, then a prune twelve seconds
# later, and eleven of twelve had already been resumed and refused. The workaround that day was to
# `docker stop` the orchestrator, which is not a thing an operator should have to know.
#
# Deliberately NOT the master switch: that one drops the durable frontier, which is precisely the
# thing maintenance exists to preserve.

def maintenance(db: Session) -> bool:
    row = db.get(Setting, "maintenance_mode")
    return row is not None and row.value == "true"


def set_maintenance(db: Session, on: bool) -> None:
    row = db.get(Setting, "maintenance_mode")
    if row is None:
        db.add(Setting(key="maintenance_mode", value="true" if on else "false"))
    else:
        row.value = "true" if on else "false"
    log_event(db, "maintenance_on" if on else "maintenance_off")
    db.commit()


# ── dispatch policy: THE one gate ─────────────────────────────────────────────
# Every other switch in this file is a HOLD on one kind of work, read by SOME of the things that
# start work. Counted on the live fleet the day it took an hour to stop everything and run only the
# daily lane: the master, the halt, maintenance, six category switches, two startup-only env
# kill-switches (CRAWLER_AUTO_REFILL, CRAWLER_AUTOPILOT) and the crawler's own stop file — and no
# two dispatchers read the same subset. The scheduler tick reads four; refill reads the halt and
# maintenance but not the master; the daily lane reads the master but not the halt; the manual
# batch loop never reads the halt at all; and three API routes CLEAR the halt as a side effect of
# dispatching, so any placer script re-armed the whole fleet. Stop meant stop on some paths, and
# "run only lane X" was not expressible on any of them — the categories are holds on buckets, and
# an ad-hoc firstcrawl_/full_/kssl_ job has no bucket switch.
#
# This is the invariant those switches were missing: ONE durable policy, stored where the other
# flags live, that every dispatcher consults FIRST. It lives in policy.py and it is DERIVED, never
# set: the run state (Start/Stop) and the mode (normal/campaign) decide which lanes are open, so
# there is no separate "lanes" row that a stop or a mode switch could leave behind to fight them.
# The old `dispatch_lanes` row is ignored.
#
# The lanes are KINDS OF WORK, not dispatchers, because the same kind reaches the fleet by several
# routes — the refill timer, the Assign button, the autopilot and maintenance-off all resume saved
# frontiers — and the operator's question is about the work: "only the daily lane", "nothing from
# the backlog". It narrows only: a lane the policy closes is closed however the master, the halt,
# maintenance or a category switch read, and `force` does not lift it (a carve-out an automation
# can walk through is a hole, see refill). Nothing here stops a crawl already in flight — that is
# the fan-out the Stop route sends — and nothing here drops a frontier.

LANES = policy.LANES


def lane_open(db: Session, lane: str) -> bool:
    """The check at the top of every dispatcher. Unknown lane names are CLOSED, so a dispatcher
    added later cannot slip past the mode by inventing one. See policy.lane_open."""
    return policy.lane_open(db, lane)


def dispatch_policy(db: Session) -> dict:
    """What the panel and the API report: the run state, the mode, and which lanes that opens."""
    st = policy.state(db)
    return {"run": st["run"], "mode": st["mode"], "lanes": st["lanes"]["open"],
            "closed": st["lanes"]["closed"], "held": st["lanes"]["held"], "known": dict(LANES)}


# ── per-category switches (Setting kv) ────────────────────────────────────────
# Stored as "category_off:<job_type>" = "true" ⇒ DISABLED. Keyed OFF, not ON, so a job_type that
# has never been touched (or one invented later) is enabled by default — an operator must never
# discover a whole category went dark because nobody clicked a switch that didn't exist yet.
# Category == ScheduledJob.job_type, which is already a denormalized indexed column.

_CAT_OFF = "category_off:"


def category_enabled(db: Session, job_type: str) -> bool:
    row = db.get(Setting, _CAT_OFF + (job_type or ""))
    return not (row is not None and row.value == "true")


def disabled_categories(db: Session) -> set[str]:
    rows = db.scalars(select(Setting).where(Setting.key.startswith(_CAT_OFF))).all()
    return {r.key[len(_CAT_OFF):] for r in rows if r.value == "true"}


def set_category(db: Session, job_type: str, on: bool) -> None:
    key = _CAT_OFF + job_type
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value="false" if on else "true"))
    else:
        row.value = "false" if on else "true"
    log_event(db, "category_on" if on else "category_off", detail=job_type)
    db.commit()


# ── schedule-log writer (no commit; caller commits) ───────────────────────────

def log_event(db: Session, event: str, schedule_id: int | None = None,
              job_id: str | None = None, detail: str | None = None) -> None:
    db.add(ScheduleLog(at=utcnow(), schedule_id=schedule_id, job_id=job_id,
                       event=event, detail=detail))


def log_cycle(db: Session, schedule_id: int | None, job_id: str | None, summary: dict | None,
              elapsed: float, *, baseline: bool = False) -> None:
    """Close the loop for ONE url: `completed` (the readable full-cycle line) PLUS one badge row.

    Two rows, not one, because the Logs page badges off `event` — and this repo has no Alembic, so
    a `new_data` COLUMN is not available (create_all only ever adds new TABLES). The flag therefore
    lives in its own event string: new_data | no_new_data | baseline.

    `stored` is the ingest-ACCEPTED count when the crawler reports it, not `kept`: that is the only
    number proving the cycle actually reached Postgres+MinIO, so a healthy crawl talking to a dead
    ingest reads stored=0 instead of quietly claiming success.
    """
    summary = summary or {}
    g = lambda k: int(summary.get(k) or 0)  # noqa: E731
    fetched, kept = g("fetched"), g("kept")
    stored = g("accepted") if "accepted" in summary else kept
    unchanged = g("skipped_unchanged") + g("not_modified_304")
    # REACHED NOTHING IS NOT A CLEAN CYCLE. fetched=0 with nothing skipped as unchanged means the
    # crawl never got a page at all — a dead apex, a WAF, a timeout. It was logged "cycle ok"
    # identically to a re-crawl that fetched 400 pages and found them all unchanged, which is how 92
    # catalogued sites sat at zero pages looking green: every one of their first crawls reported
    # `cycle ok · fetched=0 kept=0 stored=0`.
    #
    # unchanged>0 is the honest no-op — the pages WERE reached and were 304/hash-identical.
    reached_nothing = fetched == 0 and unchanged == 0
    log_event(db, "completed", schedule_id, job_id,
              detail=(f"reached nothing · fetched=0 · {elapsed:.1f}s" if reached_nothing else
                      f"cycle ok · fetched={fetched} kept={kept} stored={stored} "
                      f"unchanged={unchanged} · {elapsed:.1f}s"))
    if reached_nothing:
        # Its own event, so it is filterable and cannot be read as a successful crawl. Deliberately
        # NOT an error: the dispatch worked and the site answered nothing, which is a fact about the
        # site, and calling it a failure would put it into retry backoff for a condition retrying
        # does not fix.
        log_event(db, "reached_nothing", schedule_id, job_id,
                  detail="the crawl fetched no page at all — dead host, blocked, or timing out")
    elif baseline:
        # First-ever crawl: everything is new by definition, so a "NEW DATA" badge would be noise.
        log_event(db, "baseline", schedule_id, job_id,
                  detail=f"baseline census · {stored} pages stored")
    elif kept > 0:
        # The crawler skips re-ingesting content-hash-unchanged pages, so on a re-crawl kept>0 is
        # genuinely new-or-changed content, not a re-upload of what we already had.
        log_event(db, "new_data", schedule_id, job_id,
                  detail=f"{kept} new/changed pages stored (Postgres + MinIO)")
    else:
        log_event(db, "no_new_data", schedule_id, job_id,
                  detail=f"no change · {unchanged} pages unchanged")


# ── next-run math (pure) ──────────────────────────────────────────────────────

def compute_next_run(frequency: str, prev: dt.datetime | None, now: dt.datetime,
                     *, policy: SiteSchedule | None = None) -> dt.datetime | None:
    """None for a one-shot. For recurring, advance FROM the scheduled time so phase is kept
    (a daily 09:00 job stays 09:00), collapsing any missed windows into a single next firing.

    `tiered` is the adaptive cadence: the day/minute the site was levelled into IS its phase,
    so it does NOT phase-preserve — that assignment is what makes load levelling real."""
    if frequency == "tiered":
        return tiering.next_run_for_policy(policy, prev, now)
    if frequency == "once":
        return None
    interval = FREQ_SECONDS.get(frequency)
    if interval is None:
        return None
    step = dt.timedelta(seconds=interval)
    nxt = (prev or now) + step
    while nxt <= now:
        nxt += step
    return nxt


def due_jobs(db: Session, now: dt.datetime) -> list[ScheduledJob]:
    return list(db.scalars(
        select(ScheduledJob)
        .where(ScheduledJob.enabled.is_(True),
               ScheduledJob.next_run.is_not(None),
               ScheduledJob.next_run <= now)
        .order_by(ScheduledJob.next_run)
    ).all())


# ── priority ordering — DEFENCE SITES FIRST (used by the loop AND the Run button) ──

def policy_for(db: Session, sj: ScheduledJob) -> SiteSchedule | None:
    sid = (sj.job or {}).get("source_id")
    if not sid:
        return None
    return db.scalars(select(SiteSchedule).where(SiteSchedule.source_id == sid)).first()


def order_jobs(db: Session, jobs: list[ScheduledJob], now: dt.datetime) -> list[ScheduledJob]:
    """Sort by tiering.priority_key: baseline census first, then defence score, category urgency,
    most-overdue, then cheapest (shortest-job-first minimises mean completion on a single-flight
    worker, so more sites are done if the operator stops the run halfway)."""
    pols = {p.source_id: p for p in db.scalars(select(SiteSchedule)).all()}
    srcs = {s.source_id: s for s in db.scalars(select(Source)).all()}
    def key(sj: ScheduledJob):
        sid = (sj.job or {}).get("source_id")
        pol = pols.get(sid)
        return tiering.priority_key(pol.domain if pol else sid, pol, srcs.get(sid), now)
    return sorted(jobs, key=key)


# ── tick body (sync — this is where the whole policy lives) ───────────────────

# How many instances may crawl AT ONCE. Not a nicety — measured on the box the day the scheduler
# learned to dispatch fleet-wide:
#
#     ~7 instances crawling   load  46    190 pages/min
#     11-12 crawling          load 201     22 pages/min
#
# 38.3 of 40 cores were ours (budget: 30) across 884 Chromium processes, and the extra concurrency
# bought NEGATIVE throughput — the box was context-switching, not crawling. The crawlers are capped
# with `cpu_shares`, which is a weight and bounds nothing (see the deployment notes), so nothing
# below the orchestrator was ever going to stop this. 0 disables the cap.
MAX_BUSY_INSTANCES = int(os.environ.get("CRAWLER_MAX_BUSY_INSTANCES", "8"))

# How many due sites one instance may be handed in a single tick.
#
# THIS IS THE RE-CRAWL THROUGHPUT KNOB, and at 1 it was costing about 5x. An engine has 32 tabs and
# a 256-worker httpx pool, but per-HOST concurrency is politeness-capped (3-48 adaptive) — so one
# site per instance means 12 hosts in flight fleet-wide, each throttled, with the tabs idle.
# Measured on the live fleet at 1:
#
#     inst 1  batch_total=1  frontier=0  active=3   0.062 pages/s
#     inst 3  batch_total=1  frontier=0  active=3   0.065 pages/s
#     inst 12 batch_total=1  frontier=2257 active=16 1.899 pages/s
#
# `active=3` against 32 tabs is the whole story. Worse, a single site spends most of its life in a
# low-parallelism TAIL (frontier=0, a few stragglers), and the instance then idles until the next
# 20s tick. The first full crawl did not look like this because it was dispatched as manual batches
# of MANY sites per instance — which is why it held 13-20 pages/s and this holds 2-3.
#
# Default stays 1 so behaviour is unchanged without an explicit opt-in; the data centre sets 10.
JOBS_PER_INSTANCE = max(1, int(os.environ.get("CRAWLER_JOBS_PER_INSTANCE", "1")))

# Scheduled dispatches run here instead of blocking the 20s tick. Sized to the fleet: at most one
# job per instance is ever in flight, so the pool can never be the constraint.
_POOL = _TPE(max_workers=32, thread_name_prefix="dispatch")
# Schedule ids currently being crawled. due_jobs() selects on next_run, which is only advanced when
# a crawl SUCCEEDS — so without this the next tick re-picks a still-running job and sends it to a
# second instance.
_INFLIGHT: set[int] = set()


def free_instances(prefer: set[int] | None = None) -> list[int]:
    """Instance indices that may take work right now.

    Two conditions, not one: the instance is not already running a batch, AND the fleet is under its
    concurrency cap. Unreachable ⇒ NOT free — dispatching into a crawler we cannot see is how a job
    burns a retry for nothing.

    `prefer` is the set of instances that actually OWN due work. When the cap leaves only a slot or
    two, which idle instances those slots go to decides whether the tick dispatches anything at all.
    """
    rows = crawler_client.fanout(crawler_client.batch_status)
    live = [r for r in rows if r.get("ok") and isinstance(r.get("data"), dict)]
    busy = [r["instance"] for r in live if r["data"].get("running")]
    idle = [r["instance"] for r in live if not r["data"].get("running")]
    if MAX_BUSY_INSTANCES <= 0:
        return idle
    room = max(0, MAX_BUSY_INSTANCES - len(busy))
    # SPEND THE SCARCE SLOTS ON INSTANCES THAT HAVE WORK. Trimming by index order wasted them
    # outright — measured live: 7 instances crawling under a cap of 8 left exactly one slot, it went
    # to instance 5 which owned 0 of the 53 due jobs, and instances 8-11 sat idle owning 25 of them.
    # Every tick dispatched nothing while a quarter of the queue had an idle container waiting.
    #
    # Same class of bug as truncating the DUE list before the owner match (see run_due) — a cap
    # applied before owners are known always cuts the wrong thing. Stable sort, so the order within
    # each group is unchanged and an empty/absent `prefer` behaves exactly as before.
    if prefer:
        idle.sort(key=lambda i: i not in prefer)
    # Report only as many as would keep the fleet inside the cap. Callers take one job per entry,
    # so trimming the LIST is what actually enforces it — a check after dispatch would be too late.
    return idle[:room]


def _crawler_busy() -> bool:
    """True when there is NOWHERE to put a job — i.e. every instance is mid-batch.

    It used to ask `crawler_client.batch_status()` with no base, which is instance 0 and only
    instance 0. On a twelve-instance fleet that made one busy container speak for all twelve: the
    tick logged `skipped_busy — 20 job(s) due; crawler busy` every twenty seconds while five
    instances sat idle, and 485 of 507 enrolled re-crawls had never run once. The fleet was not
    out of work; the scheduler could not see eleven twelfths of it.

    Unreachable-everywhere ⇒ busy, because free_instances() counts only instances that answered.
    """
    try:
        return not free_instances()
    except Exception:
        return False


def _on_failure(db: Session, sj: ScheduledJob, now: dt.datetime, err: str,
                policy: SiteSchedule | None = None) -> None:
    """Retry with 60/120/240s backoff; on exhaustion, disable a one-shot or skip to the next
    window for a recurring job. An error JobRun is written so exec logs show the failure."""
    job_id = sj.job.get("job_id")
    db.add(JobRun(job_id=job_id, domain=None, entity_id=sj.job.get("target_entity"),
                  dispatched_at=orchestrate._now(), status="error", detail=err[:400]))
    if sj.retries_left > 0:
        sj.retries_left -= 1
        n = max(0, sj.retry_count - sj.retries_left - 1)
        sj.next_run = now + dt.timedelta(seconds=60 * (2 ** n))
        sj.last_status = "retrying"
        log_event(db, "retry", sj.id, job_id, detail=f"{sj.retries_left} left: {err[:200]}")
    else:
        sj.last_status = "error"
        log_event(db, "failed", sj.id, job_id, detail=err[:200])
        if sj.frequency == "once":
            sj.enabled = False
            sj.next_run = None
        else:
            sj.retries_left = sj.retry_count
            sj.next_run = compute_next_run(sj.frequency, prev=sj.next_run, now=now, policy=policy)


def _on_success(db: Session, sj: ScheduledJob, now: dt.datetime,
                policy: SiteSchedule | None = None) -> None:
    sj.retries_left = sj.retry_count
    if sj.frequency == "once":
        sj.enabled = False
        sj.next_run = None
        sj.last_status = "done"
        return
    if sj.frequency == "tiered" and policy is not None:
        # baseline→probe→steady, then re-tier from measured λ and re-bucket if the tier moved.
        tiering.advance_phase(db, policy, now)
    sj.next_run = compute_next_run(sj.frequency, prev=sj.next_run, now=now, policy=policy)
    sj.last_status = "ok"


def _dispatch_fleet(db: Session, due: list, now: dt.datetime, dispatch) -> int:
    """Send at most one due job to each FREE instance, all at once, each on its own DB session.

    Every job still runs the identical body as the serial path — same master-switch re-read, same
    tiering.observe, same success/failure bookkeeping. What changes is only how many run at a time.

    Sessions are per-thread because SQLAlchemy's Session is not thread-safe, and the failure mode if
    they were shared is not an exception, it is two ticks interleaving writes on the same rows.
    """

    from . import shard
    from .db import SessionLocal

    pool_bases = crawler_client.pool()
    n = len(pool_bases)

    # Owners FIRST, free-list second. The cap has to know which instances have work before it
    # decides which get the remaining slots, or it hands them to instances that own nothing.
    owner_of: dict[int, object] = {}        # instance -> the one job it will run this tick
    for sj in due:
        seeds = (sj.job or {}).get("seed_urls") or []
        try:
            owner = shard.instance_for(db, seeds[0] if seeds else "", n)
        except Exception:                   # noqa: BLE001 — an unshardable job is not a crash
            continue
        owner_of.setdefault(owner, []).append(sj)

    free = set(free_instances(prefer=set(owner_of)))
    if not free:
        return 0
    # NEWS FIRST — and this is the path that actually decides it.
    #
    # The daily lane's hold was written into orchestrate._dispatch_routed, which is the MANUAL
    # BATCH path. The recurring scheduler dispatches here instead and never goes through it, so the
    # hold did nothing where it mattered: measured live, instance 0 finished a daily site at 11:27
    # and was handed cat_gov_portal_darpa while 36 daily sites sat due, because this function had
    # never heard of the lane. A hold on one of two dispatch paths is not a hold.
    #
    # Unlike _dispatch_routed this does NOT keep a bucket back for the catalogue. There, holding
    # every bucket would have meant a chunk dispatching nothing at all; here `free` is just this
    # tick's idle list out of twelve instances, so holding all of it costs one 20s tick on three
    # containers while nine others keep the backlog moving. That is what "news first" has to mean
    # to be worth anything — the alternative is a lane that only ever gets an instance by winning a
    # race against a tick three times faster than its own.
    try:
        from . import daily as _daily
        reserved = _daily.reserved_instances()
        if reserved and not _daily.reservation()["exclusive"] and _daily.holds_fleet(db):
            held = free & reserved
            if held:
                free -= held
                log.info("holding instance(s) %s for the daily lane", sorted(held))
                if not free:
                    return 0
    except Exception:   # noqa: BLE001 — the lane must never be able to stop the catalogue
        log.exception("daily-lane hold check failed; dispatching to the whole fleet")
    picked = {inst: sjs[:JOBS_PER_INSTANCE] for inst, sjs in owner_of.items() if inst in free}
    picked = {i: v for i, v in picked.items() if v}
    if not picked:
        # Everything due belongs to an instance that is busy. Not an error and not a skip worth
        # logging every twenty seconds — the jobs stay due and go out on a later tick.
        return 0

    ids = {inst: [sj.id for sj in sjs] for inst, sjs in picked.items()}

    def _one(inst: int, sj_ids: list[int]) -> int:
        s = SessionLocal()
        try:
            sjs = [x for x in (s.get(ScheduledJob, i) for i in sj_ids) if x is not None]
            if not sjs:
                return 0
            base = pool_bases[inst] if inst < len(pool_bases) else None
            send = dispatch if dispatch is not None else (
                lambda job, _b=base: crawler_client.dispatch(job, base=_b))
            return _dispatch_serial(s, sjs, now, send, len(ids))
        except Exception:                   # noqa: BLE001 — one bad job must not kill the tick
            log.exception("scheduled dispatch failed on instance %s", inst)
            return 0
        finally:
            s.close()

    # FIRE AND RETURN — do NOT wait for the slowest job in this batch.
    #
    # `with ThreadPoolExecutor(...) as ex: ex.map(...)` blocks until every dispatch finishes, and a
    # dispatch is a whole crawl. So one 10-minute site pinned the tick while the four instances that
    # finished in 10 seconds sat idle for the rest of it — measured on screen as 1 of 12 crawling
    # with 483 sites due. The barrier bought nothing: the next tick re-reads which instances are
    # free, so there is no state to synchronise.
    #
    # `_INFLIGHT` is what stops a job being dispatched twice. due_jobs() selects on next_run, which
    # is not advanced until the crawl SUCCEEDS, so without this the next tick would pick the same
    # still-running job and hand it to a second instance.
    for inst in list(ids):
        ids[inst] = [sid for sid in ids[inst] if sid not in _INFLIGHT]
        if not ids[inst]:
            del ids[inst]
    if not ids:
        return 0
    picked_ids = [sid for sids in ids.values() for sid in sids]
    _INFLIGHT.update(picked_ids)

    def _run(inst: int, sj_ids: list[int]) -> None:
        try:
            _one(inst, sj_ids)
        finally:
            for sid in sj_ids:
                _INFLIGHT.discard(sid)

    for inst, sids in sorted(ids.items()):
        _POOL.submit(_run, inst, sids)
    return len(picked_ids)


def run_due(db: Session, *, now: dt.datetime | None = None, dispatch=None, busy_check=None,
            max_dispatch: int | None = MAX_DISPATCH_PER_TICK) -> int:
    """Dispatch due jobs DEFENCE-FIRST — one per FREE instance, concurrently.

    It used to be strictly sequential, with the comment "the crawler is single-flight". That was
    true of one crawler and became the fleet's ceiling: dispatch() blocks for an entire crawl, so
    one scheduled re-crawl ran at a time however many instances were idle.

    Returns #dispatched. `dispatch`/`busy_check` are injectable so tests drive this with no HTTP —
    and injecting `dispatch` also selects the SEQUENTIAL path, because a stub has no instances to
    fan out across and every existing test asserts the sequential contract (defence-first order,
    master switch honoured mid-tick, N dispatches counted).
    """
    now = now or utcnow()
    stubbed = dispatch is not None
    dispatch = dispatch or crawler_client.dispatch
    busy_check = busy_check or _crawler_busy

    due = due_jobs(db, now)
    if not due:
        return 0

    # THE ONE GATE, first. Everything below is a narrower hold on some of this work; this is the
    # policy that can hold all of it, and it is what "run only the daily lane" means here.
    if not lane_open(db, "scheduled"):
        log_event(db, "skipped_lane_closed",
                  detail=f"{len(due)} job(s) held: the 'scheduled' lane is closed by the dispatch policy")
        db.commit()
        return 0

    off = disabled_categories(db)
    # Keyed on the operator BUCKET, matching the switch the operator actually flipped. See
    # categories.bucket_of_job — job_type cannot express these switches, because News, Gov portal
    # and Think tank all generate job_type "news".
    from . import categories as _cats
    bucket = {}
    if off:
        by_source = _cats.catalog_buckets(db)
        bucket = {sj.id: _cats.bucket_of_job(sj.job, by_source) for sj in due}
    held = [sj for sj in due if bucket.get(sj.id) in off] if off else []
    if held:
        # next_run is deliberately NOT advanced: a held job stays due and fires on the first tick
        # after its category is switched back on, instead of silently losing its window.
        due = [sj for sj in due if bucket.get(sj.id) not in off]
        cats = ", ".join(sorted({_cats.label(bucket[sj.id]) for sj in held}))
        log_event(db, "skipped_category", detail=f"{len(held)} job(s) held: {cats} disabled")
        db.commit()
    if not due:
        return 0

    if busy_check():
        log_event(db, "skipped_busy", detail=f"{len(due)} job(s) due; crawler busy")
        db.commit()
        return 0

    due = order_jobs(db, due, now)                 # defence-first priority

    # ONE JOB PER FREE INSTANCE, IN PARALLEL — the difference between a fleet and twelve copies of
    # one crawler. dispatch() blocks for the whole crawl (minutes to hours), so a serial loop means
    # exactly one scheduled crawl is in flight fleet-wide however many instances are idle, and the
    # other eleven have nothing to do the moment their own frontiers drain. Measured: 485 of 507
    # enrolled re-crawls had never run.
    #
    # Routed by domain shard, because a job's frontier and seen-set live inside ONE container: send
    # it elsewhere and that site restarts from its seed while its real progress sits unreachable.
    # A job whose owner is busy is simply not dispatched this tick — it stays due and goes on the
    # next one, which is cheaper than a dispatch the crawler refuses and a retry backoff.
    # THE CAP APPLIES TO THE SERIAL PATH ONLY. Truncating the due list BEFORE the owner match was a
    # silent throughput bug: with 507 jobs due and five instances free, the tick looked at the first
    # 25 by priority, found their owners busy, and dispatched NOTHING — five idle containers and a
    # 507-deep queue, for as long as the priority order held. The fleet path scans the whole ordered
    # list instead and caps itself naturally at one job per free instance.
    if stubbed:
        capped = due[:max_dispatch] if max_dispatch else due
        return _dispatch_serial(db, capped, now, dispatch, len(capped))
    return _dispatch_fleet(db, due, now, None)


def _dispatch_serial(db: Session, due: list, now: dt.datetime, dispatch, total: int) -> int:
    dispatched = 0
    for sj in due:
        # The master switch is re-read EVERY iteration, not once per tick in _tick_once.
        #
        # dispatch() below BLOCKS for the whole crawl — minutes to hours — so the flag this loop was
        # admitted on is stale by the second job. Flipping the switch off used to halt only the crawl
        # in flight: this loop then dispatched the next of up to MAX_DISPATCH_PER_TICK jobs, and the
        # crawler clears its own stop signal on the way into a batch, so the halt was undone and the
        # operator watched the "stopped" pipeline keep crawling. Off means off, including for work
        # this tick had already decided to do.
        db.expire_all()          # SessionLocal is expire_on_commit=False, so without this the
                                 # Setting comes from this session's identity map and NEVER sees
                                 # the API session's write. The whole re-check would be a no-op.
        if not master_enabled(db):
            log_event(db, "skipped_master_off",
                      detail=f"master switch off mid-tick: {total - dispatched} job(s) not dispatched")
            db.commit()
            break
        # Same staleness argument for the dispatch policy: closed mid-tick means the rest of this
        # tick's list is not sent.
        if not lane_open(db, "scheduled"):
            log_event(db, "skipped_lane_closed",
                      detail=f"'scheduled' lane closed mid-tick: {total - dispatched} job(s) not dispatched")
            db.commit()
            break
        job_id = sj.job.get("job_id")
        pol = policy_for(db, sj)
        sj.last_status = "running"
        sj.last_run_at = now
        log_event(db, "dispatched", sj.id, job_id)
        db.commit()  # so the panel shows it live
        payload = dict(sj.job)
        payload["forward_to_ingest"] = True
        # Read the phase BEFORE dispatch: tiering.observe() flips baseline→probe below, so asking
        # afterwards would label every first crawl as an ordinary "new data" run.
        baseline = pol is not None and pol.phase == "baseline"
        # CRITICAL: the crawler only emits 304/not-modified signal when incremental=True, and a
        # BASELINE census must fetch everything. So: baseline ⇒ full crawl, everything after ⇒ incremental.
        if pol is not None:
            payload["incremental"] = not baseline
        t0 = time.monotonic()
        try:
            resp = dispatch(payload)
        except Exception as e:  # noqa: BLE001 — record + move on
            _on_failure(db, sj, now, str(e), policy=pol)
            db.commit()
            continue
        elapsed = time.monotonic() - t0
        orchestrate.record_run(db, payload, resp)
        summ = resp.get("summary", {}) if isinstance(resp, dict) else {}
        tiering.observe(db, pol, summ, now, elapsed)   # fold into λ / cost before re-tiering
        _on_success(db, sj, now, policy=pol)
        log_cycle(db, sj.id, job_id, summ, elapsed, baseline=baseline)
        db.commit()
        dispatched += 1
    return dispatched


# ── async shell ───────────────────────────────────────────────────────────────

def _tick_once() -> None:
    """One scheduler tick. THREE switches, not one.

    This checked `master_enabled` alone, and the other two were invisible to it — so Stop and
    maintenance mode did not stop the scheduler at all. Measured live, and it is worth stating
    plainly because every dashboard read as if the halt had worked:

        stop      -> halted=True, 12/12 instances acked, batches ended
        +20s      -> this tick dispatched a NEW site to every free instance
        the crawler ARMS ITSELF on each incoming batch (clear_stop on the way in), so
        `stopping` flipped back to False and the fleet looked like it had simply never stopped

    Ten minutes of that piled teardown on top of fresh dispatch and took the box to load 292 on
    40 cores, while `halted`, `maintenance` and a 12/12 stop ack all read exactly as intended.

    The same fault is already recorded twice here — the auto-refill watcher resuming a halted
    crawl, and the manual-batch loop starting its next chunk after the master switch went off.
    Both were fixed where they were found; this path was never revisited. An operator halt has to
    outlive its own teardown, and that means EVERY loop that can start work re-reads it.
    """
    with SessionLocal() as db:
        # Order matters only for the log: halt is the operator's explicit "stop", maintenance is
        # "hold still while I move things". Either one means this tick starts nothing.
        if halted(db) or maintenance(db) or not master_enabled(db):
            return
        # THE TOGGLE. The tick is a timer, and every timer is opt-in (policy.AUTOS): with
        # `scheduled` off the catalogue schedule is held — due jobs keep their next_run and go out
        # on the first tick after it is switched on, or when the operator presses Run.
        if not auto_on(db, "scheduled"):
            return
        run_due(db)


# The toggle's last known state, so the loop logs the TRANSITION and not every 20 seconds.
_TOGGLE_SEEN: dict[str, bool | None] = {"scheduled": None}


def _note_toggle(name: str, on: bool) -> None:
    if _TOGGLE_SEEN.get(name) != on:
        _TOGGLE_SEEN[name] = on
        log.info("%s: %s", name, "ON — the timer dispatches" if on else
                 "held — the '%s' toggle is off or the pipeline is stopped" % name)


async def loop(stop: asyncio.Event) -> None:
    """Tick every scheduler_tick seconds until `stop` is set. Never dies on an exception."""
    tick = get_settings().scheduler_tick
    log.info("scheduler started (tick=%ss)", tick)
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                _note_toggle("scheduled", auto_on(db, "scheduled"))
            await asyncio.to_thread(_tick_once)  # dispatch can block for minutes; keep the loop free
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    log.info("scheduler stopped")
