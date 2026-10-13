"""The control loop: catalog → jobs → crawler, with coverage tracking.

The crawler persists crawled docs to its own Postgres + MinIO (jobs carry forward_to_ingest);
the orchestrator's job ends at dispatch + recording a JobRun. (L2 was removed from the stack.)
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crawler_client, jobgen, tiering
from .models import CoverageCell, JobRun, Keyword, SiteMeasurement, SiteSchedule, Source
from .seed import Seed, load_seed
from .sources import CATEGORY_TIER, resolve_source, subdomain_parent

log = logging.getLogger("mallory.orchestrate")

FREQ_SECONDS = {"6h": 6 * 3600, "daily": 86400, "weekly": 7 * 86400}


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def upsert_source(
    db: Session,
    url: str,
    *,
    frequency: str = "daily",
    category: str | None = None,
    seed_urls: list[str] | None = None,
    search_template: str | None = None,
    region: str | None = None,
    added_by: str = "human",
) -> Source:
    """Add/update a source. Human supplies url+frequency+category; the rest is auto-resolved."""
    human_cat = {resolve_source(url).domain: category} if category else {}
    res = resolve_source(url, human_catalog=human_cat)
    existing = db.get(Source, res.domain)
    if existing is not None and subdomain_parent(url):
        # This URL is a subdomain of a source that is ALREADY catalogued, so it is not a new
        # source - it lands on the existing row. Rewriting that row's category, tier and
        # provenance with values chosen for the subdomain silently destroys a curated entry:
        # an import of asc.army.mil once refiled army.mil as a think_tank. Leave it untouched
        # and hand the caller the row it is actually covered by.
        return existing
    row = existing or Source(domain=res.domain, created_at=_now())
    row.source_id = res.source_id
    row.category = res.category
    row.tier = CATEGORY_TIER[res.category]
    row.frequency = frequency
    row.region = region
    row.source_known = res.source_known
    row.tier_origin = res.resolved_by
    row.added_by = added_by
    if seed_urls is not None:
        row.seed_urls = seed_urls
    if search_template is not None:
        row.search_template = search_template
    if not existing:
        db.add(row)
    db.commit()
    return row


def covered_by(db: Session, url: str) -> Source | None:
    """The catalogue row a URL is already covered by, or None if it would be a genuine addition.

    Lets a bulk import report "405 of these are subdomains of sources we already have" instead of
    reporting them as added - which is what made a destructive import look like a successful one.
    """
    parent = subdomain_parent(url)
    return db.get(Source, parent) if parent else None


def due_sources(db: Session) -> list[Source]:
    out = []
    now = _now()
    for s in db.scalars(select(Source)).all():
        if s.last_crawled is None:
            out.append(s)
            continue
        age = (now - s.last_crawled).total_seconds()
        if age >= FREQ_SECONDS.get(s.frequency, 86400):
            out.append(s)
    return out


def build_jobs(db: Session, seed: Seed | None = None, only_due: bool = False) -> list[dict]:
    seed = seed or load_seed()
    sources = due_sources(db) if only_due else db.scalars(select(Source)).all()
    return jobgen.generate(list(sources), seed)


def build_test_jobs(seed_dir: str | None = None) -> list[dict]:
    """Offline end-to-end batch: explicit fixture URLs with correct job_type, source-stamped."""
    import json
    import pathlib

    from .config import get_settings
    from .sources import resolve_source

    d = pathlib.Path(seed_dir or get_settings().seed_dir)
    targets = json.loads((d / "test_targets.json").read_text()).get("targets", [])
    jobs: list[dict] = []
    for i, t in enumerate(targets, 1):
        res = resolve_source(t["url"])
        job = jobgen._base(t["job_type"], f"test_{i}_{res.source_id}", [t["url"]],
                           t.get("keywords", []), t.get("target_entity"))
        job["expected_record_types"] = t.get("expected_record_types", job["expected_record_types"])
        job["source_id"] = res.source_id
        job["source_tier"] = res.tier
        job["source_type"] = res.category
        jobs.append(job)
    return jobs


def _mark_coverage(db: Session, job: dict) -> None:
    domain_id = job.get("source_id")
    entity = job.get("target_entity")
    # find the domain by source_id
    src = db.scalar(select(Source).where(Source.source_id == domain_id))
    if not src:
        return
    src.last_crawled = _now()
    cell_id = f"{src.domain}|{entity or '*'}"
    cell = db.get(CoverageCell, cell_id) or CoverageCell(id=cell_id, domain=src.domain, entity_id=entity)
    cell.last_fetched = _now()
    cell.status = "fresh"
    db.merge(cell)


def record_run(db: Session, job: dict, resp: dict) -> JobRun:
    """Persist a JobRun from a SUCCESSFUL crawler dispatch and mark coverage. Does not commit.

    Shared by run_batch (human 'Run due') and the scheduler so both log runs identically.
    """
    summ = resp.get("summary", {})
    run = JobRun(
        job_id=job["job_id"], domain=_domain_of(db, job), entity_id=job.get("target_entity"),
        dispatched_at=_now(),
        fetched=summ.get("fetched", 0), kept=summ.get("kept", 0),
        records_emitted=summ.get("records_emitted", 0), status="ok",
    )
    db.add(run)
    _mark_coverage(db, job)
    return run


def _domain_of(db: Session, job: dict) -> str | None:
    """Resolve the job's source domain. (JobRun.domain used to be hardcoded None, which made the
    run log un-joinable to the catalog.)"""
    sid = job.get("source_id")
    if sid:
        src = db.scalars(select(Source).where(Source.source_id == sid)).first()
        if src:
            return src.domain
        # `source_id` on a job is the DOMAIN in practice (scheduled_jobs write "sigsauer.com"),
        # while Source.source_id is the catalogue NAME ("SIGSAUER"). So the lookup above misses
        # every real job and JobRun.domain stayed null — exactly the un-joinable run log this
        # function was written to fix. Measured: 87 rows from one dispatch, all domain=None, which
        # left the fleet with no memory of which sites it had already tried.
        src = db.get(Source, sid)
        if src:
            return src.domain
    # Last resort, and the one that always works: the host of the seed URL.
    return _seed_host(job)


def order_jobs(db: Session, jobs: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """DEFENCE-FIRST ordering for the human Run button — the same priority key the scheduler loop
    uses. Without this, requirement "defence sites first" would hold only on the scheduler path and
    not on the button the operator actually presses."""
    now = now or tiering.utcnow()
    pols = {p.source_id: p for p in db.scalars(select(SiteSchedule)).all()}
    srcs = {s.source_id: s for s in db.scalars(select(Source)).all()}

    def key(job: dict):
        sid = job.get("source_id")
        pol = pols.get(sid)
        return tiering.priority_key(pol.domain if pol else sid, pol, srcs.get(sid), now)

    return sorted(jobs, key=key)


def run_batch(db: Session, jobs: list[dict]) -> dict:
    from . import scheduler  # deferred: scheduler imports THIS module at load time

    now = tiering.utcnow()
    totals = {"dispatched": 0, "fetched": 0, "kept": 0, "records": 0, "errors": 0}
    # THE ONE GATE. This is "dispatch due catalogue jobs now" — the same work the scheduler tick
    # sends — by a route that read none of the halt, maintenance or master switches.
    if not scheduler.lane_open(db, "scheduled"):
        return {**totals, "held": "the 'scheduled' lane is closed by the dispatch policy"}
    pols = {p.source_id: p for p in db.scalars(select(SiteSchedule)).all()}
    jobs = order_jobs(db, jobs, now)
    # The manual Run button honours the same per-category switches as the scheduler tick — a
    # category the operator turned off must not come back to life via a different entry point.
    off = scheduler.disabled_categories(db)
    if off:
        # Filtered on the operator BUCKET, not job_type. Three buckets share job_type "news", so a
        # job_type filter answering to a switch labelled "Gov portal" also silently held News and
        # Think tank. bucket_of_job never guesses: an unplaceable job is `uncatalogued`, which is
        # only ever held if the operator switched THAT row off.
        from . import categories
        by_source = categories.catalog_buckets(db)
        jobs = [j for j in jobs if categories.bucket_of_job(j, by_source) not in off]
    for job in jobs:
        pol = pols.get(job.get("source_id"))
        # Captured before observe() flips baseline→probe, so the log can label the first crawl.
        baseline = pol is not None and pol.phase == "baseline"
        if pol is not None:
            # baseline census fetches everything; every later run is incremental (which is also the
            # only way the crawler emits 304/not-modified signal for the change-rate measurement).
            job = {**job, "incremental": not baseline}
        t0 = time.monotonic()
        try:
            resp = crawler_client.dispatch(job)
            elapsed = time.monotonic() - t0
            run = record_run(db, job, resp)
            summ = resp.get("summary", {}) or {}
            tiering.observe(db, pol, summ, now, elapsed)
            if pol is not None:
                tiering.advance_phase(db, pol, now)
            scheduler.log_cycle(db, None, job.get("job_id"), summ, elapsed, baseline=baseline)
            totals["fetched"] += run.fetched
            totals["kept"] += run.kept
            totals["records"] += run.records_emitted
        except Exception as e:  # noqa: BLE001 — record and continue at scale
            db.add(JobRun(job_id=job["job_id"], domain=_domain_of(db, job),
                          entity_id=job.get("target_entity"),
                          dispatched_at=_now(), status="error", detail=str(e)[:400]))
            totals["errors"] += 1
        totals["dispatched"] += 1
    db.commit()
    return totals


# ── manual batch: run N sites at a time, recording each as it finishes ────────
# The old "Run all now" fired the whole list at the crawler with background:true and recorded
# NOTHING — no JobRun, no SiteMeasurement — so a 40-site batch could crawl for hours while the Logs
# tab stayed empty and the recrawl delta never moved. Here the list is dispatched in chunks and each
# job's summary is folded in the moment its chunk closes, so Logs fill in AS the crawl runs.
_MANUAL: dict = {"running": False, "total": 0, "done": 0, "parallel": 0,
                 "current": [], "current_domains": [], "sites": [],
                 "started_at": None, "error": None, "halted": None, "lane": "manual"}


def manual_status() -> dict:
    """Snapshot of the manual batch for the dashboard to poll."""
    s = dict(_MANUAL)
    s["sites"] = list(_MANUAL["sites"])[-200:]
    return s


def _seed_host(job: dict) -> str | None:
    """Domain from the job's first seed URL — the fallback when a job carries no catalog source_id
    (a pasted/ad-hoc batch), so its run still lands under a real host in the logs."""
    from urllib.parse import urlsplit
    for u in (job.get("seed_urls") or []):
        h = (urlsplit(str(u)).hostname or "").lower().removeprefix("www.")
        if h:
            return h
    return None


def _dispatch_routed(db, chunk: list[dict], *, timeout: float) -> dict:
    """Send a chunk to the crawler, splitting it across the fleet by domain ownership.

    A site's durable frontier lives inside ONE instance's data directory, so a job has to go to the
    instance that owns its domain — anywhere else and the crawl silently restarts from the seed
    while the real frontier sits unreachable next door. `shard.route` assigns each domain once and
    then never moves it.

    With a single instance this is exactly the previous behaviour: one bucket, one call.

    The sub-batches run CONCURRENTLY. Sending them one after another would serialise the fleet back
    down to one instance's throughput, which is the whole thing we are trying to escape — the calls
    are synchronous (background:false) so that a chunk still closes before the next one is sent.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import shard

    bases = crawler_client.pool()
    payload = {"jobs": chunk, "background": False, "forward_to_ingest": True}
    if len(bases) <= 1:
        # Byte-identical to the pre-fleet call, `base` not even passed. A one-instance deployment
        # must not take a different code path — that is what makes this safe to ship before any
        # second instance exists.
        return crawler_client.dispatch_batch(payload, timeout=timeout)
    buckets = shard.route(db, chunk, len(bases))
    # NEWS FIRST. In priority mode the daily lane's instances also own catalogue domains, and work
    # them between sweeps — but while a daily site is due, catalogue work bound for those instances
    # is HELD so the lane finds them free. Held jobs are simply not sent this chunk; their schedule
    # is untouched, so they go out on a later chunk once the daily list is clear.
    #
    # Never hold the LAST bucket: if the daily lane owns every instance that this chunk routes to,
    # holding them all would mean the catalogue silently stops rather than merely waiting, and a
    # crawler that quietly does nothing is the worst of the available failures.
    held_for_daily: list[int] = []
    try:
        from . import daily as _daily
        reserved = _daily.reserved_instances()
        if reserved and not _daily.reservation()["exclusive"] and _daily.holds_fleet(db):
            candidate = [i for i in buckets if i in reserved]
            if candidate and len(candidate) < len(buckets):
                held_for_daily = candidate
                for i in held_for_daily:
                    buckets.pop(i, None)
    except Exception:  # noqa: BLE001 — the lane must never be able to stop the catalogue
        log.exception("daily-lane hold check failed; dispatching to the whole fleet")
    if held_for_daily:
        log.info("holding %d bucket(s) for the daily lane: instances %s",
                 len(held_for_daily), held_for_daily)
    if not buckets:
        return {"jobs": 0, "results": [], "instances": [], "held_for_daily": held_for_daily}
    if len(buckets) == 1:
        only = next(iter(buckets))
        res = crawler_client.dispatch_batch({**payload, "jobs": buckets[only]},
                                            timeout=timeout, base=bases[only])
        if held_for_daily and isinstance(res, dict):
            res = {**res, "held_for_daily": held_for_daily}
        return res

    def _send(item):
        idx, jobs = item
        return crawler_client.dispatch_batch(
            {"jobs": jobs, "background": False, "forward_to_ingest": True},
            timeout=timeout, base=bases[idx])

    merged: dict = {"jobs": 0, "results": [], "instances": sorted(buckets),
                    "held_for_daily": held_for_daily}
    errors = []
    with ThreadPoolExecutor(max_workers=len(buckets)) as ex:
        for idx, res in zip(sorted(buckets), ex.map(_send, sorted(buckets.items()))):
            try:
                merged["results"].extend((res or {}).get("results") or [])
                merged["jobs"] += int((res or {}).get("jobs") or 0)
            except Exception as e:  # noqa: BLE001
                errors.append({"instance": idx, "error": str(e)[:200]})
    # Every instance failing is a failed chunk; some failing is a partial chunk that must still be
    # recorded, so the caller's own error handling stays in charge.
    if errors and len(errors) == len(buckets):
        raise RuntimeError(f"every instance failed: {errors[0]['error']}")
    if errors:
        merged["errors"] = errors
    return merged


def _chunks(seq: list, n: int):
    if n <= 0:
        yield list(seq)
        return
    for i in range(0, len(seq), n):
        yield list(seq[i:i + n])


def _halt_reason(db: Session, *, first_chunk: bool = False, lane: str | None = None) -> str | None:
    """Why this batch must not dispatch anything more — or None to carry on.

    `lane` is the KIND of work this batch is (policy.LANES): a pasted batch and gap-fill are
    "manual" (campaign work); the Assign button's first-crawls of never-crawled catalogue sites are
    "scheduled" (the system's own work). Same loop, different gate, so a catalogue first-crawl
    cannot be smuggled through a campaign and a campaign cannot run through the normal mode.
    None = the running batch's own lane (_MANUAL["lane"]).

    Two independent operator stops, checked together because either one alone is a way for a
    "stopped" pipeline to keep crawling:

      * the orchestrator's master switch, and
      * the crawler's own stop flag, which the crawler CLEARS on the way into each batch, so a
        STOP pressed there survives only until the next chunk asks for one.

    `first_chunk` is the important argument. The crawler's stop signal is a FILE, so it outlives the
    container that wrote it: a Stop pressed last week, or the one the master switch itself sends
    when it halts a crawl, is still sitting there when you press Run tomorrow. Checked before the
    FIRST chunk, that stale flag refuses the whole batch — measured: "stop requested on the crawler
    — 802 site(s) not crawled", with nothing running and nobody having pressed anything.

    So before the first chunk the flag is ignored, because it cannot mean "stop THIS batch" — the
    batch has not started. It is not being suppressed either: crawler-api arms itself on the way
    into every dispatch (its own `_arm()`), which clears exactly that staleness. From the second
    chunk on the flag can only have been set DURING this run, which is when it means what it says.

    An unreachable crawler is deliberately NOT a halt: the master switch is the authority, and a
    transient status blip must not silently abandon a running batch half-done.
    """
    from . import scheduler  # deferred: scheduler imports this module at load time

    # SessionLocal is expire_on_commit=False, so without this the Setting comes from this session's
    # identity map and never sees the API session's write — the re-check would be a no-op.
    db.expire_all()
    if not scheduler.master_enabled(db):
        return "master switch off — the pipeline is stopped"
    # Maintenance is the operator saying "hold still". It already stands down the refill watcher and
    # the autopilot; this loop was the last thing that could keep feeding the fleet through it.
    if scheduler.maintenance(db):
        return "maintenance mode on"
    # THE ONE GATE. Every caller of run_manual_batch — the batch box, the Assign button's
    # first-crawls, gap-fill — lands here before every chunk, so closing the lane holds all of
    # them at once, including a batch that is mid-way through its list. Read AFTER the expire_all
    # above, or a batch that holds one session for hours never sees the operator's Stop.
    lane = lane or str(_MANUAL.get("lane") or "manual")
    if not scheduler.lane_open(db, lane):
        return "the '%s' lane is closed: %s" % (lane, scheduler.why_closed(db, lane))
    if first_chunk:
        return None
    try:
        # ANY instance. A stop is broadcast to all twelve, but this asked instance 0 alone, so a
        # chunk loop whose work had been routed elsewhere kept dispatching after a stop.
        if any((r.get("data") or {}).get("stop_pending")
               for r in crawler_client.fanout(crawler_client.batch_status) if r.get("ok")):
            return "stop requested on the crawler"
    except Exception:  # noqa: BLE001 — see the docstring
        pass
    return None


def _record_manual(db: Session, job: dict, summary: dict, elapsed: float) -> dict:
    """Fold ONE finished site into the run history: JobRun + SiteMeasurement + the cycle log —
    the same three writes the scheduled path makes, so Logs and the recrawl delta treat a manual
    run exactly like a scheduled one."""
    from . import scheduler  # deferred: scheduler imports this module at load time

    now = tiering.utcnow()
    pol = db.scalar(select(SiteSchedule).where(SiteSchedule.source_id == job.get("source_id"))) \
        if job.get("source_id") else None
    run = record_run(db, job, {"summary": summary})
    domain = run.domain or _seed_host(job)
    # Mark the catalog row crawled by DOMAIN. record_run's _mark_coverage only does this when the job
    # carries a source_id that resolves, so an ad-hoc/pasted job (or one whose source_id didn't match)
    # left last_crawled=None — which is what made "only never-crawled sites" keep re-offering sites we
    # had already crawled. Keyed on the resolved domain, which we always have.
    if domain:
        src = db.get(Source, domain)
        if src is not None:
            src.last_crawled = _now()
    if pol is not None:
        tiering.observe(db, pol, summary, now, elapsed)   # writes SiteMeasurement + folds the policy
        tiering.advance_phase(db, pol, now)
    elif domain:
        # No scheduler policy for this site (it was never added to the scheduler), and observe()
        # returns early without one — so a manual crawl would leave NO measurement and the recrawl
        # delta would stay empty forever. Record the observation directly; /api/logs/runs diffs
        # consecutive SiteMeasurement rows per domain, so this is all the delta needs.
        g = lambda k: int(summary.get(k) or 0)  # noqa: E731
        kept, checked = g("kept"), g("kept") + g("skipped_unchanged") + g("skipped_duplicate") + g("not_modified_304")
        db.add(SiteMeasurement(
            domain=domain, job_id=job.get("job_id"), at=now, interval_days=0.0,
            tier_at_run=None, is_sentinel=False,
            fetched=g("fetched"), kept=kept, skipped_unchanged=g("skipped_unchanged"),
            skipped_duplicate=g("skipped_duplicate"), not_modified_304=g("not_modified_304"),
            dropped_by_gate=g("dropped_by_gate"), errors=g("errors"), recovered=g("recovered"),
            checked=checked, change_rate=(kept / checked if checked else 0.0),
            changes_per_day=0.0, duration_s=float(elapsed)))
    scheduler.log_cycle(db, None, job.get("job_id"), summary, elapsed)
    # No `or "completed"`: an absent stop_reason means the crawler told us nothing, which is not the
    # same as a clean finish. Defaulting it to success is how a cut-short run got a green badge.
    stop = summary.get("stop_reason") or "unknown"
    return {
        "job_id": job.get("job_id"),
        "domain": domain or (job.get("seed_urls") or [""])[0],
        # None, not 0 — for the same reason stop_reason above refuses to default to "completed".
        # A crawler that reported no counts told us nothing, and a measured zero is a different
        # claim; the dashboard renders None as "—".
        "fetched": summary.get("fetched"),
        "kept": summary.get("kept"),                                 # new/changed this run
        "unchanged": int(summary.get("skipped_unchanged") or 0) + int(summary.get("not_modified_304") or 0),
        "stop_reason": stop,
        "max_pages": summary.get("max_pages"),
        "duration_s": round(elapsed, 1),
        "at": _now().isoformat(),
    }


def run_manual_batch(jobs: list[dict], parallel: int = 0, lane: str = "manual") -> None:
    """Crawl `jobs` in chunks of `parallel` sites at a time (<=0 = the whole list at once).

    Runs on the caller's thread — the API starts it in a background thread. Each chunk is dispatched
    SYNCHRONOUSLY so the crawler hands back per-job summaries, which are recorded immediately.
    `lane` names the kind of work for the gate before every chunk — see _halt_reason.
    """
    from .db import SessionLocal

    _MANUAL.update(running=True, total=len(jobs), done=0, parallel=parallel,
                   current=[], current_domains=[], sites=[],
                   started_at=_now().isoformat(), error=None, halted=None, lane=lane)
    by_id = {j.get("job_id"): j for j in jobs}
    sent_any = False          # has THIS batch dispatched anything yet — see _halt_reason
    try:
        with SessionLocal() as db:
            for chunk in _chunks(jobs, parallel):
                if not chunk:
                    continue
                halt = _halt_reason(db, first_chunk=not _MANUAL["done"] and not sent_any)
                if halt:
                    # THE master-switch bug. This loop dispatches chunk after chunk, and each
                    # dispatch BLOCKS for the whole crawl. Switching the master off stopped the
                    # chunk in flight — and then this loop calmly started the next one, so an
                    # operator who "stopped the pipeline" watched the next five sites begin.
                    # Worse, the crawler ARMS itself on every batch (clear_stop on the way in),
                    # so a STOP pressed on the crawler's own dashboard was undone here too.
                    # Both signals are therefore re-read before every chunk: off means off,
                    # including for work this batch had already decided to do.
                    _MANUAL["halted"] = f"{halt} — {len(jobs) - _MANUAL['done']} site(s) not crawled"
                    break
                _MANUAL["current"] = [j.get("job_id") for j in chunk]
                # Domains too, so the Logs tab can mark these hosts "crawling" instead of reading a
                # mid-flight report and calling them complete.
                _MANUAL["current_domains"] = [d for d in (_seed_host(j) for j in chunk) if d]
                t0 = time.monotonic()
                sent_any = True
                try:
                    resp = _dispatch_routed(db, chunk, timeout=float(_MANUAL_CHUNK_TIMEOUT_S))
                except Exception as e:  # noqa: BLE001 — one bad chunk must not kill the batch
                    _MANUAL["error"] = str(e)[:300]
                    for j in chunk:
                        db.add(JobRun(job_id=j["job_id"], domain=_domain_of(db, j),
                                      entity_id=j.get("target_entity"), dispatched_at=_now(),
                                      status="error", detail=str(e)[:400]))
                    _MANUAL["done"] += len(chunk)
                    db.commit()
                    continue
                # split the chunk's wall-clock across its jobs — the crawler runs them concurrently,
                # so no per-job timing exists; this keeps the duration column honest-ish, not precise.
                each = (time.monotonic() - t0) / max(1, len(chunk))
                for r in resp.get("results", []) or []:
                    job = by_id.get(r.get("job_id"))
                    if not job:
                        continue
                    try:
                        _MANUAL["sites"].append(_record_manual(db, job, r.get("summary") or {}, each))
                    except Exception:  # noqa: BLE001
                        pass
                    _MANUAL["done"] += 1
                db.commit()
                _MANUAL["current"] = []
                _MANUAL["current_domains"] = []
    finally:
        _MANUAL.update(running=False, current=[], current_domains=[])


# A chunk runs to frontier exhaustion, which can be hours on an unlimited-depth profile job.
_MANUAL_CHUNK_TIMEOUT_S = 24 * 3600


def generate_panel_jobs(db: Session, job_type: str, *, batch_size: int = 10,
                        max_pages: int | None = None, max_depth: int | None = None,
                        use_keyword_bank: bool = True) -> list[dict]:
    """Build the catalog×seed matrix, filter to job_type, cap to batch_size, apply overrides,
    and (optionally) merge the keyword bank. Powers the dashboard's Job Generator page."""
    seed = load_seed()
    sources = db.scalars(select(Source)).all()
    jobs = [j for j in jobgen.generate(list(sources), seed) if j.get("job_type") == job_type]
    jobs = jobs[: max(1, batch_size)]
    for j in jobs:
        if max_pages and max_pages > 0:
            j["max_pages"] = max_pages
        if max_depth is not None and max_depth >= 0:
            j["max_depth"] = max_depth
    if use_keyword_bank:
        bank = db.scalars(
            select(Keyword.term).where(Keyword.group_label.in_([job_type, "general"]))
        ).all()
        if bank:
            for j in jobs:
                j["keywords"] = list(dict.fromkeys(list(j.get("keywords", [])) + list(bank)))[:12]
    return jobs
