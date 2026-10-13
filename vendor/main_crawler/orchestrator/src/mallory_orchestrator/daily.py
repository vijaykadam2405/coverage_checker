"""The DAILY lane: a fixed URL list, crawled once a day, on a small set of crawler instances.

The catalogue scheduler and this lane want opposite things. The catalogue wants throughput: 500+
sites spread over the fleet, each crawl running to frontier exhaustion, some for hours. The daily
list wants punctuality: 37 news sites that publish several times a day, each of which is worthless
if it is fetched a day late. Run them in one queue and punctuality always loses, because a six-hour
catalogue crawl holds the instance and every daily site behind it slips.

So the daily list gets instances of its own - `CRAWLER_DAILY_INSTANCES`, e.g. "0,1,2" - and the
sites are dispatched across them CONCURRENTLY, one per free instance per tick.

There are two ways to own an instance, and which one is in force is a real decision with no free
answer (see config.daily_exclusive):

  * EXCLUSIVE. The lane's instances never take catalogue work: `shard.allowed_instances` refuses to
    assign a domain there. Punctuality is guaranteed and the instances idle once the list is clear.

  * PRIORITY (the default). The lane's instances also own catalogue domains, so once the daily list
    is clear for the day they rejoin the fleet and work the backlog. While any daily site is due,
    `orchestrate._dispatch_routed` HOLDS catalogue work bound for them - news first, backlog after.
    The cost is bounded and must be stated rather than discovered: a catalogue crawl already in
    flight is never interrupted, so the day's first sweep can be delayed by up to the length of
    that crawl. Rows keep their next_run and fire on the first free tick, so a delay is never a
    lost day - and `overdue()` counts the ones it happened to, so the trade is visible.

Same shape as scheduler.py, deliberately: every decision function is SYNC and takes an explicit
`now`/`db`, with injectable `dispatch`/`busy_check`, so the whole lane is testable with no HTTP and
no asyncio. Only `loop()` is async.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import threading
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import categories, crawler_client, scheduler, shard
from .config import daily_exclusive_enabled, daily_instance_list, get_settings
from .db import SessionLocal
from .models import DailyUrl, DomainShard

log = logging.getLogger("mallory.daily")

# How many sites one tick may dispatch. dispatch() BLOCKS for the whole crawl, so this is really
# "how long a tick may run before the master switch and the due-list are re-read", not a rate.
MAX_DISPATCH_PER_TICK = 5

# A failed site retries in an hour rather than waiting out the full day: a news site that was down
# for one fetch is the ordinary case, and losing a whole day of it to a single timeout is not.
RETRY_AFTER_S = 3600

# Bounded by max_pages, not by depth: these are news sites where new articles sit behind
# pagination, so a depth cap silently truncates the very thing the lane exists to collect.
DAILY_MAX_PAGES = 300


def _job_defaults() -> dict:
    """The crawl shape for a daily re-visit. `incremental` is what makes it cheap - unchanged pages
    answer 304 and skip the render entirely."""
    return {
        "job_type": "news",
        "keyword": False,
        "keywords": [],
        "render_js": False,
        "path_scope": [],
        "max_pages": DAILY_MAX_PAGES,
        "max_depth": -1,
        "freshness_days": 7,
        "same_domain_only": True,
        "incremental": True,
        "capture": ["html", "text"],
    }


# -- the reservation ---------------------------------------------------------------------------

def reservation() -> dict:
    """Which instances this lane runs on, and whether they are really its own.

    `shared` is the honest case that must not be hidden: claiming every instance of the pool means
    the catalogue and the daily list are on the same containers after all, and the lane still runs -
    it just cannot promise punctuality. Reporting that as a plain boolean is the difference between
    a documented limitation and a mystery.

    Indices past the end of the pool are dropped, not clamped: clamping would silently point the
    lane at a container that holds a different site's frontier. They are named in `missing` so a
    typo in CRAWLER_DAILY_INSTANCES is visible instead of being quietly absorbed.
    """
    s = get_settings()
    bases = crawler_client.pool()
    want = daily_instance_list(s)
    if not want:
        return {"enabled": False, "instances": [], "bases": [], "missing": [], "pool": len(bases),
                "shared": False, "exclusive": daily_exclusive_enabled(s),
                "reason": "CRAWLER_DAILY_INSTANCES unset"}
    idx = [i for i in want if i < len(bases)]
    missing = [i for i in want if i >= len(bases)]
    if not idx:
        return {"enabled": False, "instances": [], "bases": [], "missing": missing,
                "pool": len(bases), "shared": False, "exclusive": daily_exclusive_enabled(s),
                "reason": "no requested instance exists: the pool has %d" % len(bases)}
    return {"enabled": True, "instances": idx, "bases": [bases[i] for i in idx],
            "missing": missing, "pool": len(bases), "shared": len(idx) >= len(bases),
            "exclusive": daily_exclusive_enabled(s),
            "reason": ("instance(s) %s do not exist: the pool has %d"
                       % (missing, len(bases))) if missing else None}


def reserved_bases() -> list[str]:
    return reservation()["bases"]


def reserved_instances() -> set[int]:
    """The instance indices the lane runs on. Used by the batch dispatcher to hold catalogue work
    back while the list is due."""
    return set(reservation()["instances"])


# -- the list ----------------------------------------------------------------------------------

def list_path() -> Path:
    return Path(get_settings().seed_dir) / "daily_urls.txt"


def parse_list(text: str) -> list[tuple[str, str]]:
    """(url, note) per line. '#' starts a comment, whole-line or trailing; blanks ignored."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, _, note = line.partition("#")
        url = url.strip()
        if url:
            out.append((url, note.strip()))
    return out


def sync_from_file(db: Session, now: dt.datetime | None = None,
                   path: Path | None = None) -> dict:
    """Make the table match the file. Idempotent - safe to call on every startup.

    A URL dropped from the file is DISABLED, never deleted, so its run history survives an edit and
    a site removed by mistake comes back with its schedule intact instead of as a fresh baseline.

    First-ever rows are spread evenly across the next 24 hours rather than all set to `now`. The
    lane is sequential, so a thundering herd would not overload anything - it would just mean every
    site's crawl hour is decided by its position in a queue on one particular day. Spreading gives
    each site a stable hour of its own.
    """
    now = now or scheduler.utcnow()
    path = path or list_path()
    if not path.exists():
        return {"file": str(path), "found": False, "added": 0, "reenabled": 0,
                "disabled": 0, "total": 0}
    entries = parse_list(path.read_text(encoding="utf-8"))
    wanted = {u for u, _ in entries}
    existing = {r.url: r for r in db.scalars(select(DailyUrl)).all()}
    added = reenabled = disabled = 0
    step = dt.timedelta(seconds=86400 / max(1, len(entries)))
    for i, (url, note) in enumerate(entries):
        row = existing.get(url)
        if row is None:
            db.add(DailyUrl(url=url, domain=shard.host_of(url), note=note or None,
                            enabled=True, next_run=now + step * i, created_at=now))
            added += 1
            continue
        row.note = note or row.note
        if not row.enabled:
            row.enabled = True
            row.next_run = row.next_run or now
            reenabled += 1
    for url, row in existing.items():
        if url not in wanted and row.enabled:
            row.enabled = False
            disabled += 1
    db.commit()
    if added or reenabled or disabled:
        scheduler.log_event(
            db, "daily_sync",
            detail="%d added, %d re-enabled, %d removed from list" % (added, reenabled, disabled))
        db.commit()
    pin = pin_to_lane(db)
    return {"file": str(path), "found": True, "added": added, "reenabled": reenabled,
            "disabled": disabled, "total": len(entries), "pinned": pin}


def pin_to_lane(db: Session, res: dict | None = None, *, move: bool = True) -> dict:
    """Point every daily domain's shard at one of the lane's instances, and keep it there.

    `move=False` CLAIMS domains nobody owns yet but leaves a domain owned elsewhere where it is —
    the tick's behaviour while the `repin` toggle is off. A claim is not a re-pin: it undoes no
    rebalance, it only gives a newly listed site an owner so it can be dispatched at all. Domains
    left elsewhere are counted in `drifted` so the tick can say they are undispatchable.

    WITHOUT THIS THE LANE CANNOT BE INCREMENTAL, and the first live run proved it. A site's frontier
    and its crawl history live inside ONE instance's data directory — that is the whole reason
    domain_shards exists. The lane, though, dispatched each due site to whichever of its instances
    happened to be free, so a domain owned by instance 7 was crawled on instance 1, where there was
    no history to answer 304 with. Measured on the first dispatch: infodefensa.com (owner 7, sent to
    1) was still walking an hour later and blew the 3600s dispatch timeout, and only 10 of the 37
    daily domains were owned by a lane instance at all. Left alone it would not have settled — a
    site bounces between 0, 1 and 2 by whoever is free, so it re-walks from the seed EVERY day and
    never accumulates history anywhere.

    The assignment is a hash, so it is stable and spread: the same domain lands on the same lane
    instance every day, which is exactly what `incremental` needs.

    THIS MOVES DOMAINS, and moving a domain strands its existing frontier in the old container (the
    same cost `shard.rebalance` documents). For these sites that is a bounded, one-time price: they
    are news pages behind a 7-day freshness gate and a 300-page cap, so the stranded frontier is a
    day of listing pages, not a half-walked site. It is paid once per domain, against a re-walk that
    was otherwise going to be paid every single day.
    """
    res = res or reservation()
    lane = res["instances"]
    if not lane:
        return {"moved": 0, "kept": 0, "lane": [], "drifted": []}
    moved = kept = 0
    drifted: list[str] = []
    for row in db.scalars(select(DailyUrl).where(DailyUrl.enabled.is_(True))).all():
        if not row.domain:
            continue
        cur = db.get(DomainShard, row.domain)
        if cur is not None and cur.instance in lane:
            kept += 1
            continue
        if cur is not None and not move:
            drifted.append(row.domain)
            continue
        d = hashlib.blake2b(row.domain.encode("utf-8", "ignore"), digest_size=8).digest()
        idx = lane[int.from_bytes(d, "big") % len(lane)]
        if cur is None:
            db.add(DomainShard(domain=row.domain, instance=idx))
        else:
            cur.instance = idx
        moved += 1
    db.commit()
    if moved:
        scheduler.log_event(db, "daily_pinned",
                            detail="%d domain(s) re-pointed at lane instance(s) %s, %d already there"
                                   % (moved, lane, kept))
        db.commit()
    return {"moved": moved, "kept": kept, "lane": lane, "drifted": drifted}


def owner_of(db: Session, row: DailyUrl) -> int | None:
    """Which instance holds this site's frontier. None when nothing has been assigned — the caller
    treats that as "not dispatchable yet" rather than guessing, because guessing is what sent
    infodefensa.com to an instance that had never seen it."""
    if not row.domain:
        return None
    r = db.get(DomainShard, row.domain)
    return None if r is None else r.instance


# -- the tick ----------------------------------------------------------------------------------

def due(db: Session, now: dt.datetime) -> list[DailyUrl]:
    """Enabled rows whose day has come, most overdue first."""
    return list(db.scalars(
        select(DailyUrl)
        .where(DailyUrl.enabled.is_(True),
               DailyUrl.next_run.is_not(None),
               DailyUrl.next_run <= now)
        .order_by(DailyUrl.next_run)
    ).all())


def has_due(db: Session, now: dt.datetime | None = None) -> bool:
    """Is there daily work waiting? The signal `orchestrate._dispatch_routed` holds catalogue work
    on. Cheap by design - it runs on every batch chunk, so it is an EXISTS, not a full fetch."""
    now = now or scheduler.utcnow()
    return db.scalars(
        select(DailyUrl.url)
        .where(DailyUrl.enabled.is_(True),
               DailyUrl.next_run.is_not(None),
               DailyUrl.next_run <= now)
        .limit(1)
    ).first() is not None


def holds_fleet(db: Session, now: dt.datetime | None = None) -> bool:
    """Should catalogue work be held off the lane's instances right now? `has_due` AND the lane is
    actually allowed to run. In campaign mode, or while stopped, the daily list may well be due —
    but it is not going to be dispatched, so holding instances for it would be holding them for
    nothing. This is the signal the three catalogue dispatchers read (scheduler._dispatch_fleet,
    orchestrate._dispatch_routed, refill.refill_once)."""
    return scheduler.lane_open(db, "daily") and has_due(db, now)


def build_job(row: DailyUrl) -> dict:
    job = _job_defaults()
    job["job_id"] = "daily_" + (row.domain or "site").replace(".", "_")
    job["seed_urls"] = [row.url]
    job["source_id"] = row.domain
    job["forward_to_ingest"] = True
    return job


def bucket_of(row: DailyUrl, by_source: dict[str, str]) -> str:
    """The category switch that governs this site, via the catalogue, exactly as the main scheduler
    resolves it. A domain the catalogue does not hold comes back UNCATALOGUED - which is itself a
    switchable bucket, so it is visible rather than exempt."""
    return categories.bucket_of_job({"source_id": row.domain}, by_source)


def _busy(base: str | None) -> bool:
    """True if this instance is mid-batch. Unreachable counts as not busy, so the dispatch below
    fails loudly and the row retries, instead of the lane going quiet on a dead container."""
    try:
        return bool(crawler_client.batch_status(base=base).get("running"))
    except Exception:  # noqa: BLE001
        return False


def free_instances(res: dict | None = None, busy_check=None) -> list[int]:
    """The lane's instances that can take a site right now.

    In PRIORITY mode an instance can legitimately be mid-crawl on catalogue work when the daily
    list comes due. That crawl is not interrupted - killing it would strand a frontier mid-walk -
    so the lane simply uses whichever of its instances are free and leaves the rest for the next
    tick. With three instances the odds of all three being blocked at once are small; with one they
    were the whole risk.
    """
    res = res or reservation()
    if not res["enabled"]:
        return []
    check = busy_check or (lambda i, b: _busy(b))
    pairs = list(zip(res["instances"], res["bases"]))
    if not pairs:
        return []
    with ThreadPoolExecutor(max_workers=len(pairs)) as ex:
        busy = list(ex.map(lambda p: check(p[0], p[1]), pairs))
    return [i for (i, _), b in zip(pairs, busy) if not b]


def _batch(base: str | None) -> dict:
    try:
        return crawler_client.batch_status(base=base) or {}
    except Exception:  # noqa: BLE001 — an unreachable instance is not preemptible; say nothing
        return {}


def preempt_for_due(db: Session, rows: list[DailyUrl], res: dict, *,
                    status=None, stop=None) -> list[int]:
    """Stop CATALOGUE work on a lane instance that a due defence site is waiting for.

    Defence news runs first, always. Holding new catalogue work off the lane was not enough on its
    own: a crawl already in flight is never interrupted, so the day's first sweep queued behind
    whatever was running — measured, an 18-job batch on one instance and a six-hour gov-portal walk
    on another, and the sweep only moved once those were stopped BY HAND.

    Stopping is safe and reversible, which is the only reason this is allowed to be automatic. The
    crawler's stop is graceful: it drains and writes the frontier to disk, and the job comes back in
    /v1/resumable with its config. Verified on the live fleet before this was written — 19 frontiers
    on one instance survived a stop intact, and the auto-refill watcher puts them back once no
    defence site is due (which is what the refill hold guarantees).

    Four things it will not do, each of which would be a different bug:
      * never stop another DAILY crawl — that is the lane eating itself;
      * never stop an instance already draining (`stop_pending`), or a 60s tick would re-issue the
        stop every minute and the batch would never finish tearing down;
      * never stop an instance no due site is actually waiting for — a stop has a real cost and
        paying it for nothing is worse than waiting;
      * never stop for a site a category switch is holding, which is why the switch filter runs
        BEFORE this does.
    """
    lane = dict(zip(res["instances"], res["bases"]))
    st = status or _batch
    kill = stop or (lambda base: crawler_client.stop_crawl(base=base))
    waiting = {owner_of(db, r) for r in rows} & set(lane)
    out: list[int] = []
    for inst in sorted(waiting):
        s = st(lane[inst])
        if not s.get("running") or s.get("stop_pending"):
            continue
        if str(s.get("current_job") or "").startswith("daily_"):
            continue
        try:
            kill(lane[inst])
        except Exception as e:  # noqa: BLE001 — a failed stop must not stop the tick
            log.warning("could not preempt instance %d: %s", inst, e)
            continue
        out.append(inst)
    return out


def overdue(db: Session, now: dt.datetime | None = None, hours: float = 24.0) -> list[DailyUrl]:
    """Due rows that have been waiting longer than a whole cadence.

    Priority mode trades a bounded delay for the instances rejoining the backlog, and a trade is
    only acceptable while you can see what it costs. This is that number: sites the lane has been
    unable to reach because its instances stayed busy.
    """
    now = now or scheduler.utcnow()
    cut = now - dt.timedelta(hours=hours)
    return [r for r in due(db, now) if r.next_run and r.next_run <= cut]


def run_due(db: Session, *, now: dt.datetime | None = None, dispatch=None, busy_check=None,
            max_dispatch: int | None = MAX_DISPATCH_PER_TICK,
            preempt=None, preempt_status=None, preempt_stop=None) -> int:
    """Crawl every due site across the lane's instances, in parallel. Returns #dispatched.

    `dispatch` is called as dispatch(job, base) so a test can assert which instance each site went
    to; the single-argument form the old single-instance lane used is still accepted.
    """
    now = now or scheduler.utcnow()
    res = reservation()
    if not res["enabled"]:
        return 0

    # THE ONE GATE, before anything else — before the re-pin and before preempt_for_due, which
    # STOPS other work. A closed daily lane must not move shard rows or kill catalogue crawls on the
    # way to dispatching nothing.
    if not scheduler.lane_open(db, "daily"):
        if has_due(db, now):
            scheduler.log_event(db, "daily_skipped_lane_closed",
                                detail="the 'daily' lane is closed: %s"
                                       % scheduler.why_closed(db, "daily"))
            db.commit()
        return 0

    # RE-PIN EVERY TICK, not just at startup. The pin is not a one-off migration because something
    # else keeps undoing it: shard.rebalance re-hashes finished domains across the WHOLE fleet, and
    # api.crawler_fleet_assign calls it whenever an instance is idle-and-empty — which the autopilot
    # does on a timer. Measured after ~12 hours: 22 of the 37 daily domains had drifted back onto
    # instances 3-11, and because a site is only ever dispatched to its owner, all 22 had become
    # UNDISPATCHABLE — 18 of the 25 sites then due could not run at all while two lane instances sat
    # idle. The lane looked slow; it was actually starved, and nothing said so.
    #
    # Re-pinning here rather than teaching rebalance about the lane is deliberate: it is 37 rows and
    # a no-op once they are in place (`moved` is 0), and it is correct against ANY future mover
    # rather than the one that happens to exist today.
    #
    # MOVING is a TOGGLE (policy.AUTOS "repin"), off by default like every other timer-driven
    # mover: re-pinning moves shard rows, and a mover that runs every minute was what undid every
    # deliberate rebalance. CLAIMING a domain nobody owns yet always happens — a line just added to
    # daily_urls.txt must get an owner or it sits undispatchable for ever, and a claim undoes
    # nothing. With the toggle off the lane still re-pins ONCE at boot (sync_from_file) and on
    # every switch into normal mode (the API), and a due site that has drifted off the lane is
    # REPORTED rather than silently left undispatchable.
    pin = pin_to_lane(db, res, move=scheduler.auto_on(db, "repin"))

    rows = due(db, now)
    if not rows:
        return 0
    drifted = sorted(set(pin.get("drifted") or []) & {r.domain for r in rows})
    if drifted:
        scheduler.log_event(db, "daily_unpinned",
                            detail="%d due site(s) are owned by a non-lane instance and cannot "
                                   "be dispatched: %s — switch the 'repin' toggle on, or "
                                   "re-enter normal mode to pin once"
                                   % (len(drifted), ", ".join(drifted[:8])))
        db.commit()
    # The category switches govern this lane too. Every site on the daily list is a news site, so a
    # lane that ignored them would keep crawling News on the reserved instance minutes after the
    # operator switched News off — the switch would be true of the catalogue and false of the
    # dashboard. Held rows keep their next_run, so they fire on the first tick after the category
    # comes back rather than losing the day.
    off = scheduler.disabled_categories(db)
    if off:
        by_source = categories.catalog_buckets(db)
        held = [r for r in rows if bucket_of(r, by_source) in off]
        if held:
            rows = [r for r in rows if bucket_of(r, by_source) not in off]
            names = ", ".join(sorted({categories.label(bucket_of(r, by_source)) for r in held}))
            scheduler.log_event(db, "daily_skipped_category",
                                detail="%d site(s) held: %s disabled" % (len(held), names))
            db.commit()
        if not rows:
            return 0

    free = free_instances(res, busy_check=busy_check)

    # DEFENCE FIRST, ALWAYS. Anything still holding an instance a due site is waiting for gets
    # stopped; see preempt_for_due for why that is safe to do automatically.
    if preempt is not False:
        stopped = preempt_for_due(db, rows, res, status=preempt_status, stop=preempt_stop)
        if stopped:
            scheduler.log_event(
                db, "daily_preempted",
                detail="stopped catalogue work on instance(s) %s: a due defence site owns them"
                       % stopped)
            db.commit()

    if not free:
        late = overdue(db, now)
        scheduler.log_event(
            db, "daily_skipped_busy",
            detail="%d site(s) due, %d overdue by a full day; all %d lane instance(s) busy"
                   % (len(rows), len(late), len(res["instances"])))
        db.commit()
        return 0

    # EACH SITE GOES TO THE INSTANCE THAT OWNS IT, or it waits. Pairing due rows with free
    # instances positionally — which is what this did — sends a site to a container holding neither
    # its frontier nor its crawl history, so `incremental` has nothing to compare against and the
    # crawl is a cold walk from the seed. A site whose owner is busy is simply left for the next
    # tick; that is also what stops a still-running daily crawl from being dispatched a second time
    # when its row's retry comes round, because its owner reads busy.
    free_set = set(free)
    pairs: list[tuple[str, int]] = []
    for r in rows:
        if max_dispatch and len(pairs) >= max_dispatch:
            break
        own = owner_of(db, r)
        if own in free_set:
            pairs.append((r.url, own))
            free_set.discard(own)   # one site per instance per tick
    if not pairs:
        return 0

    bases = {i: b for i, b in zip(res["instances"], res["bases"])}
    send = dispatch or (lambda job, base: crawler_client.dispatch(job, base=base))
    log_lock = threading.Lock()

    def _one(pair) -> int:
        """Crawl one site on one instance.

        Its own Session: these run concurrently and a SQLAlchemy Session is not thread-safe. The
        row is re-fetched by primary key rather than passed across the thread boundary for the
        same reason.
        """
        row_url, inst = pair
        with SessionLocal() as sdb:
            row = sdb.get(DailyUrl, row_url)
            if row is None:
                return 0
            # Re-read the master switch per site, not once per tick: dispatch() blocks for a whole
            # crawl, so the flag this tick was admitted on is stale by the time a later site
            # starts. Off means off, including for work this tick had already decided to do.
            sdb.expire_all()
            if not scheduler.master_enabled(sdb):
                with log_lock:
                    scheduler.log_event(sdb, "daily_skipped_master_off",
                                        detail="master switch off mid-tick: %s not crawled" % row.url)
                    sdb.commit()
                return 0
            if not scheduler.lane_open(sdb, "daily"):
                with log_lock:
                    scheduler.log_event(sdb, "daily_skipped_lane_closed",
                                        detail="'daily' lane closed mid-tick: %s not crawled" % row.url)
                    sdb.commit()
                return 0
            job = build_job(row)
            row.last_status = "running"
            row.last_run_at = now
            scheduler.log_event(sdb, "daily_dispatched", job_id=job["job_id"],
                                detail="%s -> instance %d" % (row.url, inst))
            sdb.commit()
            t0 = time.monotonic()
            try:
                resp = send(job, bases[inst])
            except Exception as e:  # noqa: BLE001 - record and leave the row for the next tick
                row.last_status = "error"
                row.last_detail = str(e)[:400]
                row.next_run = now + dt.timedelta(seconds=RETRY_AFTER_S)
                scheduler.log_event(sdb, "daily_failed", job_id=job["job_id"], detail=str(e)[:200])
                sdb.commit()
                return 0
            elapsed = time.monotonic() - t0
            summ = (resp or {}).get("summary", {}) if isinstance(resp, dict) else {}
            g = lambda k: int(summ.get(k) or 0)  # noqa: E731
            if str(summ.get("stop_reason") or "") == "stopped":
                # CUT SHORT IS NOT DONE. The crawler reports "stopped" only for an operator stop
                # or the engine's wall-clock chop (reaching max_pages is "max_pages", a drained
                # site is "completed"). Recording it as a good run advanced next_run a whole day,
                # so a fleet-wide stop — the first step of "run only the daily lane" — silently
                # cost every in-flight daily site its day. Retry within the hour instead, exactly
                # as a failed fetch does; the policy decides whether that retry may go out.
                row.last_status = "stopped"
                row.last_detail = "stopped after %.0fs (fetched=%d) — retrying within the hour" % (
                    elapsed, g("fetched"))
                row.next_run = now + dt.timedelta(seconds=RETRY_AFTER_S)
                scheduler.log_event(sdb, "daily_interrupted", job_id=job["job_id"],
                                    detail="%s - %s" % (row.url, row.last_detail))
                sdb.commit()
                return 0
            kept = g("kept")
            # `stored` is the ingest-ACCEPTED count when the crawler reports it: that is the only
            # number proving the cycle reached Postgres+MinIO, so a healthy crawl talking to a
            # dead ingest reads stored=0 instead of quietly claiming success.
            stored = g("accepted") if "accepted" in summ else kept
            row.last_status = "ok"
            row.runs += 1
            row.last_kept = kept
            row.last_seconds = elapsed
            row.last_detail = "fetched=%d kept=%d stored=%d unchanged=%d" % (
                g("fetched"), kept, stored, g("skipped_unchanged") + g("not_modified_304"))
            # Advance FROM the scheduled time, not from now, so a site keeps its hour of the day
            # instead of drifting later every time a crawl runs long.
            row.next_run = scheduler.compute_next_run("daily", prev=row.next_run, now=now)
            scheduler.log_event(sdb, "daily_completed", job_id=job["job_id"],
                                detail="%s - %s - %.1fs on instance %d"
                                       % (row.url, row.last_detail, elapsed, inst))
            sdb.commit()
            return 1

    with ThreadPoolExecutor(max_workers=len(pairs)) as ex:
        return sum(ex.map(_one, pairs))


def status(db: Session) -> dict:
    """Everything the panel needs in one call: the reservation, the queue, and the conflicts."""
    rows = db.scalars(select(DailyUrl).order_by(DailyUrl.next_run)).all()
    res = reservation()
    return {
        "reservation": res,
        "conflicts": shard.reserved_conflicts(db) if res["enabled"] else [],
        "due_now": len(due(db, scheduler.utcnow())) if res["enabled"] else 0,
        "overdue": len(overdue(db)) if res["enabled"] else 0,
        "sites": [{"url": r.url, "domain": r.domain, "enabled": r.enabled,
                   "next_run": r.next_run.isoformat() if r.next_run else None,
                   "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                   "last_status": r.last_status, "last_detail": r.last_detail,
                   "runs": r.runs, "last_kept": r.last_kept,
                   "last_seconds": round(r.last_seconds, 1)} for r in rows],
        "enabled_sites": sum(1 for r in rows if r.enabled),
        "total_sites": len(rows),
    }


# -- async shell -------------------------------------------------------------------------------

def tick_once() -> None:
    with SessionLocal() as db:
        if scheduler.master_enabled(db):
            run_due(db)


async def loop(stop: asyncio.Event) -> None:
    """Tick until `stop` is set. Never dies on an exception, and no-ops entirely when no instance
    is reserved - so this task is safe to start unconditionally."""
    res = reservation()
    if not res["enabled"]:
        log.info("daily lane idle: %s", res["reason"])
        return
    tick = get_settings().daily_tick
    log.info("daily lane started on instance(s) %s, %s, tick=%ss",
             res["instances"], "exclusive" if res["exclusive"] else "priority", tick)
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick_once)   # dispatch blocks for minutes; keep the loop free
        except Exception:
            log.exception("daily tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    log.info("daily lane stopped")
