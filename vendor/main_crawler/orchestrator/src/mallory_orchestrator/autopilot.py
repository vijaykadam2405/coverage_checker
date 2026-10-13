"""Run the health actions on a timer, so the fleet does not need someone watching it.

Every action here already existed as a BUTTON, and every one of them was only ever pressed when
somebody happened to look. The failures they fix are quiet by nature — an instance idle on a stale
stop flag, work stranded on one container, a crawl halted by a teardown that never cleared — and the
measured cost of noticing late was hours of a twelve-instance fleet running at a fraction of its
rate.

WHAT IT WILL NOT DO. It only ever takes actions that are safe to repeat and cheap to undo:

  * it never STOPS anything, never flushes, and never touches the CORPUS — no stored page, blob or
    catalogue row is ever deleted here. The one thing it does remove is queued URLs the crawler's
    own enqueue filter would reject today (see `traps` below), and only on instances that are not
    crawling; a URL taken out of a queue costs nothing if it was real, because the page linking to
    it offers it again;
  * it obeys the durable operator halt — if the master switch is off, every tick is a no-op, because
    a watchdog that quietly restarts a crawl the operator stopped is worse than no watchdog (this
    exact bug has already shipped twice here);
  * it never rebalances domains on its own. `assign` moves ownership only for FINISHED domains, and
    moving live work needs a stop/resume cycle that must stay a human decision;
  * the one catalogue field it writes is the seed of a site holding ZERO pages (see `seeds` below),
    which by definition cannot disturb a working crawl — and only ever to a URL a probe has just
    confirmed answers.

Cadence is deliberately slow. These are not tile refreshes: `assign` fans out to twelve instances
and `quality` scans the whole corpus, so running them often would put steady load on the very
services they exist to protect. Default 15 minutes, `CRAWLER_AUTOPILOT_MINUTES` to change it,
`CRAWLER_AUTOPILOT=0` to switch it off.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

log = logging.getLogger(__name__)

# Bounded, because the history is served to a browser and read by a human. Older entries are of no
# operational use — if a fault is real it will still be there on the next tick.
HISTORY_MAX = 40
_history: list[dict] = []
_state: dict = {"last_at": None, "next_at": None, "running": False, "ticks": 0}


def enabled() -> bool:
    return os.environ.get("CRAWLER_AUTOPILOT", "1") != "0"


def interval_s() -> int:
    # Floor of 5 minutes: `assign` fans out to twelve instances and the corpus scan is seconds of
    # Postgres. A one-minute autopilot would be a load generator wearing a health-check's name.
    return max(300, int(float(os.environ.get("CRAWLER_AUTOPILOT_MINUTES", "15"))) * 60)


def status() -> dict:
    return {**_state, "enabled": enabled(), "interval_s": interval_s(),
            "history": list(reversed(_history[-HISTORY_MAX:]))}


def _record(action: str, ok: bool, detail: str, changed: bool = False) -> None:
    _history.append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "action": action, "ok": ok, "changed": changed, "detail": detail[:300]})
    del _history[:-HISTORY_MAX]


def run_once() -> dict:
    """One pass. Never raises — this runs on a timer and one bad action must not stop the rest."""
    from . import api, scheduler
    from .db import SessionLocal

    out: dict = {"actions": [], "skipped": None}
    db = SessionLocal()
    try:
        # THE OPERATOR'S STOP WINS, checked before anything else and re-checked nowhere else needed:
        # every action below either starts work or reads. If the fleet is halted, all of them are
        # wrong.
        if scheduler.halted(db):
            out["skipped"] = "halted: the pipeline is stopped by the operator — autopilot does nothing"
            _record("halted", True, out["skipped"])
            return out
        # …and the same for maintenance. An operator moving frontiers between instances needs the
        # fleet to hold still; an automation that resumes crawls underneath them turns a ten-minute
        # job into a fight.
        if scheduler.maintenance(db):
            out["skipped"] = "maintenance mode — autopilot stands down"
            _record("maintenance", True, out["skipped"])
            return out

        # 1. Put idle instances back to work. The cheap, high-value one: this is the fault that
        #    silently costs the most, and the action is idempotent.
        #
        #    ITS OWN TOGGLE (policy.AUTOS "health_dispatch"), separate from the autopilot's. This is
        #    the one action here that STARTS crawls — resume, rebalance, first-crawls — and an
        #    operator who wants the health report every quarter hour does not thereby want a robot
        #    pressing Assign. Skipped is recorded, not silent: an automation that does nothing must
        #    say so, or a fleet left idle by a toggle reads as a fleet the autopilot failed to see.
        if not scheduler.auto_on(db, "health_dispatch"):
            _record("assign", True, "skipped — the 'health_dispatch' toggle is off")
            out["actions"].append({"action": "assign", "skipped": "health_dispatch toggle off"})
        else:
            try:
                r = api.crawler_fleet_assign(db=db)
                changed = bool(r.get("resumed") or r.get("moved") or r.get("started_sites"))
                _record("assign", bool(r.get("ok")), str(r.get("summary") or ""), changed)
                out["actions"].append({"action": "assign", "result": r.get("summary"),
                                       "changed": changed})
            except Exception as e:  # noqa: BLE001
                _record("assign", False, str(e))
                out["actions"].append({"action": "assign", "error": str(e)[:200]})

        # 2. Sweep queued junk. The fault this catches is the one that hid best: on 2026-07-31 the
        #    fleet held 128 of 128 render-lane slots with zero renders in flight and had stored two
        #    pages in an hour, because 14,049 of 19,014 queued URLs were calendar exports and login
        #    recursion — each fetched through a browser, each timing out on the 240s watchdog, each
        #    requeueing itself. Every other number on the dashboard looked busy the whole time.
        #
        #    `apply=True` is safe unattended for one specific reason: an instance running a batch
        #    REFUSES (409) and is reported as skipped, so this never takes a frontier out from under
        #    a live engine. What it does clean is an instance that has gone idle holding junk, which
        #    is where the junk otherwise sits until someone looks. Removed URLs are recoverable in
        #    the only sense that matters — if one were ever real, the page linking to it re-offers it.
        try:
            p = api.crawler_fleet_prune(apply=True)
            _record("traps", bool(p.get("ok")), str(p.get("summary") or ""),
                    bool(p.get("junk")))
            out["actions"].append({"action": "traps", "junk": p.get("junk"),
                                   "result": p.get("summary")})
        except Exception as e:  # noqa: BLE001
            _record("traps", False, str(e))
            out["actions"].append({"action": "traps", "error": str(e)[:200]})

        # 3. Report fleet health. Read-only, but it is what turns "something is wrong" into a named
        #    verdict in the history below, so a human arriving later can see WHEN it started.
        try:
            h = api.crawler_fleet_health()
            bad = h.get("verdict") not in ("ok", "not_crawling")
            _record("health", not bad, "%s — %s" % (h.get("verdict"), h.get("detail") or ""))
            out["actions"].append({"action": "health", "verdict": h.get("verdict")})
        except Exception as e:  # noqa: BLE001
            _record("health", False, str(e))
            out["actions"].append({"action": "health", "error": str(e)[:200]})

        # 4. Re-verify the seeds of catalogued sites that still hold ZERO pages.
        #
        #    A seed probe is a point-in-time measurement, not a permanent verdict. Measured the
        #    afternoon the import-time probe shipped: casic.com.cn answered on
        #    `http://www.casic.com.cn/` during the batch and was unreachable twenty minutes later. A
        #    site that happened to be down at enrolment keeps the conventional `https://<apex>/` and
        #    nothing ever asks again — which is how 92 sources sat at zero pages for months, 28 of
        #    them alive the whole time and merely pointed at the wrong URL.
        #
        #    It writes (a seed, and the schedule's next_run), which is why it runs before `quality`
        #    and not last. Safe unattended for the reason the whole action rests on: ZERO-PAGE sites
        #    only, so it cannot disturb a working crawl, and a probe that finds nothing leaves the
        #    existing seed exactly as it was.
        try:
            # apply_moves stays FALSE here on purpose. A same-site fix (www, a locale path, a
            # subdomain) is a URL correction; a cross-domain one is a claim about who the company
            # now is, and the first live pass showed why that cannot be automatic — pgz.pl, the
            # Polish Armaments Group, resolved to a perfume shop, and sentekglobal.com to
            # deloitte.com. Both answered 200 with a large, entirely real page.
            sd = api.seeds_recheck(limit=25, apply=True, apply_moves=False, db=db)
            fixed = len(sd.get("applied") or [])
            waiting = len(sd.get("proposed") or [])
            _record("seeds", bool(sd.get("ok")), str(sd.get("summary") or ""), bool(fixed))
            out["actions"].append({"action": "seeds", "corrected": fixed,
                                   "proposed": waiting, "result": sd.get("summary")})
        except Exception as e:  # noqa: BLE001
            _record("seeds", False, str(e))
            out["actions"].append({"action": "seeds", "error": str(e)[:200]})

        # 5. Live invariants. Read-only, and the one action here that is looking for faults nobody
        #    has noticed yet rather than fixing a known one. Every check guards a failure that has
        #    already happened in this project AND passed its unit tests — most sharply, an ingest
        #    API answering 200 "accepted" while the row never reached Postgres, which no read-only
        #    dashboard tile could ever show.
        try:
            sc = api.selfcheck_run(db=db)
            _record("invariants", bool(sc.get("ok")), str(sc.get("summary") or ""),
                    bool(sc.get("failed")))
            out["actions"].append({"action": "invariants", "failed": sc.get("failed"),
                                   "not_measured": sc.get("not_measured"),
                                   "result": sc.get("summary")})
        except Exception as e:  # noqa: BLE001
            _record("invariants", False, str(e))
            out["actions"].append({"action": "invariants", "error": str(e)[:200]})

        # 6. Corpus quality. Read-only and the slowest, so it runs LAST — if the process is going to
        #    be interrupted, the actions that change something should already have happened.
        try:
            q = api.data_quality()
            if q.get("reachable") is False:
                _record("quality", False, str(q.get("error") or "store unreachable"))
            else:
                dup = int(q.get("duplicate_content") or 0)
                empty = int((q.get("empty") or {}).get("no_text") or 0)
                _record("quality", True, "%s duplicate, %s with no text" % (dup, empty))
            out["actions"].append({"action": "quality", "reachable": q.get("reachable")})
        except Exception as e:  # noqa: BLE001
            _record("quality", False, str(e))
            out["actions"].append({"action": "quality", "error": str(e)[:200]})
    finally:
        db.close()
        _state["ticks"] += 1
        _state["last_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return out


def tick() -> dict | None:
    """One TIMER pass, or None when the `autopilot` toggle (policy.AUTOS) holds it.

    The loop calls this; the Run-now button calls run_once directly, because a button is the
    operator asking. `auto_on` is the effective value — off while stopped whatever the toggle
    says — so a stopped fleet gets no autopilot pass by this path either (run_once ALSO stands down
    on the stop, so the button cannot restart anything).
    """
    from . import scheduler
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if not scheduler.auto_on(db, "autopilot"):
            return None
    finally:
        db.close()
    return run_once()


async def loop(stop: asyncio.Event) -> None:
    if not enabled():
        log.info("autopilot disabled by CRAWLER_AUTOPILOT=0")
        return
    tick_s = interval_s()
    log.info("autopilot started (every %ss) — held until the 'autopilot' toggle is on", tick_s)
    held: bool | None = None
    # A first pass on boot would fire during startup, while instances are still coming up, and
    # report a fleet-wide fault that is really just the stack booting. Wait one interval.
    while not stop.is_set():
        _state["next_at"] = (dt.datetime.now(dt.timezone.utc)
                             + dt.timedelta(seconds=tick_s)).isoformat(timespec="seconds")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_s)
            return                                  # stop was set — leave without a final tick
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        _state["running"] = True
        try:
            # to_thread: every action here is blocking HTTP against twelve instances plus a corpus
            # scan, and this shares the event loop with the API the dashboard is polling.
            r = await asyncio.to_thread(tick)
            if (r is None) != held:                 # the toggle, logged on the transition
                held = r is None
                log.info("autopilot: %s", "held — the 'autopilot' toggle is off or the pipeline "
                         "is stopped" if held else "the 'autopilot' toggle is on")
        except Exception:
            log.exception("autopilot tick failed")
        finally:
            _state["running"] = False
    log.info("autopilot stopped")
