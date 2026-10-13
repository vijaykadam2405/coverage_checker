"""Keep an idle crawler instance working instead of letting it sit.

A batch is split across the fleet by domain, and domains are wildly unequal — one site today held
16,000 pages while most hold under a hundred. So instances finish at very different times, and the
first one done has historically just stopped: measured on the live fleet, TWO of seven instances
were idle while holding ~12,000 pending URLs between them, because their share of the batch had
ended while work remained. Restarting them by hand took the fleet from 10.7 to 16.7 pages/s.

That is the whole job here. Every tick, for each instance:

  * if it is crawling, leave it alone
  * if it is idle AND holds saved frontiers, resume them — its own, on its own container, so
    nothing moves and nothing is re-crawled

It deliberately does NOT move work between instances. A domain's frontier — its pending URLs, its
already-seen set, its spent budget — lives inside one container's sqlite with no export path, so
"stealing" a site from a busy instance would restart it from the seed while the real progress sat
unreachable next door. Idle instances get their OWN unfinished work, which is the part that is both
free and safe. Moving finished domains onto new instances is a separate, deliberate operation
(shard.rebalance).

Off by default is wrong here — an idle crawler is pure waste — so it is ON, with CRAWLER_AUTO_REFILL=0
to disable and CRAWLER_REFILL_TICK_S to slow it down.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import crawler_client

log = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("CRAWLER_AUTO_REFILL", "1") != "0"


def refill_once(max_jobs: int = 200, force: bool = False) -> dict:
    """Resume saved frontiers on every instance that is idle. Returns what it did.

    Never raises: this runs on a timer beside the scheduler, and a crawler hiccup must not kill the
    loop that exists to keep the fleet busy.

    `force` only changes the treatment of a lingering `stop_pending`, and exists for the operator's
    "check the fleet and put it back to work" button. On the timer that flag must be respected — it
    is how a deliberate stop survives — but a flag left set by a teardown that finished long ago is
    the single most common reason an instance sits idle holding a full frontier, and the operator
    pressing the button IS the decision to override it. The durable operator halt below is checked
    either way and is NOT overridable here; that one is the real stop.
    """
    out: dict = {"checked": 0, "idle": 0, "resumed": 0, "instances": [], "errors": []}

    # THE OPERATOR'S STOP WINS, and it has to be checked here rather than relying on the crawler's
    # own flag. `stop_pending` below is true only WHILE an instance is tearing down; once that
    # finishes it reads as plain idleness, and this loop then resumed a crawl that had been halted
    # on purpose — measured, about a minute after the stop completed. A watcher that quietly undoes
    # an operator's decision is worse than no watcher, so the durable flag is consulted first.
    from . import scheduler
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if scheduler.halted(db):
            out["halted"] = True
            return out
        # MAINTENANCE WINS OVER `force`, and that is the whole point of it. `force` is the operator
        # saying "put the fleet back to work"; maintenance is the operator saying "hold still while
        # I move the frontiers". Refill resuming an instance twelve seconds after a fleet-wide stop
        # is what made the prune and the move refuse with 409 on eleven of twelve instances.
        if scheduler.maintenance(db):
            out["maintenance"] = True
            return out
        # THE DISPATCH POLICY, and `force` does not lift it either. Resuming a saved frontier is
        # how the backlog came back after every stop the operator pressed — via this timer, via
        # the Assign button, via the autopilot pressing Assign, via maintenance-off — so this is
        # where "run only the daily lane" has to bite, whoever is asking.
        if not scheduler.lane_open(db, "resume"):
            out["lane_closed"] = "resume"
            out["reason"] = scheduler.why_closed(db, "resume")
            return out
    finally:
        db.close()

    bases = crawler_client.pool()
    # THE FLEET CONCURRENCY CAP BINDS HERE TOO, or it is decorative: the scheduler and this loop are
    # two independent things that put instances to work, and capping only one just moves the
    # saturation to the other. Measured at 11-12 instances crawling: load 201, 38.3 of 40 cores
    # ours against a 30-core budget, and throughput DOWN from 190 pages/min to 22.
    busy = 0
    for base in bases:
        try:
            if bool((crawler_client.batch_status(base=base) or {}).get("running")):
                busy += 1
        except Exception:                      # noqa: BLE001 — counted as not-busy; the per-instance
            pass                               # loop below reports its own errors
    room = scheduler.MAX_BUSY_INSTANCES - busy if scheduler.MAX_BUSY_INSTANCES > 0 else len(bases)
    if room <= 0:
        # AT CAPACITY MEANS THIS PASS CHECKS NOTHING, and that has to be visible. With the cap at 7
        # and 10 instances crawling, room was permanently negative: every tick returned here, the
        # loop below only logs when it RESUMES something, and so the watcher produced zero log lines
        # for days while the fleet drained 12 -> 9 -> 7. An instance that finished was never
        # restarted, because the cap only falls when even more instances go idle. The fleet could
        # only ever shrink, and nothing said so.
        out["at_capacity"] = busy
        out["room"] = room
        return out

    # NEWS FIRST — and this is the THIRD dispatch path, after scheduler._dispatch_fleet and
    # orchestrate._dispatch_routed. This watcher is the one that would have silently undone the
    # other two: it resumes a saved frontier on ANY idle instance within a minute, so the moment a
    # lane instance finished its catalogue work it would be handed straight back to the catalogue
    # and the daily lane — which ticks no faster — would have to win a race to get it. A hold that
    # covers two paths out of three is not a hold.
    #
    # `force` DOES NOT LIFT THIS, and that distinction is the whole fix. `force` exists to override
    # a stale `stop_pending` left by a teardown that finished long ago — a dead flag. The daily
    # hold is not a dead flag, it is a live reservation, and the two are only the same thing if you
    # read `force` as "a human decided". It is not: api.crawler_fleet_assign calls this with
    # force=True and the AUTOPILOT presses that button on a 900s timer. Measured — with the hold
    # exempting force, the autopilot resumed 17 catalogue jobs onto a lane instance minutes after
    # they were stopped for the daily sweep, and did it again every quarter of an hour. A carve-out
    # for "the operator" that an automation walks through is not a carve-out, it is a hole.
    #
    # The reservation is transient by construction: it lasts only while a daily site is due, which
    # is minutes a day once the list is warm. An operator who genuinely needs these instances back
    # empties the lane (CRAWLER_DAILY_INSTANCES) rather than racing it.
    held: set[int] = set()
    try:
        from . import daily as _daily
        from .db import SessionLocal as _SL
        reserved = _daily.reserved_instances()
        if reserved and not _daily.reservation()["exclusive"]:
            with _SL() as _db:
                if _daily.holds_fleet(_db):
                    held = reserved
    except Exception:  # noqa: BLE001 — the lane must never be able to stop the refill watcher
        log.exception("daily-lane hold check failed; refilling the whole fleet")

    # THE MODE OWNS ITS FRONTIERS. /v1/resumable lists everything an instance holds — a campaign's
    # half-walked competitor sites next to the catalogue's. Resuming all of it is what "revived
    # backlog" means: stop the campaign, start the normal crawl, and a minute later the campaign is
    # back because it was the biggest thing idle. So only the current mode's frontiers are resumed;
    # the other mode's are PARKED — counted and reported, never dropped — and come back when that
    # mode is active again. Classified once per pass, not once per instance.
    sdb = SessionLocal()
    try:
        scope = lambda ids: scheduler.owned_by_mode(sdb, ids)  # noqa: E731
        return _resume_idle(out, bases, held, room, busy, max_jobs, force, scope)
    finally:
        sdb.close()


def _resume_idle(out: dict, bases: list[str], held: set[int], room: int, busy: int,
                 max_jobs: int, force: bool, scope) -> dict:
    for i, base in enumerate(bases):
        out["checked"] += 1
        if i in held:
            # Reported, not silent: an instance sitting idle on purpose looks identical to one the
            # watcher failed to notice, and that is exactly the confusion this file exists to end.
            out["held_for_daily"] = out.get("held_for_daily", 0) + 1
            continue
        if out["resumed"] and room <= 0:
            out["at_capacity"] = busy
            break
        try:
            st = crawler_client.batch_status(base=base) or {}
            if bool(st.get("running")):
                continue                      # working — leave it alone
            # A STOP IS NOT IDLENESS. The crawler's stop signal is a file that outlives the batch,
            # so an instance the operator just halted looks exactly like one that ran out of work —
            # and resuming it would undo the stop within a minute, silently. That is the same
            # "something restarted the crawl behind my back" failure as the multiprocess stop-file
            # bug and the chunk loop before it; a watcher that fights the operator is worse than no
            # watcher. The flag clears when a new batch is dispatched on purpose.
            if bool(st.get("stop_pending")) and not force:
                out["stopped"] = out.get("stopped", 0) + 1
                continue
            jobs = (crawler_client.resumable(base=base) or {}).get("jobs") or []
            # A JOB WITH NO SAVED CONFIG CAN NEVER BE RESUMED, and asking anyway is an infinite
            # retry. /v1/resumable lists anything holding pending frontier rows, but resume replays
            # the job's stored definition — and when that is missing the endpoint puts the id in
            # `missing_config` and, if NONE of the ids have config, answers 400. Seen live on five
            # jobs across the fleet (news_marinecorpstimes_com, recrawl_army_mil, news_navalnews_com,
            # recrawl_avinc_com, recrawl_bt-ag_ch).
            #
            # Unfiltered, an instance whose only leftovers are these would be counted as idle-with-
            # work every tick, fail, and be counted again a minute later, forever — busy-looking and
            # permanently idle. The endpoint already reports has_config per row; this is just the
            # caller finally reading it.
            usable = [j for j in jobs if j.get("job_id") and j.get("has_config")]
            stranded = len(jobs) - len(usable)
            if stranded:
                out["stranded"] = out.get("stranded", 0) + stranded
            mine = scope([str(j["job_id"]) for j in usable])
            parked = len(usable) - len(mine)
            if parked:
                out["parked"] = out.get("parked", 0) + parked
            ids = mine[:max_jobs]
            if not ids:
                # Genuinely finished, OR holding only un-resumable leftovers. Either way there is
                # nothing this watcher can do, and it must not report work it cannot start.
                continue
            out["idle"] += 1
            res = crawler_client.resume(ids, background=True, forward_to_ingest=True, base=base)
            n = len(res.get("resumed") or [])
            out["resumed"] += n
            room -= 1                          # this instance is now crawling; spend the budget
            out["instances"].append({"instance": i, "resumed": n,
                                     "pending": sum(int(j.get("pending") or 0) for j in jobs)})
            log.info("auto-refill: instance %d was idle, resumed %d saved job(s)", i, n)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"instance": i, "error": str(e)[:200]})
    return out


def tick() -> dict | None:
    """One TIMER pass, or None when the `refill` toggle (policy.AUTOS) holds it.

    The loop calls this and nothing else does: the Assign button, the Refill button and
    maintenance-off call refill_once directly, because a button is the operator asking and a toggle
    is a permission for a timer. `auto_on` is the effective value — off while stopped whatever the
    toggle says — so a stopped fleet is never refilled by this path either.
    """
    from . import scheduler
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if not scheduler.auto_on(db, "refill"):
            return None
    finally:
        db.close()
    return refill_once()


async def loop(stop: asyncio.Event) -> None:
    """Tick until `stop` is set. Never dies on an exception."""
    # DEFERRED, like refill_once's: scheduler imports this module at load time, so a module-level
    # `from . import scheduler` here is a circular import. Referencing scheduler.* in this function
    # without it is a NameError on the FIRST line of the loop — which kills the task silently,
    # because an exception in a bare asyncio task surfaces nowhere until someone awaits it. Done
    # exactly that once already.
    from . import scheduler

    tick_s = max(30, int(os.environ.get("CRAWLER_REFILL_TICK_S", "60")))
    if not enabled():
        log.info("auto-refill disabled by CRAWLER_AUTO_REFILL=0")
        return
    log.info("auto-refill started (tick=%ss, max_busy=%s) — held until the 'refill' toggle is on",
             tick_s, scheduler.MAX_BUSY_INSTANCES)
    capped = False
    closed = False
    held: bool | None = None
    while not stop.is_set():
        try:
            # to_thread: resume() can block while a crawler reloads a large frontier, and this loop
            # shares the event loop with the API.
            r = await asyncio.to_thread(tick)
            # THE TOGGLE, logged on the transition like the other holds below.
            if (r is None) != held:
                held = r is None
                log.info("auto-refill: %s", "held — the 'refill' toggle is off or the pipeline is "
                         "stopped" if held else "the 'refill' toggle is on, resuming idle instances")
            if r is None:
                raise _Held
            if r["resumed"]:
                log.info("auto-refill: %d job(s) resumed across %d idle instance(s)",
                         r["resumed"], r["idle"])
            # Same rule as the capacity line below: a watcher held by the dispatch policy says so
            # once, on the transition, so a frozen fleet is not silent.
            if bool(r.get("lane_closed")) != closed:
                closed = not closed
                log.info("auto-refill: the 'resume' lane is %s by the dispatch policy",
                         "CLOSED — nothing will be resumed" if closed else "open again")
            # A WATCHER THAT DOES NOTHING MUST SAY SO. Logging only on success made a permanently
            # capped refill indistinguishable from a healthy one — both silent. Logged on the
            # TRANSITION rather than every tick, so it stays one line per state change and does not
            # bury the resume lines it sits next to.
            if bool(r.get("at_capacity")) != capped:
                capped = not capped
                if capped:
                    log.warning("auto-refill AT CAPACITY: %s instance(s) busy vs max_busy=%s — "
                                "nothing will be refilled until that drops, so any instance that "
                                "finishes stays idle", r.get("at_capacity"),
                                scheduler.MAX_BUSY_INSTANCES)
                else:
                    log.info("auto-refill: back under capacity, resuming normal checks")
        except _Held:
            pass
        except Exception:
            log.exception("auto-refill tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
        except asyncio.TimeoutError:
            pass
    log.info("auto-refill stopped")


class _Held(Exception):
    """Control flow only: the toggle held this pass, skip the bookkeeping below the call."""
