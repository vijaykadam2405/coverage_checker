"""Admin/control-plane API — the dashboard's backend.

Sources catalog, job generation, the crawl scheduler (scheduled jobs + master switch),
crawler proxies (live metrics / stop), keyword bank, and run history.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from threading import Lock, Thread
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import func as sa_func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (campaigns, categories, crawler_client, errorclass, importer, jobgen, orchestrate,
               policy, scheduler, seedprobe, tiering, twin, cache)
from .config import get_settings
from .db import get_db
from .sources import resolve_source
from .models import (Campaign, CoverageCell, JobRun, Keyword, ScheduledJob, ScheduleLog,
                     SiteMeasurement, SiteSchedule, Source, SourceRow)

router = APIRouter(prefix="/api")

# "tiered" = adaptive cadence driven by measured change rate (see tiering.py); "monthly" is its
# slowest bucket, also selectable manually.
_FREQS = {"once", "6h", "daily", "weekly", "monthly", "tiered"}


# ── DTO helpers ───────────────────────────────────────────────────────────────

def _iso(d: dt.datetime | None) -> str | None:
    return (d.isoformat() + "Z") if d else None  # scheduler datetimes are naive UTC


def _src_dto(s: Source) -> dict:
    return {
        "domain": s.domain, "source_id": s.source_id, "category": s.category,
        # Raw stays — it is what every filter, tier rule and export keys on. The label is what the
        # operator reads. See ui_category().
        "category_label": ui_category(s.category), "tier": s.tier,
        "frequency": s.frequency, "source_known": s.source_known, "tier_origin": s.tier_origin,
        "added_by": s.added_by, "region": s.region, "accept_rate": s.accept_rate,
        "last_crawled": s.last_crawled.isoformat() if s.last_crawled else None,
    }


def _sched_dto(sj: ScheduledJob) -> dict:
    return {
        "id": sj.id, "name": sj.name, "job_id": sj.job.get("job_id"), "job_type": sj.job_type,
        "frequency": sj.frequency, "retry_count": sj.retry_count, "retries_left": sj.retries_left,
        "start_at": _iso(sj.start_at), "next_run": _iso(sj.next_run), "last_run_at": _iso(sj.last_run_at),
        "last_status": sj.last_status, "enabled": sj.enabled, "created_at": _iso(sj.created_at),
        "job": sj.job,
    }


def _kw_dto(k: Keyword) -> dict:
    return {"id": k.id, "term": k.term, "group": k.group_label, "created_at": _iso(k.created_at)}


def _slog_dto(row: ScheduleLog) -> dict:
    return {"id": row.id, "at": _iso(row.at), "schedule_id": row.schedule_id,
            "job_id": row.job_id, "event": row.event, "detail": row.detail}


# ── request models ────────────────────────────────────────────────────────────

class AddSource(BaseModel):
    url: str
    frequency: str = "daily"
    category: str | None = None  # optional; auto-classified if omitted


class BulkSources(BaseModel):
    sources: list[dict]  # each {url, frequency?, category?, region?}


class GenerateJobs(BaseModel):
    job_type: str
    batch_size: int = 10
    max_pages: int | None = None
    max_depth: int | None = None
    use_keyword_bank: bool = True


class ScheduleCreate(BaseModel):
    job: dict
    name: str | None = None
    frequency: str = "once"
    retry_count: int = 0
    start_at: str | None = None      # ISO-8601 (offset honored)
    start_date: str | None = None    # "YYYY-MM-DD" (paired with start_time, interpreted UTC)
    start_time: str | None = None    # "HH:MM"
    max_pages: int | None = None
    max_depth: int | None = None
    keywords: list[str] | None = None
    enabled: bool = True


class MasterToggle(BaseModel):
    enabled: bool
    flush: bool = False    # switching OFF: also halt the live crawl and drop its resume position


class CategoryToggle(BaseModel):
    category: str          # == ScheduledJob.job_type (news|tender|profile|spec|patent_aux)
    enabled: bool


class ModeSwitch(BaseModel):
    """`mode` is "normal" (the daily lane + the catalogue schedule) or "campaign" (ad-hoc batches).
    See policy.MODES. The switch stops whatever the OTHER mode had in flight — frontiers kept."""
    mode: str


class AutoToggle(BaseModel):
    """One of policy.AUTOS by name. `enabled` is the STORED value; it takes effect only while the
    pipeline is running, and Stop hard-disables it without changing what is stored here."""
    name: str
    enabled: bool


class CampaignPlanRequest(BaseModel):
    """The launcher's intake: a pasted/uploaded list (text, or a list of strings) and the three
    options (window_from/window_to as YYYY-MM-DD, scope whole_site|news_only, pdfs). See
    campaigns.build_plan — nothing is dispatched by a plan."""
    name: str | None = None
    sites: str | list[str]
    options: dict | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    sites: str | list[str] | None = None
    options: dict | None = None


class KeywordCreate(BaseModel):
    term: str | None = None
    terms: list[str] | None = None
    group: str = "general"


def _to_naive_utc(d: dt.datetime) -> dt.datetime:
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _parse_start(start_at: str | None, start_date: str | None, start_time: str | None) -> dt.datetime | None:
    if start_at:
        try:
            return _to_naive_utc(dt.datetime.fromisoformat(start_at.replace("Z", "+00:00")))
        except ValueError:
            raise HTTPException(422, "start_at must be ISO-8601")
    if start_date:
        try:
            return dt.datetime.fromisoformat(f"{start_date}T{start_time or '00:00'}")
        except ValueError:
            raise HTTPException(422, "start_date/start_time must be YYYY-MM-DD / HH:MM")
    return None


# ── Sources ───────────────────────────────────────────────────────────────────

@router.get("/sources")
def list_sources(db: Session = Depends(get_db)) -> list[dict]:
    return [_src_dto(s) for s in db.scalars(select(Source).order_by(Source.tier, Source.domain)).all()]


@router.post("/sources")
def add_source(body: AddSource, db: Session = Depends(get_db)) -> dict:
    s = orchestrate.upsert_source(db, body.url, frequency=body.frequency, category=body.category)
    return _src_dto(s)


@router.post("/sources/bulk")
def bulk_sources(body: BulkSources, db: Session = Depends(get_db)) -> dict:
    added, errors = 0, []
    for row in body.sources:
        url = str(row.get("url") or "").strip()
        if not url:
            errors.append({"url": url, "error": "missing url"})
            continue
        try:
            orchestrate.upsert_source(db, url, frequency=(row.get("frequency") or "daily"),
                                      category=row.get("category"), region=row.get("region"))
            added += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"url": url, "error": str(e)[:200]})
    return {"added": added, "errors": errors}


@router.delete("/sources")
def delete_all_sources(confirm: str = "", db: Session = Depends(get_db)) -> dict:
    """Wipe the whole source catalog and the per-site config derived from it.

    Requires ?confirm=DELETE_ALL — this empties the table the scheduler and job generator read
    from, and there is no undo, so it must not be reachable by a stray DELETE /sources.

    Clears sources + everything keyed off a domain that describes HOW to crawl it (imported rows,
    coverage, cadence/phase schedules, measurements). Deliberately KEEPS job_runs: that is the
    historical record of what was actually crawled, and losing the audit trail because someone
    re-imported a catalog would be a far worse outcome than a few orphaned run rows."""
    if confirm != "DELETE_ALL":
        raise HTTPException(400, "refusing to wipe the catalog without ?confirm=DELETE_ALL")
    n = db.scalar(select(sa_func.count()).select_from(Source)) or 0
    # Some of these tables are created lazily (source_rows only appears once a CSV/XLSX import has
    # run), so a DELETE against a fresh DB raises "no such table" and — inside one transaction —
    # would abort the entire wipe. Only touch tables that actually exist.
    present = set(sa_inspect(db.get_bind()).get_table_names())
    cleared = []
    for model in (CoverageCell, SourceRow, SiteSchedule, SiteMeasurement, Source):
        if model.__tablename__ in present:
            db.execute(sa_delete(model))
            cleared.append(model.__tablename__)
    db.commit()
    return {"deleted": n, "cleared": cleared, "kept": "job_runs (crawl history)"}


@router.delete("/sources/{domain}")
def delete_source(domain: str, db: Session = Depends(get_db)) -> dict:
    s = db.get(Source, domain)
    if not s:
        raise HTTPException(404, "no such source")
    db.delete(s)
    db.execute(sa_delete(CoverageCell).where(CoverageCell.domain == domain))
    db.commit()
    return {"deleted": domain}


# ── Coverage / run history ────────────────────────────────────────────────────

@router.get("/coverage")
def coverage(db: Session = Depends(get_db)) -> dict:
    sources = db.scalars(select(Source)).all()
    cells = db.scalars(select(CoverageCell)).all()
    return {
        "sources": len(sources),
        "coverage_cells": len(cells),
        "fresh": sum(1 for c in cells if c.status == "fresh"),
        "gaps": sum(1 for s in sources if s.last_crawled is None),
        "auto_discovered": sum(1 for s in sources if s.added_by == "auto"),
    }


@router.get("/jobs")
def jobs(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    limit = max(1, min(500, limit))
    runs = db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(limit)).all()
    return [
        {"job_id": r.job_id, "entity": r.entity_id, "fetched": r.fetched, "kept": r.kept,
         "records": r.records_emitted, "forwarded": r.records_forwarded, "l2_accepted": r.l2_accepted,
         "status": r.status, "detail": r.detail,
         "at": r.dispatched_at.isoformat() if r.dispatched_at else None}
        for r in runs
    ]


@router.get("/matrix")
def matrix(db: Session = Depends(get_db)) -> dict:
    jb = orchestrate.build_jobs(db, only_due=False)
    return {"total_jobs": len(jb), "by_type": dict(Counter(j["job_type"] for j in jb))}


@router.post("/run")
def run(db: Session = Depends(get_db)) -> dict:
    """Production: dispatch due catalog jobs now (real network)."""
    return orchestrate.run_batch(db, orchestrate.build_jobs(db, only_due=True))


@router.post("/run/test")
def run_test(db: Session = Depends(get_db)) -> dict:
    """Offline end-to-end: dispatch the fixture batch through the crawler."""
    return orchestrate.run_batch(db, orchestrate.build_test_jobs())


# ── Job generator ─────────────────────────────────────────────────────────────

@router.post("/jobs/generate")
def generate_jobs(body: GenerateJobs, db: Session = Depends(get_db)) -> dict:
    jobs_out = orchestrate.generate_panel_jobs(
        db, body.job_type, batch_size=body.batch_size, max_pages=body.max_pages,
        max_depth=body.max_depth, use_keyword_bank=body.use_keyword_bank)
    return {"count": len(jobs_out), "jobs": jobs_out}


# ── Scheduled jobs ────────────────────────────────────────────────────────────

@router.get("/schedule")
def list_schedule(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(ScheduledJob).order_by(ScheduledJob.enabled.desc(), ScheduledJob.next_run)
    ).all()
    return [_sched_dto(s) for s in rows]


@router.post("/schedule", status_code=201)
def create_schedule(body: ScheduleCreate, db: Session = Depends(get_db)) -> dict:
    if body.frequency not in _FREQS:
        raise HTTPException(422, f"frequency must be one of {sorted(_FREQS)}")
    job = dict(body.job)
    if not job.get("job_id") or not job.get("job_type"):
        raise HTTPException(422, "job must contain job_id and job_type")
    if body.max_pages is not None:
        job["max_pages"] = body.max_pages
    if body.max_depth is not None:
        job["max_depth"] = body.max_depth
    if body.keywords is not None:
        job["keywords"] = body.keywords
    retry = max(0, min(3, body.retry_count))
    now = scheduler.utcnow()
    start = _parse_start(body.start_at, body.start_date, body.start_time)
    next_run = start if (start and start > now) else now
    sj = ScheduledJob(
        name=body.name or job.get("job_id"), job=job, job_type=str(job.get("job_type", "")),
        frequency=body.frequency, retry_count=retry, retries_left=retry,
        start_at=start, next_run=next_run, last_status="pending",
        enabled=body.enabled, created_at=now)
    db.add(sj)
    db.flush()
    scheduler.log_event(db, "scheduled", sj.id, job.get("job_id"),
                        detail=f"{body.frequency} retry={retry}")
    db.commit()
    db.refresh(sj)
    return _sched_dto(sj)


# Editing a schedule in place lives at `patch_schedule` further down — it predates this block, and
# it is the better handler: it also takes max_depth and it recomputes next_run through
# scheduler.policy_for. A second @router.patch("/schedule/{sid}") added here shadowed it silently
# (FastAPI dispatches to the FIRST match), which quietly dropped max_depth from the Scheduler tab.


class EnrolCompleted(BaseModel):
    frequency: str = "weekly"
    max_pages: int | None = None
    enabled: bool = True


@router.post("/schedule/enrol-completed")
def enrol_completed(body: EnrolCompleted, db: Session = Depends(get_db)) -> dict:
    """Put every FULLY-CRAWLED site onto the schedule so it gets re-crawled on a cadence.

    A first crawl is a census; the value is in the second one. Until now a site that finished simply
    stopped — its pages aged in the corpus with nothing bringing it back — because getting it onto
    the schedule meant generating a job for it by hand. "Complete" here is the same rule the Logs
    tab paints green with: the crawler reported the host AND left nothing unresolved. A host with
    stored pages but no crawl report does NOT qualify; we would only know that pages exist, not that
    it finished.

    Idempotent, and it never touches a site that is already scheduled — re-running it enrols only
    what is newly finished, so it is safe to press after every batch.
    """
    if body.frequency not in _FREQS:
        raise HTTPException(422, f"frequency must be one of {sorted(_FREQS)}")

    rows = logs_sites(limit=5000, db=db).get("rows") or []
    have = set()
    for sj in db.scalars(select(ScheduledJob)).all():
        for u in (sj.job.get("seed_urls") or []):
            h = (urlsplit(str(u)).hostname or "").lower().removeprefix("www.")
            if h:
                have.add(h)

    now = scheduler.utcnow()
    added, skipped_open, already = [], 0, 0
    for r in rows:
        host = str(r.get("host") or "").lower().removeprefix("www.")
        if not host:
            continue
        # unresolved is None when no crawl report exists — "not known to have finished", which is
        # not the same as finished, so it is excluded rather than assumed.
        # Same bar as the "complete" badge, INCLUDING an exhausted frontier. Scheduling a weekly
        # re-crawl of a site that never finished its first pass re-walks it from the seed while its
        # real remaining work sits in a saved frontier nobody resumes.
        if not (r.get("stored_pages") and r.get("unresolved") == 0 and not r.get("pending")):
            skipped_open += 1
            continue
        if host in have:
            already += 1
            continue
        # job_type is the CRAWLER's vocabulary, not the catalogue's. Writing the category here
        # ("manufacturer_ir") produced a job the crawler 422s, so all 508 enrolled re-crawls would
        # have failed on their first tick — invisibly, because they were dated a week out.
        job = {"job_id": f"recrawl_{host.replace('.', '_')}",
               "job_type": jobgen.job_type_for(r.get("category")),
               "seed_urls": [f"https://{host}/"],
               "source_id": host, "same_domain_only": True, "max_depth": -1,
               "max_pages": int(body.max_pages) if body.max_pages else 2000}
        # ScheduledJob.job_type stays the CATEGORY: it drives the per-category on/off switches on
        # the control panel, which are keyed by category and not by crawler job type.
        sj = ScheduledJob(name=host, job=job, job_type=str(r.get("category") or "manufacturer_ir"),
                          frequency=body.frequency, retry_count=1, retries_left=1,
                          start_at=None,
                          next_run=scheduler.compute_next_run(body.frequency, None, now),
                          last_status="pending", enabled=body.enabled, created_at=now)
        db.add(sj)
        have.add(host)
        added.append(host)
    if added:
        scheduler.log_event(db, "scheduled", None, None,
                            detail=f"enrolled {len(added)} completed site(s) at {body.frequency}")
    db.commit()
    return {"enrolled": len(added), "frequency": body.frequency, "hosts": added[:100],
            "already_scheduled": already, "not_complete": skipped_open}


@router.post("/schedule/{sid}/run-delta")
def run_delta(sid: int, db: Session = Depends(get_db)) -> dict:
    """Re-crawl one scheduled site NOW and report only what CHANGED.

    Not a different kind of crawl — the crawler already compares each page's content hash against
    what it stored and reports `skipped_unchanged` — but a different question, asked on demand
    instead of on a cadence: "has anything on this site moved since we last looked". That is the
    check an operator wants before a report goes out, and there was no way to ask it.

    Dispatched in the FOREGROUND deliberately: the answer is the point, and a background run would
    put it in a log the operator then has to go find.
    """
    sj = db.get(ScheduledJob, sid)
    if not sj:
        raise HTTPException(404, "no such schedule")
    # A delta check re-crawls a SCHEDULED site: catalogue work, so it belongs to normal mode.
    if not scheduler.lane_open(db, "scheduled"):
        raise HTTPException(409, "the 'scheduled' lane is closed: %s"
                            % scheduler.why_closed(db, "scheduled"))
    # ONE fan-out, not two. Calling it inside both halves of the condition asked every instance
    # twice and could read a batch that started between the two calls.
    if any((r.get("data") or {}).get("running")
           for r in crawler_client.fanout(crawler_client.batch_status) if r.get("ok")):
        raise HTTPException(409, "a crawl is already running — a delta check would queue behind it")

    job = dict(sj.job)
    job["job_id"] = f"delta_{job.get('job_id', sid)}"
    host = (urlsplit((job.get("seed_urls") or [""])[0]).hostname or "").lower()
    base = crawler_client.pool()
    from . import shard
    owner = shard.owner_of(db, host, len(base)) if host else None
    res = crawler_client.dispatch_batch(
        {"jobs": [job], "background": False, "forward_to_ingest": True},
        timeout=900.0, base=base[owner] if owner is not None and owner < len(base) else None)
    tot = ((res.get("summary") or {}).get("totals") or {}) or res.get("totals") or {}
    fetched = int(tot.get("fetched") or 0)
    unchanged = int(tot.get("skipped_unchanged") or 0) + int(tot.get("not_modified_304") or 0)
    kept = int(tot.get("kept") or 0)
    return {"schedule_id": sid, "host": host, "job_id": job["job_id"],
            "fetched": fetched, "changed_or_new": kept, "unchanged": unchanged,
            # The headline. "Nothing changed" is a real, useful and common answer, and it must not
            # render as a failed or empty crawl.
            "verdict": ("nothing has changed since the last crawl" if fetched and not kept
                        else f"{kept} page(s) new or changed" if kept
                        else "nothing was fetched — see the crawl log"),
            "raw": res.get("summary") or {}}


@router.delete("/schedule")
def clear_schedule(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(ScheduledJob)).all()
    n = len(rows)
    for sj in rows:
        db.delete(sj)
    scheduler.log_event(db, "removed", None, None, detail=f"cleared {n}")
    db.commit()
    return {"deleted": n}


@router.delete("/schedule/{sid}")
def delete_schedule(sid: int, db: Session = Depends(get_db)) -> dict:
    sj = db.get(ScheduledJob, sid)
    if not sj:
        raise HTTPException(404, "no such schedule")
    jid = sj.job.get("job_id")
    db.delete(sj)
    scheduler.log_event(db, "removed", sid, jid)
    db.commit()
    return {"deleted": sid}


@router.post("/schedule/toggle-all")
def toggle_all(body: MasterToggle, db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(ScheduledJob)).all()
    now = scheduler.utcnow()
    for sj in rows:
        if body.enabled and sj.frequency == "once" and sj.last_status == "done":
            continue
        sj.enabled = body.enabled
        if body.enabled and sj.next_run is None and sj.frequency != "once":
            sj.next_run = max(now, sj.start_at or now)
    scheduler.log_event(db, "enabled" if body.enabled else "paused", None, None,
                        detail=f"toggle-all ({len(rows)})")
    db.commit()
    return {"updated": len(rows)}


@router.post("/schedule/{sid}/enable")
def enable_schedule(sid: int, db: Session = Depends(get_db)) -> dict:
    sj = db.get(ScheduledJob, sid)
    if not sj:
        raise HTTPException(404, "no such schedule")
    if sj.frequency == "once" and sj.last_status == "done":
        raise HTTPException(409, "one-shot already ran")
    sj.enabled = True
    now = scheduler.utcnow()
    if sj.next_run is None and sj.frequency != "once":
        sj.next_run = max(now, sj.start_at or now)
    scheduler.log_event(db, "enabled", sid, sj.job.get("job_id"))
    db.commit()
    db.refresh(sj)
    return _sched_dto(sj)


@router.post("/schedule/{sid}/pause")
def pause_schedule(sid: int, db: Session = Depends(get_db)) -> dict:
    sj = db.get(ScheduledJob, sid)
    if not sj:
        raise HTTPException(404, "no such schedule")
    sj.enabled = False
    scheduler.log_event(db, "paused", sid, sj.job.get("job_id"))
    db.commit()
    db.refresh(sj)
    return _sched_dto(sj)


@router.get("/schedule/logs")
def schedule_logs(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:
    limit = max(1, min(500, limit))
    rows = db.scalars(select(ScheduleLog).order_by(ScheduleLog.id.desc()).limit(limit)).all()
    return [_slog_dto(r) for r in rows]


@router.delete("/schedule/logs")
def clear_schedule_logs(db: Session = Depends(get_db)) -> dict:
    n = len(db.scalars(select(ScheduleLog)).all())
    db.execute(sa_delete(ScheduleLog))
    db.commit()
    return {"deleted": n}


# ── Control panel ─────────────────────────────────────────────────────────────

def _control_counts(db: Session) -> dict:
    rows = db.scalars(select(ScheduledJob)).all()
    enabled = sum(1 for s in rows if s.enabled)
    return {"total": len(rows), "enabled": enabled, "paused": len(rows) - enabled}


# Every category the crawler can emit — crawler.models.JobType. The panel is a CONTROL, not a
# report: an operator switching "tender" off before generating tender jobs is a legitimate thing to
# want, and deriving the list purely from jobs that already exist meant the switch only appeared
# after it was too late to be useful (measured: 4 jobs -> only news and profile were switchable).
# Kept as a literal rather than imported, because the orchestrator does not depend on the crawler
# package; the test below pins the two lists together so they cannot drift.
JOB_CATEGORIES = categories.BUCKETS


def _category_rows(db: Session) -> list[dict]:
    """One row per OPERATOR BUCKET — Manufacture / News / Tender / Think tank / Gov portal.
    `due_now` uses the same predicate as scheduler.due_jobs, so the number on the panel is exactly
    what the next tick would pick up.

    These used to be the crawler's job types (news/tender/profile/spec/patent_aux), which is the
    wrong vocabulary in two ways. It asked the operator to stop "profile" when what they wanted to
    stop was Manufacture — and worse, the mapping is many-to-one: News, Gov portal and Think tank
    ALL generate job_type "news", so no job-type switch can express "stop the ministries but keep
    the trade press". Keying on the source's own category is the only way that switch can mean what
    its label says.

    Always lists all five. A bucket with no jobs shows honest zeros — different from being absent,
    and its switch still works. A job whose source category we cannot place gets an `uncatalogued`
    row rather than being folded into a bucket that does not describe it.
    """
    now = scheduler.utcnow()
    off = scheduler.disabled_categories(db)
    by_source = categories.catalog_buckets(db)
    mk = lambda c: {"category": c, "label": categories.label(c), "enabled": c not in off,
                    "total": 0, "enabled_jobs": 0, "paused_jobs": 0, "due_now": 0}
    # Seeded with the known vocabulary AND the disabled set: a bucket switched off and then emptied
    # of jobs would otherwise vanish from the panel while still holding every future job, and one
    # that never had a job could not be pre-emptively switched off at all.
    rows: dict[str, dict] = {c: mk(c) for c in (*JOB_CATEGORIES, *off)}
    for sj in db.scalars(select(ScheduledJob)).all():
        r = rows.setdefault(categories.bucket_of_job(sj.job, by_source),
                            mk(categories.bucket_of_job(sj.job, by_source)))
        r["total"] += 1
        if sj.enabled:
            r["enabled_jobs"] += 1
            if sj.next_run is not None and sj.next_run <= now:
                r["due_now"] += 1
        else:
            r["paused_jobs"] += 1
    # Fixed operator order, not alphabetical: the panel should read the same as every other
    # category control on the site. Anything unexpected (uncatalogued, a legacy job-type key left
    # in Settings) sorts to the end where it is visible rather than mixed in.
    order = {c: i for i, c in enumerate(JOB_CATEGORIES)}
    return sorted(rows.values(), key=lambda r: (order.get(r["category"], 99), r["category"]))


@router.get("/control")
def control(db: Session = Depends(get_db)) -> dict:
    return {"master": scheduler.master_enabled(db), **_control_counts(db),
            "categories": _category_rows(db)}


@router.post("/control/master")
def set_master(body: MasterToggle, db: Session = Depends(get_db)) -> dict:
    """Master switch. Off means OFF — it ALWAYS stops the live crawl.

    It used to gate one thing only — whether the scheduler tick dispatches due jobs — so flipping it
    off while a crawl was in flight left that crawl running to completion. That was fixed by making
    the stop conditional on `flush`, which turned out to be the same bug wearing a hat: `flush` also
    DROPS the durable frontier, so the only way to stop the fleet was to agree to throw away its
    resume position, and an operator who declined that (reasonably — it is hours of work) got a
    master switch that showed OFF while twelve instances kept crawling. Measured: master off,
    categories off, and the crawl only actually stopped when maintenance mode was used instead.

    So the two decisions are now separate, which is what they always were:
      * OFF  -> stop every instance. Not optional, no dialog, no way to end up switched off and
                still crawling.
      * flush -> ALSO drop the saved resume position. Opt-in, and only ever reached after the stop
                has been confirmed complete.

    What it deliberately does NOT touch: captured pages. Documents and blobs are the product of
    every crawl so far, not "current" state, and no toggle should be able to destroy them. Wiping
    those is DELETE /sites?confirm=DELETE-ALL on the ingest API — separate, and explicit.
    """
    # The master switch IS the run state now (policy.py): ON is the explicit Start, OFF is the hard
    # Stop that outlives the teardown it causes and that nothing but Start lifts. This route keeps
    # its stop-then-WAIT-then-optionally-flush ordering; the plain Stop button (/control/stop) is
    # the same stop without the wait.
    scheduler.set_master(db, body.enabled)
    out: dict = {"master": body.enabled, "run": "running" if body.enabled else "stopped",
                 "stopped": False, "flushed": None, "detail": ""}
    if body.enabled:
        return out

    try:
        # BROADCAST. This called stop_crawl() with no base, so the master switch stopped INSTANCE 0
        # and left the other eleven crawling — the same partial-stop bug /api/crawler/stop was
        # already fixed for, and the more dangerous of the two because it then went on to flush the
        # frontier of a fleet that was still running.
        rows = crawler_client.fanout(crawler_client.stop_crawl)
        acked = [r for r in rows if r.get("ok")]
        if not acked:
            raise RuntimeError(str(rows[0].get("error") if rows else "no instance answered"))
        out["stopped"] = any((r.get("data") or {}).get("stopping") for r in acked)
        if len(acked) < len(rows):
            out["detail"] = (f"STOP INCOMPLETE — {len(rows)-len(acked)} of {len(rows)} instance(s) "
                             f"did not acknowledge; frontier NOT flushed")
            out["flushed"] = False
            return out
    except Exception as e:  # noqa: BLE001 — the switch itself must still flip
        out["detail"] = f"crawler unreachable, could not stop it: {e}"[:300]
        return out

    # A stop is asynchronous — the engine tears down a browser pool — so wait for it rather than
    # racing it, and wait REGARDLESS of `flush`. The operator's question is "did it actually stop",
    # and answering that needs the same poll the flush needed; reporting the switch as thrown while
    # instances are still fetching is precisely the lie that made this switch untrustworthy.
    deadline = time.monotonic() + _MASTER_STOP_WAIT_S
    busy: list[int] = []
    while True:
        try:
            # Fleet-wide: waiting on instance 0 alone would declare the crawl down while eleven
            # others were still tearing theirs down.
            busy = [r["instance"] for r in crawler_client.fanout(crawler_client.batch_status)
                    if r.get("ok") and (r.get("data") or {}).get("running")]
        except Exception:  # noqa: BLE001
            busy = []
            break
        if not busy or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    out["still_running"] = busy

    if busy:
        # NOT a failed stop: the signal is set and durable, so these instances will come down. But
        # they are still holding workers, so nothing may be cleared out from under them.
        out["flushed"] = False if body.flush else None
        out["detail"] = ("stop signalled to every instance; %s still winding down after %ss (they "
                         "hold workers inside the item watchdog). Nothing new will be fetched. "
                         % (", ".join("i%d" % (i + 1) for i in busy), _MASTER_STOP_WAIT_S)
                         + ("The resume state was NOT cleared, so nothing is lost — clear it from "
                            "the Resume tab once they are down." if body.flush else ""))
        return out

    out["detail"] = "stopped — every instance is down"
    if not body.flush:
        # The frontier is deliberately KEPT. Say so: an operator who has just watched this switch
        # fail twice needs to be told what state the machine is in, not left to infer it.
        out["detail"] += "; resume position kept (nothing to re-crawl from scratch)"
        return out

    try:
        r = crawler_client.clear_resumable()
        out["flushed"] = True
        out["detail"] = f"stopped. Resume state cleared — {r.get('jobs', 0)} job(s), " \
                        f"{r.get('rows', 0)} queued URL(s)"
    except Exception as e:  # noqa: BLE001
        out["flushed"] = False
        out["detail"] = f"crawl stopped, but the resume state could not be cleared: {e}"[:300]
    return out


# A stop tears down a browser pool and flushes the frontier, so it is not instant. Long enough for
# a real teardown, short enough that the operator is not left staring at a spinner.
_MASTER_STOP_WAIT_S = 45


# ── Flush: reset the machine to "never crawled" after a test ──────────────────
# Run data only. The catalog, the schedule and the keywords are what the operator BUILT; a button
# that quietly destroyed those would make the next test start with re-importing sources. Everything
# below is something a crawl produced.
#
# Deleting the documents alone is the trap: crawl_pages still holds every URL's content_hash, so
# the next crawl classifies each page "unchanged" and stores nothing. Anyone reaching for a subset
# of these steps should know that the store and the history have to go together or the machine ends
# up in a state where it has no data and refuses to collect any.

def _flush_targets() -> list[tuple[str, str, str]]:
    """(key, human label, what it costs to lose) — the single source of truth for the preview and
    the flush, so the dialog cannot promise something the endpoint does not do."""
    return [
        ("documents", "Postgres documents", "every captured page on this machine"),
        ("blobs", "MinIO blobs", "captured PDFs"),
        ("history", "crawl history", "content hashes + fetched URLs — without this the next crawl skips everything"),
        ("resumable", "resume state", "cut-short crawls can no longer be resumed"),
        ("issues", "issues log", "the unresolved-URL list"),
        ("runlogs", "scheduler run logs + site measurements",
         "dispatch/outcome history and the per-run measurements the Logs tab and the learned "
         "cadence are built from (the jobs themselves stay)"),
    ]


@router.get("/control/flush/preview")
def flush_preview(db: Session = Depends(get_db)) -> dict:
    """Real counts for the confirm dialog. Every number is fetched, never assumed — a step whose
    service is unreachable reports null so the dialog can say "unknown" instead of a confident 0.
    """
    out: dict = {"counts": {}, "errors": {}, "targets": [
        {"key": k, "label": lbl, "cost": cost} for k, lbl, cost in _flush_targets()]}
    try:
        s = _ingest_get("/api/live/summary")
        out["counts"]["documents"] = (s.get("postgres") or {}).get("documents")
        out["counts"]["blobs"] = (s.get("minio") or {}).get("objects")
    except Exception as e:  # noqa: BLE001
        out["errors"]["store"] = str(e)[:200]
    try:
        out["counts"]["resumable"] = crawler_client.resumable().get("count")
    except Exception as e:  # noqa: BLE001
        out["errors"]["resumable"] = str(e)[:200]
    try:
        # All THREE tables the runlogs step clears. Counting only ScheduleLog understated the
        # number, and understating it is how the dialog promised less than the flush did —
        # the opposite failure to the one below, and just as misleading.
        out["counts"]["runlogs"] = sum(len(db.scalars(select(m)).all())
                                       for m in (ScheduleLog, JobRun, SiteMeasurement))
    except Exception as e:  # noqa: BLE001
        out["errors"]["runlogs"] = str(e)[:200]
    # crawl history has no cheap count endpoint; the flush reports what it actually deleted.
    out["counts"]["history"] = None
    try:
        # Fleet-wide: instance 0 being idle is not the fleet being idle, and this number decides
        # whether the operator is shown a "safe to flush" dialog.
        out["crawl_running"] = bool(_fleet_running()["batch_running"])
    except Exception:  # noqa: BLE001
        out["crawl_running"] = False
    return out


@router.post("/control/flush")
def flush_all(confirm: str = "", db: Session = Depends(get_db)) -> dict:
    """Delete everything this machine collected, keeping the catalog/schedule/keywords.

    Refuses while a crawl is running rather than stopping it: a flush mid-crawl races the engine,
    which is still writing history rows and frontier positions, and the operator would get a
    half-cleared machine with no way to tell which half. Stop it first, then flush.

    Every step is attempted even if an earlier one failed, and each reports its own outcome. A
    partial flush is a real state and has to be legible — reporting one blanket "ok" after the
    third step failed is how someone ends up with documents deleted and history intact, which is
    the one combination that silently breaks the next crawl.

    The data centre is NEVER touched. It holds other machines' documents; this button is local.
    """
    if confirm != "FLUSH":
        raise HTTPException(400, "pass ?confirm=FLUSH — this deletes every document on this machine")
    try:
        # ANY instance, not instance 0. This guard protects a destructive operation from racing the
        # engine, and checking one of twelve made it a coin flip.
        st = _fleet_running()
        if st["batch_running"]:
            raise HTTPException(409, "a crawl is running on instance(s) %s — stop it first, then "
                                     "flush" % ", ".join("i%d" % (i + 1)
                                                         for i in st["running_instances"]))
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — crawler down means nothing is writing; carry on
        pass

    steps: list[dict] = []

    def step(key: str, label: str, fn) -> None:
        try:
            steps.append({"key": key, "label": label, "ok": True, "detail": str(fn())[:300]})
        except Exception as e:  # noqa: BLE001
            steps.append({"key": key, "label": label, "ok": False, "detail": str(e)[:300]})

    def _wipe_store():
        import httpx
        base = get_settings().ingest_api.rstrip("/")
        r = httpx.delete(f"{base}/sites", params={"confirm": "DELETE-ALL"}, timeout=120.0)
        r.raise_for_status()
        return r.json()

    # Both wipes BROADCAST. Each instance keeps its own crawl history and issue log in its own data
    # directory, so a flush that hit only instance 0 would report "everything on this machine was
    # deleted" and leave every other instance's history intact — after which the next crawl skips
    # those pages as unchanged and the operator cannot see why. A partial flush is the worst
    # possible half-state, so a failure on any instance raises rather than reporting a clean wipe.
    def _wipe_all(path: str, timeout: float) -> dict:
        import httpx
        total, per = 0, []
        for i, base in enumerate(crawler_client.pool()):
            r = httpx.post(f"{base.rstrip('/')}{path}", params={"confirm": "FLUSH"}, timeout=timeout)
            r.raise_for_status()
            d = r.json()
            per.append({"instance": i, **(d if isinstance(d, dict) else {"result": d})})
            for k in ("deleted", "cleared", "rows", "removed"):
                total += int((d or {}).get(k) or 0) if isinstance(d, dict) else 0
        return {"instances": per, "total": total}

    def _wipe_history():
        return _wipe_all("/v1/history/flush", 120.0)

    def _wipe_issues():
        return _wipe_all("/v1/issues/clear", 60.0)

    def _wipe_runlogs():
        n_log = len(db.scalars(select(ScheduleLog)).all())
        n_run = len(db.scalars(select(JobRun)).all())
        # SiteMeasurement is the one the Logs tab actually reads — BOTH its panels are built from
        # it (`/logs/runs` entirely, `/logs/sites` for the "history" source). Deleting only the
        # other two left a flush that reported "everything collected on this machine was deleted"
        # and then listed every one of those runs on the next tab. Measured: documents 0, blobs 0,
        # job_runs 0, schedule_logs 0 — and site_measurements still 2, still on screen.
        #
        # It is also the table the tiering reads (change_rate / changes_per_day / interval_days),
        # so a measurement kept past its documents would go on deriving a crawl cadence from pages
        # that no longer exist. Same reasoning as the last_run_at reset below, one table over.
        n_meas = len(db.scalars(select(SiteMeasurement)).all())
        db.execute(sa_delete(ScheduleLog))
        db.execute(sa_delete(JobRun))
        db.execute(sa_delete(SiteMeasurement))
        # The jobs SURVIVE — only their run history goes. Their per-job status is reset so the
        # panel does not show "ok, last run 2h ago" for a run whose log and documents are gone.
        for sj in db.scalars(select(ScheduledJob)).all():
            sj.last_status, sj.last_run_at = "pending", None
        db.commit()
        return {"schedule_logs": n_log, "job_runs": n_run, "site_measurements": n_meas}

    step("store", "Postgres documents + MinIO blobs", _wipe_store)
    step("history", "crawl history (content hashes + fetched URLs)", _wipe_history)
    step("resumable", "resume state", crawler_client.clear_resumable)
    step("issues", "issues log", _wipe_issues)
    step("runlogs", "scheduler run logs", _wipe_runlogs)

    failed = [s for s in steps if not s["ok"]]
    return {"ok": not failed, "steps": steps,
            "kept": "sources catalog, scheduled jobs, keywords, learned per-host hints, "
                    "browser sessions, and the data centre",
            "detail": ("everything collected on this machine was deleted"
                       if not failed else
                       f"{len(failed)} of {len(steps)} step(s) FAILED — this machine is "
                       f"half-cleared; see steps[] and re-run once the cause is fixed")}


@router.get("/control/categories")
def list_categories(db: Session = Depends(get_db)) -> list[dict]:
    return _category_rows(db)


def _job_ids_in_disabled_categories(db: Session) -> list[str]:
    """Every job_id the crawler must NOT be working on, given the current switches.

    Recomputed in full from the switches each time rather than tracked incrementally: the crawler
    replaces its drop set wholesale, so this is the one place the answer has to be right, and a
    full recompute cannot drift out of sync with the panel the operator is looking at."""
    off = scheduler.disabled_categories(db)
    if not off:
        return []
    by_source = categories.catalog_buckets(db)
    out = []
    for sj in db.scalars(select(ScheduledJob)).all():
        if categories.bucket_of_job(sj.job, by_source) in off:
            jid = (sj.job or {}).get("job_id")
            if jid:
                out.append(str(jid))
    return sorted(set(out))


def _push_drop_set(db: Session) -> dict:
    """Tell every instance which jobs are switched off. Never raises: the switch itself must flip
    even with a crawler down, and a down instance is reported rather than swallowed."""
    ids = _job_ids_in_disabled_categories(db)
    rows = crawler_client.fanout(crawler_client.drop_jobs, ids)
    ok = [r for r in rows if r.get("ok")]
    return {"held_jobs": len(ids), "instances_acked": len(ok), "instances_total": len(rows),
            "unreachable": [r["instance"] for r in rows if not r.get("ok")]}


@router.post("/control/categories")
def toggle_category(body: CategoryToggle, db: Session = Depends(get_db)) -> dict:
    """Stop/start a whole category — INCLUDING whatever is crawling right now.

    It used to write the switch and stop there. The switch is only consulted when the scheduler
    decides what to DISPATCH, so a category switched off mid-crawl kept crawling to exhaustion:
    hours of a category the operator had visibly turned off, with the panel showing it disabled the
    whole time. That is indistinguishable from a broken switch, and it is why "just use the master
    switch" and then "just use maintenance mode" became the working answer.

    Now the live jobs of a disabled category are pushed to every instance as a drop set, so the
    crawler releases their URLs unprocessed. Their queued URLs stay pending and the jobs end
    `stopped`, so nothing is lost — switching the category back on lets the scheduler pick them up
    from where they were on the next tick. Other categories in the same batch are untouched, which
    is the whole point and the thing neither of the other two switches can do.
    """
    known = set(JOB_CATEGORIES) | {categories.UNCATALOGUED}
    known |= scheduler.disabled_categories(db)   # an off switch must always be re-flippable
    if body.category not in known:
        raise HTTPException(400, f"unknown category '{body.category}' (have: {sorted(known)})")
    scheduler.set_category(db, body.category, body.enabled)
    # Fallback row, not next() bare: re-ENABLING a category whose jobs were all deleted leaves it
    # in neither the disabled set nor scheduled_jobs, and a bare next() would 500 on that.
    blank = {"category": body.category, "label": categories.label(body.category),
             "enabled": body.enabled, "total": 0,
             "enabled_jobs": 0, "paused_jobs": 0, "due_now": 0}
    row = next((r for r in _category_rows(db) if r["category"] == body.category), blank)
    try:
        row["stop"] = _push_drop_set(db)
    except Exception as e:  # noqa: BLE001 — the switch is already written; report, don't unwind
        row["stop"] = {"error": str(e)[:200]}
    return row


def _fleet_running() -> dict:
    """Is ANY instance crawling — and which. Every "is a crawl running" answer on this service has
    to come from here.

    Asking `batch_status()` with no base is instance 0 of twelve, which is how the control panel
    reported "no crawl running" over a live eleven-instance crawl, and how the master-switch dialog
    failed to warn that a crawl was in flight. Same lie shape as the partial stop it sits next to:
    the fleet is the unit, never one member of it.
    """
    rows = crawler_client.fanout(crawler_client.batch_status)
    up = [r for r in rows if r.get("ok")]
    running = [r["instance"] for r in up if (r.get("data") or {}).get("running")]
    dropped: set[str] = set()
    for r in up:
        dropped |= set((r.get("data") or {}).get("dropped_jobs") or [])
    return {"reachable": bool(up), "batch_running": bool(running), "running_instances": running,
            "instances": {"up": len(up), "down": len(rows) - len(up), "total": len(rows)},
            # A crawl running with jobs held off by a category switch is a PARTIAL state, and the
            # panel has to be able to say so rather than showing a plain green "crawling".
            "held_jobs": len(dropped)}


@router.get("/control/state")
def control_state(request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        crawler = _fleet_running()
    except Exception:  # noqa: BLE001
        crawler = {"reachable": False, "batch_running": False, "running_instances": [],
                   "instances": {"up": 0, "down": 0, "total": 0}, "held_jobs": 0}
    nxt = db.scalar(
        select(ScheduledJob.next_run)
        .where(ScheduledJob.enabled.is_(True), ScheduledJob.next_run.is_not(None))
        .order_by(ScheduledJob.next_run).limit(1))
    task = getattr(request.app.state, "scheduler_task", None)
    st = policy.state(db)
    return {"master": scheduler.master_enabled(db),
            # THE CONTROL PLANE (policy.py): run state, mode, the lanes that opens, the toggles.
            # Everything below `mode` is reported next to it so a fleet that will not start is
            # explained on the panel — "stopped", "campaign mode holds the daily lane", "the
            # scheduled toggle is off" — instead of being a mystery to diagnose from logs.
            "run": st["run"],
            "mode": st["mode"],
            "modes": st["modes"],
            "lanes": st["lanes"],
            "auto": st["auto"],
            # The two boot-time env kill-switches. CRAWLER_AUTO_REFILL=0 / CRAWLER_AUTOPILOT=0
            # stop the LOOP from starting at all, and a toggle over a loop that never runs is a
            # dead control — worse than an absent one. Reported so the panel can say so.
            "loops": _loops_enabled(),
            # The other holds, reported alongside rather than only on their own panels. An operator
            # looking at a fleet that will not start needs to see WHICH one is holding it.
            "halted": scheduler.halted(db),
            "maintenance": scheduler.maintenance(db),
            "disabled_categories": sorted(scheduler.disabled_categories(db)),
            "manual_batch": {k: orchestrate.manual_status().get(k)
                             for k in ("running", "lane", "total", "done", "halted")},
            "campaign": campaigns.active(),
            "dispatch": scheduler.dispatch_policy(db),
            "scheduler_alive": bool(task and not task.done()),
            "tick_seconds": get_settings().scheduler_tick,
            "crawler": crawler,
            "next_due": _iso(nxt),
            **_control_counts(db)}


def _loops_enabled() -> dict:
    """Which timer loops the environment let start. Deferred imports: both modules import api."""
    from . import autopilot, refill
    return {"refill": refill.enabled(), "autopilot": autopilot.enabled(),
            "scheduled": True, "health_dispatch": autopilot.enabled(), "repin": True}


# ── THE control plane: Stop / Start, the mode switch, the auto-behaviour toggles ──────────────
# Four small routes over policy.py. Every path that can put work on an instance reads the state
# these write, through scheduler.lane_open, before it sends anything; nothing else writes it.

def _stop_fleet() -> dict:
    """The graceful fleet-wide stop: every instance drains and saves its frontier. Frontiers are
    KEPT — nothing here flushes. Reported per instance, because a stop that one container did not
    hear is the worst outcome available and must never read as "stopped"."""
    rows = crawler_client.fanout(crawler_client.stop_crawl)
    bad = [r for r in rows if not r.get("ok")]
    return {"acked": len(rows) - len(bad), "total": len(rows),
            "stopping": any((r.get("data") or {}).get("stopping") for r in rows if r.get("ok")),
            "failed": [{"instance": r["instance"], "error": r.get("error")} for r in bad]}


@router.post("/control/stop")
def control_stop(db: Session = Depends(get_db)) -> dict:
    """STOP. Halts every instance now, closes every lane, and hard-disables every timer — the
    scheduler tick, auto-refill, the autopilot, the daily re-pin — until an explicit Start.

    The state is written BEFORE the broadcast and it is durable: the crawler's own stop flag is
    consumed as the engine winds down, so anything reading the crawler alone sees plain idleness a
    minute later and would put the fleet back to work. Nothing does now — not the refill timer,
    not the autopilot, not maintenance-off, not a pasted batch, not a resume, not a placer script
    calling any of those routes — because every one of them reads this state first and none of
    them can write it. The mode is left alone: Stop is WHETHER, the mode is WHAT.
    """
    changed = policy.stop(db, reason="Stop pressed — every lane closed until Start")
    out = {"run": "stopped", "changed": changed, "mode": policy.mode(db), "stop": _stop_fleet()}
    if out["stop"]["acked"] == 0 and out["stop"]["total"]:
        out["detail"] = ("STOPPED here, but no crawler instance acknowledged the stop: they keep "
                         "their current batch until they answer. Nothing new will be dispatched.")
    # A running CAMPAIGN ends here too, and the mode returns to normal: "if I stop the campaign
    # and start my normal crawl, it should just work" — Start is then all that is left to press.
    camp = campaigns.mark_stopped(db, "Stop pressed")
    if camp is not None:
        out["campaign"] = {"id": camp.id, "name": camp.name, "status": camp.status}
        out["mode"] = policy.mode(db)
    return out


@router.post("/control/start")
def control_start(db: Session = Depends(get_db)) -> dict:
    """START. The one thing that lifts a stop. Opens the current mode's lanes; the timers with
    their toggle on resume on their next tick. Dispatches nothing itself — the daily lane, the
    scheduler tick and the refill watcher pick up within their own tick, and a campaign starts when
    the operator submits its batch."""
    changed = policy.start(db)
    st = policy.state(db)
    note = None
    if scheduler.maintenance(db):
        note = "started, but maintenance mode is still on — every lane stays closed until it is off"
    return {"run": "running", "changed": changed, "mode": st["mode"], "lanes": st["lanes"],
            "auto": st["auto"], "detail": note}


@router.get("/control/stop/status")
def control_stop_status(db: Session = Depends(get_db)) -> dict:
    """Is the fleet actually stopped YET? — the missing feedback behind "I pressed Stop and can't
    tell if it worked".

    Stop is durable the instant it is pressed (the run-state flips and no timer can undo it), but
    the instances take real time to wind their current pages down. This polls every instance's
    batch status and reports the teardown as a phase the UI can show:
      running  — a crawl is still going (Stop not pressed, or nothing to stop)
      stopping — Stop is in effect but at least one instance is still finishing its current work
      stopped  — every reachable instance has gone idle; the stop is complete
    An instance that never answers is called out separately: it keeps its batch until it does, so
    the honest state is "stop incomplete", never a false "stopped".
    """
    rows = crawler_client.fanout(crawler_client.batch_status)
    reachable = [r for r in rows if r.get("ok")]
    still = [r for r in reachable if (r.get("data") or {}).get("running")]
    unreachable = [r for r in rows if not r.get("ok")]
    stopped_flag = policy.stopped(db)

    if not stopped_flag and still:
        phase = "running"
    elif reachable and not still:
        phase = "stopped"
    elif still:
        phase = "stopping"
    else:
        phase = "unknown"   # the run-state says stopped but no instance answered

    out = {
        "run": "stopped" if stopped_flag else "running",
        "phase": phase,
        "total": len(rows),
        "idle": len(reachable) - len(still),
        "still_running": len(still),
        "unreachable": len(unreachable),
        "instances": [
            {"instance": r["instance"],
             "running": bool((r.get("data") or {}).get("running")) if r.get("ok") else None,
             "reachable": bool(r.get("ok"))}
            for r in rows],
    }
    if unreachable:
        out["detail"] = (f"{len(unreachable)} of {len(rows)} machine(s) did not answer — each keeps "
                         "its current work until it does, so the stop is not fully confirmed for them.")
    return out


@router.post("/control/mode")
def control_mode(body: ModeSwitch, db: Session = Depends(get_db)) -> dict:
    """The MODE switch: exactly one of normal / campaign is active.

    Switching STOPS the other mode's work in flight (graceful, frontiers kept) so the fleet is free
    for the new mode, and the resume lane is scoped by ownership so the old mode's frontiers stay
    PARKED rather than creeping back through refill. Entering normal mode additionally pins every
    daily domain back onto a lane instance ONCE, so a rebalance run during the campaign cannot
    leave daily sites orphaned on instances the lane never dispatches to. That is the whole of
    "nothing lingering": no revived backlog, no orphaned pins, and no third row left behind.

    Does not touch the run state. Switching while stopped is how a campaign is set up before
    Start; switching while running takes effect on the next tick.
    """
    try:
        changed = policy.set_mode(db, body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out: dict = {"mode": policy.mode(db), "changed": changed, "run": policy.state(db)["run"]}
    if not changed:
        out["detail"] = "already in %s mode — nothing changed" % out["mode"]
        return out
    # In-flight work belongs to the mode that dispatched it. Down it comes, frontiers saved: the
    # same graceful broadcast as Stop, without changing the run state.
    out["stop"] = _stop_fleet()
    if out["mode"] == "normal":
        from . import daily
        try:
            out["pinned"] = daily.pin_to_lane(db)
        except Exception as e:  # noqa: BLE001 — the switch must still land; say what did not
            out["pinned"] = {"error": str(e)[:200]}
    # A manual batch mid-way through its list halts at its next chunk by reading the lane; its
    # status says so. Reported here so the switch explains itself.
    if orchestrate.manual_status().get("running"):
        out["manual_batch"] = "a batch is running; it halts before its next chunk"
    # Leaving campaign mode ends the running campaign (its thread sees the closed lane on its
    # next poll); the launcher's row says why.
    if out["mode"] == "normal":
        camp = campaigns.mark_stopped(db, "mode switched to normal")
        if camp is not None:
            out["campaign"] = {"id": camp.id, "name": camp.name, "status": camp.status}
    out["lanes"] = policy.state(db)["lanes"]
    return out


@router.get("/control/auto")
def control_auto(db: Session = Depends(get_db)) -> dict:
    return {"run": policy.state(db)["run"], "auto": policy.autos(db)}


@router.post("/control/auto")
def control_auto_set(body: AutoToggle, db: Session = Depends(get_db)) -> dict:
    """Switch one auto-behaviour on or off (policy.AUTOS). `enabled` is stored; `effective` is what
    the timer will actually do, which is nothing while the pipeline is stopped."""
    try:
        policy.set_auto(db, body.name, body.enabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run": policy.state(db)["run"], "auto": policy.autos(db)}


@router.get("/control/dispatch")
def get_dispatch_policy(db: Session = Depends(get_db)) -> dict:
    """Read-only: the lanes DERIVED from the run state and the mode."""
    return scheduler.dispatch_policy(db)


@router.post("/control/dispatch")
def set_dispatch_policy() -> dict:
    """Retired. The lanes are derived from Stop/Start and the mode, never set directly — a lanes
    row that outlived a stop or a mode switch was one more thing left behind to fight them."""
    raise HTTPException(410, "retired: use POST /api/control/stop, /api/control/start and "
                             "/api/control/mode {\"mode\": \"normal\"|\"campaign\"}")


# ── Campaign launcher: paste a list -> plan -> approve -> run (campaigns.py) ─────────────────
# The plan is an artefact the operator reads and approves; Go dispatches exactly that artefact.

def _campaign_or_404(db: Session, cid: int) -> Campaign:
    camp = db.get(Campaign, cid)
    if camp is None:
        raise HTTPException(404, "no such campaign")
    return camp


@router.get("/campaigns")
def campaigns_list(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Campaign).order_by(Campaign.updated_at.desc())).all()
    return {"campaigns": [campaigns.dto(c, with_plan=False) for c in rows],
            "defaults": campaigns.default_options(), "scopes": campaigns.SCOPES,
            "tiers": campaigns.TIERS, "active": campaigns.active(),
            "mode": policy.mode(db), "run": policy.state(db)["run"]}


@router.post("/campaigns/plan")
def campaigns_plan(body: CampaignPlanRequest, db: Session = Depends(get_db)) -> dict:
    """Tier every site (known verdicts first, then ONE probe each), build the phase jobs and the
    estimates, and SAVE the result as a draft. Dispatches nothing."""
    try:
        plan = campaigns.build_plan(body.sites, body.options, measured=campaigns.measured_pages(db))
    except ValueError as e:
        raise HTTPException(400, str(e))
    camp = campaigns.save_plan(db, body.name, body.sites, body.options, plan)
    return campaigns.dto(camp)


@router.get("/campaigns/{cid}")
def campaigns_get(cid: int, db: Session = Depends(get_db)) -> dict:
    camp = _campaign_or_404(db, cid)
    out = campaigns.dto(camp)
    out["progress"] = campaigns.progress(db, camp)
    return out


@router.put("/campaigns/{cid}")
def campaigns_update(cid: int, body: CampaignUpdate, db: Session = Depends(get_db)) -> dict:
    """Tweak a saved campaign — name, list or options — and rebuild its plan. Not while running."""
    camp = _campaign_or_404(db, cid)
    if camp.status == "running":
        raise HTTPException(409, "the campaign is running — stop it before editing")
    sites = body.sites if body.sites is not None else camp.sites
    options = body.options if body.options is not None else camp.options
    try:
        plan = campaigns.build_plan(sites, options, measured=campaigns.measured_pages(db))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return campaigns.dto(campaigns.save_plan(db, body.name, sites, options, plan, camp=camp))


@router.delete("/campaigns/{cid}")
def campaigns_delete(cid: int, db: Session = Depends(get_db)) -> dict:
    camp = _campaign_or_404(db, cid)
    if camp.status == "running":
        raise HTTPException(409, "the campaign is running — stop it before deleting")
    db.delete(camp)
    db.commit()
    return {"deleted": cid}


@router.post("/campaigns/{cid}/go")
def campaigns_go(cid: int, db: Session = Depends(get_db)) -> dict:
    """GO: the approval. Switches to campaign mode (normal work in flight is stopped, frontiers
    kept), starts the pipeline, and dispatches the saved plan phase by phase to the instances
    that own each domain. One campaign at a time."""
    camp = _campaign_or_404(db, cid)
    if scheduler.maintenance(db):
        raise HTTPException(409, "maintenance mode is on — turn it off before launching")
    try:
        return campaigns.launch(db, camp, stop_fleet=_stop_fleet)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.post("/campaigns/{cid}/stop")
def campaigns_stop(cid: int, db: Session = Depends(get_db)) -> dict:
    """The hard stop, from the launcher: every instance halts, the pipeline STAYS stopped until
    Start, and the mode returns to normal with the campaign's frontiers parked."""
    camp = _campaign_or_404(db, cid)
    out = control_stop(db=db)
    campaigns.close(db, camp, "stopped from the launcher")
    db.refresh(camp)
    return {**out, "campaign": campaigns.dto(camp)}


# ── Job generation from an uploaded CSV/Excel: probe → drafts → confirm ───────
# The probe measures what it can from ONE fetch (~86% accurate — the honest ceiling), so the
# REVIEW step is load-bearing, not garnish: low-confidence rows sort to the top for a human call.

class ProbeRow(BaseModel):
    url: str
    job_type: str = "news"
    max_pages: int | None = None
    max_depth: int | None = None
    freshness_days: int | None = None
    category: str | None = None
    frequency: str | None = None


class ProbeRows(BaseModel):
    rows: list[ProbeRow]
    probe: bool = True
    parallel: int = 32


# Per-class budgets, taken from the operator's own labelled job files rather than invented.
_PURE_DEFAULTS = {"max_pages": 100000, "max_depth": -1, "freshness_days": None}
_MIXED_DEFAULTS = {"max_pages": 4000, "max_depth": 3, "freshness_days": 120}


@router.post("/jobs/generate-from-probe")
def generate_from_probe(body: ProbeRows, db: Session = Depends(get_db)) -> dict:
    """CSV/Excel rows → one quick probe per URL → DRAFT jobs whose keyword-gate / render_js /
    path_scope are MEASURED, not guessed. Commits nothing — the operator reviews, then confirms."""
    rows = [r for r in body.rows if (r.url or "").strip()]
    if not rows:
        raise HTTPException(400, "no rows")
    probes: dict = {}
    if body.probe:
        try:
            resp = crawler_client.probe_sites([r.url for r in rows], parallel=body.parallel)
            probes = {p.get("url"): p for p in resp.get("results", [])}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"probe failed: {str(e)[:200]}")

    drafts, review_required = [], 0
    for i, row in enumerate(rows, 1):
        pr = probes.get(row.url) or {}
        rec = pr.get("recommend") or {}
        res = resolve_source(row.url)
        gate = bool(rec.get("keyword", False))
        scoped = bool(rec.get("path_scope"))
        # NOTE on defence SUBDOMAINS (defence.economictimes.indiatimes.com): no special handling is
        # needed. canonicalize.registered_domain() is deliberately HOST-EXACT (it only strips
        # "www."), so same_site()/same_domain_only confine the crawl to that exact host — it can
        # never wander into the parent publisher's markets/sport. Gating them "just in case" would
        # silently drop real pages on what is, within its own host, a pure-defence site.
        # Budget class keys off "is this a publisher SECTION?" — true for a path_scope site, which
        # gets the bounded budget rather than a pure site's unlimited crawl.
        dflt = _MIXED_DEFAULTS if (gate or scoped) else _PURE_DEFAULTS
        prov = {"keyword": "probe" if rec else "default", "render_js": "probe" if rec else "default",
                "path_scope": "probe" if rec.get("path_scope") else "default", "job_type": "csv"}

        def pick(name: str, csv_val, default_val):
            if csv_val is not None:
                prov[name] = "csv"
                return csv_val
            prov[name] = "default:mixed" if (gate or scoped) else "default:pure"
            return default_val

        job = {
            "job_id": f"gen_{res.source_id}_{row.job_type}".lower().replace(" ", "_"),
            "job_type": row.job_type,
            "seed_urls": [row.url],
            # NEVER assign a per-URL keyword list. The gate evaluates against the GLOBAL corpus
            # (gate.evaluate uses ctx.kp, not job.keywords), so the `keyword` bool is the whole
            # control: true ⇒ keep only pages hitting the full corpus, false ⇒ keep everything.
            # `keywords` is a different mechanism (link-relevance / search seeding) and populating
            # it here would only muddy that distinction.
            "keywords": [],
            "keyword": gate,                       # ← pure ⇒ false (capture all), mixed ⇒ true
            "render_js": bool(rec.get("render_js", False)),
            "path_scope": rec.get("path_scope") or [],
            "max_pages": pick("max_pages", row.max_pages, dflt["max_pages"]),
            "max_depth": pick("max_depth", row.max_depth, dflt["max_depth"]),
            "freshness_days": pick("freshness_days", row.freshness_days, dflt["freshness_days"]),
            "same_domain_only": True,
            "capture": jobgen._CAPTURE.get(row.job_type, ["html", "text"]),
            "source_id": res.source_id, "source_tier": res.tier,
            "source_type": row.category or res.category,
        }
        if row.job_type == "tender":
            job["max_pdfs_per_page"] = 0   # never truncate a tender listing's RFP table
        if rec.get("review_required", not rec):
            review_required += 1
        drafts.append({
            "draft_id": f"d{i}", "url": row.url,
            "source": {"domain": res.domain, "source_id": res.source_id,
                       "category": row.category or res.category, "tier": res.tier},
            "probe": {"confidence": rec.get("confidence", "low"),
                      "review_required": bool(rec.get("review_required", not rec)),
                      "evidence": pr.get("evidence", "not probed"),
                      "status": pr.get("status"), "error": pr.get("error")},
            "job": job, "provenance": prov, "frequency": row.frequency or "tiered",
        })
    drafts.sort(key=lambda d: (d["probe"]["confidence"] != "low", d["url"]))  # uncertain first
    return {"count": len(drafts), "probed": len(probes),
            "review_required": review_required, "drafts": drafts}


class ConfirmDraft(BaseModel):
    url: str
    job: dict
    frequency: str = "tiered"
    category: str | None = None


class ConfirmBody(BaseModel):
    drafts: list[ConfirmDraft]


@router.post("/jobs/confirm")
def confirm_drafts(body: ConfirmBody, db: Session = Depends(get_db)) -> dict:
    """Commit reviewed drafts: upsert the Source, register a ScheduledJob on the adaptive `tiered`
    cadence (baseline census first, then measured), and create its cadence policy row."""
    if not body.drafts:
        raise HTTPException(400, "no drafts")
    now = scheduler.utcnow()
    made = []
    for d in body.drafts:
        src = orchestrate.upsert_source(db, d.url, frequency="daily", category=d.category)
        job = dict(d.job)
        job.setdefault("source_id", src.source_id)
        db.add(ScheduledJob(
            name=src.domain, job=job, job_type=job.get("job_type", "news"),
            frequency=(d.frequency if d.frequency in _FREQS else "tiered"),
            retry_count=1, retries_left=1, start_at=None, next_run=now,
            last_status="pending", enabled=True, created_at=now))
        made.append(src.domain)
    db.commit()
    created = tiering.ensure_site_schedules(db)
    return {"scheduled": len(made), "policies_created": created, "domains": made}


# ── Adaptive cadence (tiering) ────────────────────────────────────────────────

def _pol_dto(p: SiteSchedule) -> dict:
    return {
        "domain": p.domain, "source_id": p.source_id,
        "phase": p.phase, "tier": p.tier, "tier_reason": p.tier_reason,
        "defence_score": round(float(p.defence_score or 0.0), 3),
        "lambda_per_day": round(float(p.lambda_ewma), 4) if p.lambda_ewma is not None else None,
        "change_rate": round(float(p.change_rate_ewma), 3) if p.change_rate_ewma is not None else None,
        "measurements": int(p.measurements or 0),
        "assigned_weekday": p.assigned_weekday, "assigned_dom": p.assigned_dom,
        "careful": bool(p.careful), "sentinel": bool(p.sentinel),
        "est_seconds": round(float(p.seconds_ewma or 0.0)),
        "last_success_at": _iso(p.last_success_at),
    }


@router.get("/schedule/policies")
def schedule_policies(db: Session = Depends(get_db)) -> list[dict]:
    """Per-site cadence policy: which tier each site is on, WHY, and which day it runs."""
    return [_pol_dto(p) for p in db.scalars(select(SiteSchedule).order_by(SiteSchedule.domain)).all()]


@router.post("/schedule/reconcile")
def schedule_reconcile(db: Session = Depends(get_db)) -> dict:
    """Create a cadence policy row for every Source that lacks one. Idempotent — this is the
    no-Alembic migration path (new TABLE, never a new column)."""
    made = tiering.ensure_site_schedules(db)
    return {"created": made, "total": len(db.scalars(select(SiteSchedule)).all())}


@router.get("/schedule/forecast")
def schedule_forecast(window_hours: float = 8.0, db: Session = Depends(get_db)) -> dict:
    """Steady-state daily crawl load in crawl-SECONDS with a p90 band. `sizing.
    required_parallel_workers` is the number to size hardware from — a site COUNT can't, because
    one careful .gov host costs 20-50x a news site."""
    return tiering.forecast(db, window_hours=window_hours)


# ── Crawler proxies ───────────────────────────────────────────────────────────

@router.get("/sipri/summary")
def sipri_summary() -> dict:
    """The SIPRI harvest, proxied so the Watchfloor can render it same-origin.

    SIPRI is a separate stack on its own Docker network and serves no CORS headers, so the browser
    cannot read it from this page — the tab falls back to embedding its console. This route is the
    fix, and it needs the orchestrator attached to that network:

        docker network connect mallory-sipri_default mallory-crawler-orchestrator-1

    which Docker does to a RUNNING container, no restart. Unreachable is reported as unreachable;
    the tab renders its fallback rather than a zero, because "no SIPRI here" and "SIPRI holds
    nothing" are opposite facts.

    `attachments` is counted here and nowhere else: the SIPRI app's own /api/summary predates the
    PDF backfill and its stats() never learned about the table.
    """
    import os

    import httpx

    base = os.environ.get("SIPRI_API", "http://sipri:8095").rstrip("/")
    out: dict = {}
    try:
        r = httpx.get(f"{base}/api/summary", timeout=15.0)
        r.raise_for_status()
        out = dict(r.json())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"SIPRI did not answer: {str(e)[:160]}")
    for path, key in (("/api/categories", "categories"), ("/api/datasets", "datasets_list")):
        try:
            rr = httpx.get(f"{base}{path}", timeout=15.0)
            out[key] = rr.json() if rr.status_code < 400 else None
        except Exception:  # noqa: BLE001 — a missing sub-panel must not lose the whole tab
            out[key] = None
    try:
        rr = httpx.get(f"{base}/api/run/status", timeout=10.0)
        out["run"] = rr.json() if rr.status_code < 400 else None
    except Exception:  # noqa: BLE001
        out["run"] = None
    return out


# Counters that describe WORK DONE and therefore add up across the fleet. Everything else is a
# gauge or a description of the machine, and summing those invents numbers: cpu_percent and
# host_mem are host-wide (every instance reports the SAME box, so adding them multiplies one
# reading by N), and pool/host_concurrency describe a configuration, not a quantity.
_FLEET_SUM_TOTALS = ("fetched", "kept", "sent", "accepted", "dropped_by_gate", "skipped_unchanged",
                     "skipped_duplicate", "deflected_render", "deflected_host", "deferred_render",
                     "render_gaveup", "host_busy_gaveup", "paid_recovered")
_FLEET_SUM_LANES = ("render_lane_cap", "render_lane_used", "httpx_reserve", "render_q", "frontier")


def _merge_metrics(rows: list[dict]) -> dict:
    """One fleet-wide /v1/metrics from N instances.

    `instances` is not decoration. If an instance is unreachable its counters are simply absent, so
    a silently-skipped instance makes the fleet total SMALLER — which reads as "the crawl slowed
    down" when the truth is "a container is down". The count is what tells those apart, and it is
    the same rule every other panel here follows about numbers nobody measured.
    """
    up = [r for r in rows if r.get("ok") and isinstance(r.get("data"), dict)]
    if not up:
        err = next((r.get("error") for r in rows if r.get("error")), "no crawler instance answered")
        return {"reachable": False, "error": str(err)[:200],
                "instances": {"up": 0, "down": len(rows), "total": len(rows)}}

    # Instance 0 supplies the machine-level facts verbatim — they describe the host, which every
    # instance shares, so there is nothing to merge and nothing to average.
    out = dict(up[0]["data"])
    totals = {k: 0 for k in _FLEET_SUM_TOTALS}
    lanes = {k: 0 for k in _FLEET_SUM_LANES}
    pages = 0
    rate = 0.0
    running = False
    tabs = browsers = active = render_limit = 0
    cram_used = cram_limit = 0.0
    for r in up:
        d = r["data"]
        p = d.get("progress") or {}
        pages += int(p.get("pages") or 0)
        rate += float(p.get("pages_per_sec") or 0.0)
        running = running or bool(p.get("running"))
        for k in totals:
            totals[k] += int((p.get("totals") or {}).get(k) or 0)
        for k in lanes:
            lanes[k] += int((d.get("lanes") or {}).get(k) or 0)
        tabs += int((d.get("pool") or {}).get("tabs") or 0)
        # BROWSERS must be summed alongside tabs or the tile is arithmetic nonsense: it read
        # "224 — 4 browsers × 8 tabs" because tabs was the fleet sum and browsers was instance 0's.
        browsers += int((d.get("pool") or {}).get("browsers") or 0)
        active += int((d.get("adaptive") or {}).get("active") or 0)
        render_limit += int((d.get("adaptive") or {}).get("render_limit") or 0)
        # Container RAM is per-container, so it sums — unlike host RAM, which every instance
        # reports identically. Showing instance 0's alone under the label "our cap" understated the
        # fleet's real footprint by a factor of N.
        cr = d.get("container_ram") or {}
        cram_used += float(cr.get("used_gb") or 0.0)
        cram_limit += float(cr.get("limit_gb") or 0.0)

    out["progress"] = {**(up[0]["data"].get("progress") or {}),
                       "pages": pages, "pages_per_sec": round(rate, 3),
                       "running": running, "totals": totals}
    out["lanes"] = lanes
    out["pool"] = {**(up[0]["data"].get("pool") or {}), "tabs": tabs, "browsers": browsers}
    out["adaptive"] = {**(up[0]["data"].get("adaptive") or {}),
                       "active": active, "render_limit": render_limit}
    if cram_limit:
        out["container_ram"] = {"used_gb": round(cram_used, 1), "limit_gb": round(cram_limit, 1),
                                "percent": round(100 * cram_used / cram_limit, 1),
                                "scope": f"{len(up)} instance(s) combined"}
    # `running` across the fleet is any(), and total/done add up: a batch is only finished when
    # every instance's share of it is.
    b = {"running": False, "total": 0, "done": 0, "current": "", "stopping": False}
    for r in up:
        d = r["data"].get("batch") or {}
        b["running"] = b["running"] or bool(d.get("running"))
        b["total"] += int(d.get("total") or 0)
        b["done"] += int(d.get("done") or 0)
        b["stopping"] = b["stopping"] or bool(d.get("stopping"))
        b["current"] = b["current"] or str(d.get("current") or "")
    out["batch"] = b
    out["reachable"] = True
    # `crawling` — how many instances have a batch RUNNING, which is a different question from how
    # many answered. An idle instance answers /v1/metrics perfectly, so `up` was 12/12 while SIX
    # instances held no jobs and no frontier and fetched nothing. The dashboard reported a confident
    # green fleet over a half-idle one; this is the number "are all 12 working?" actually asks for.
    crawling = sum(1 for r in up if bool(((r["data"] or {}).get("batch") or {}).get("running")))
    out["instances"] = {"up": len(up), "down": len(rows) - len(up), "total": len(rows),
                        "crawling": crawling, "idle": len(up) - crawling,
                        # So the tile can tell "held back by the concurrency cap" from "out of
                        # work". Without it, a deliberately-throttled fleet is reported as one that
                        # has nothing left to crawl — while 480 sites sit due.
                        "cap": scheduler.MAX_BUSY_INSTANCES or None,
                        "down_detail": [{"instance": r["instance"], "error": r.get("error")}
                                        for r in rows if not r.get("ok")]}
    return out


@router.get("/crawler/metrics")
def crawler_metrics() -> dict:
    return _merge_metrics(crawler_client.fanout(crawler_client.get_metrics))


# ── Ground truth: what is ACTUALLY in the store ───────────────────────────────
# The orchestrator's own DB holds the control plane (catalog, schedule, run history) and knows
# nothing about documents landed by direct crawler-api batches — which is why its job/log panels
# diverged from reality. These proxy the ingest service, which owns Postgres `documents` and the
# MinIO bucket, so the dashboard shows the store instead of the orchestrator's opinion of it.
# The self-check's write-path canary is a real document in the real store — that is the only way to
# prove an accepted write actually lands. But it is not a SOURCE, and it was being listed as one:
# 39 pages under `selfcheck-canary.invalid`, shown to the operator on the corpus panel next to two
# genuine uncatalogued hosts. Filtered here, at the one call every sources listing goes through,
# rather than at five call sites that would drift apart.
_CANARY_SOURCE = "selfcheck-canary.invalid"


def _ingest_get(path: str, timeout: float = 20.0):
    import httpx
    base = get_settings().ingest_api.rstrip("/")
    r = httpx.get(f"{base}{path}", timeout=timeout)
    r.raise_for_status()
    out = r.json()
    # ...but NOT when the caller explicitly asked for it. check_write_path reads its canary back
    # through this very function, so an unconditional filter would make `landed` permanently False
    # and the write-path check would cry "ingest ACCEPTED a document and it never reached the store"
    # every 15 minutes — a false alarm about silent success, raised by the silent-success detector.
    # Its unit tests could not catch it either: they monkeypatch _ingest_get, so the filter is not
    # in the path under test. Exactly the shape this module exists to find.
    if (path.startswith("/api/live/sources") and isinstance(out, list)
            and _CANARY_SOURCE not in path.lower()):
        return [x for x in out
                if str((x or {}).get("source_id", "")).lower() != _CANARY_SOURCE]
    return out


def _ingest_post(path: str, body: dict, timeout: float = 20.0):
    """Only the self-check writes through here — it needs the WRITE path, not a read.

    A read-only probe cannot see the fault it exists for: the ingest API answered 200 `accepted`
    while the row never reached Postgres, and every read endpoint stayed perfectly healthy because
    the rest of the fleet was still writing.
    """
    import httpx
    base = get_settings().ingest_api.rstrip("/")
    r = httpx.post(f"{base}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── corpus rollups: single-flight + short TTL ─────────────────────────────────
# The whole-corpus aggregates (summary, types, categories, per-source) each scan `documents_current`
# and cost seconds, while the dashboard's fast timer is 3s. That is not a slow query, it is a
# STAMPEDE: measured on the live stack, FIVE copies of the same 20s summary query were running
# concurrently, staggered six seconds apart, so `documents` was never once free of a reader. It
# pinned Postgres, it made every tile read "timed out" (the query takes 20.4s against a 20.0s client
# timeout), and it blocked schema changes outright — an ALTER TABLE could not find a lock window in
# thirty attempts.
#
# SINGLE-FLIGHT IS THE POINT, not the TTL. A plain expiry cache still lets N simultaneous misses all
# reach the database; the lock is what collapses them into one query whose answer everyone shares.
# Failures are cached too, briefly: without that, a slow-failing ingest turns a stampede into a
# QUEUE, each waiter serially paying the full timeout.
_ROLLUP_TTL_S = 30.0
_ROLLUP_FAIL_TTL_S = 5.0
# A query slower than its own TTL is expired the moment it lands, so the next poll starts it again
# and the database never gets a break: /api/data/types measures 39s against a 30s TTL, which is a
# permanently-running full-corpus scan pretending to be a cache. Holding a slow answer for twice the
# time it cost to produce caps ANY rollup at a 50% duty cycle, whatever its cost, without needing a
# hand-tuned TTL per endpoint. `as_of_s` still tells the caller exactly how old the number is.
_ROLLUP_SLOW_FACTOR = 2.0
# key -> (stored_at, payload, ttl_for_this_entry)
_rollup_cache: dict[str, tuple[float, dict, float]] = {}
_rollup_locks: dict[str, Lock] = {}
_rollup_guard = Lock()


def _rollup(key: str, fn, ttl: float | None = None) -> dict:
    """Call `fn` at most once per TTL per key, no matter how many callers arrive at once.

    `fn` must return the FINAL response dict and raise nothing — error shapes are cached like any
    other answer, which is what keeps a broken ingest from serialising every poller behind it.

    `ttl` overrides the 30s default for things that must stay live — batch status drives "is a crawl
    running", so it is cached only long enough to collapse simultaneous pollers, not long enough to
    go stale.
    """
    base_ttl = _ROLLUP_TTL_S if ttl is None else ttl

    def fresh() -> dict | None:
        hit = _rollup_cache.get(key)
        if not hit:
            return None
        age = time.monotonic() - hit[0]
        if age >= hit[2]:
            return None
        # Age is reported, not hidden. A number up to TTL seconds old is fine on a corpus growing at
        # ~17 pages/s, but the dashboard's one rule is that it never implies precision it lacks.
        #
        # TWO decimals, not one: a freshly-computed answer returns as_of_s=0.0, so rounding to 0.1s
        # made every cache hit younger than 50ms report the value that means "live". Same shape as
        # the not-measured rule this codebase runs on — a number nobody re-derived must not be
        # indistinguishable from one that was.
        return dict(hit[1], as_of_s=round(age, 2))

    got = fresh()
    if got is not None:
        return got
    with _rollup_guard:
        lock = _rollup_locks.setdefault(key, Lock())
    with lock:
        got = fresh()            # someone may have filled it while we queued on the lock
        if got is not None:
            return got
        t0 = time.monotonic()
        val = fn()
        took = time.monotonic() - t0
        entry_ttl = (_ROLLUP_FAIL_TTL_S if val.get("reachable") is False
                     else max(base_ttl, took * _ROLLUP_SLOW_FACTOR))
        _rollup_cache[key] = (time.monotonic(), val, entry_ttl)
        return dict(val, as_of_s=0.0)


# ── Push to the data centre ──────────────────────────────────────────────────
# Proxied, not reimplemented: ingest owns the documents and the pushed_at watermark. The
# orchestrator only carries the button.

@router.get("/push/status")
def push_status() -> dict:
    try:
        d = _ingest_get("/api/live/push/status")
        d["reachable_ingest"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"reachable_ingest": False, "error": str(e)[:200]}


@router.get("/centre/check")
def centre_check() -> dict:
    """Per-leg data-centre connection test — the button you press on a machine you just set up.

    Longer timeout than /push/status because every leg is allowed to fail slowly: a dead tunnel
    blackholes a TCP handshake, and the whole point is to report that rather than inherit the
    caller's timeout and render as "the dashboard is broken".
    """
    try:
        d = _ingest_get("/api/live/centre/check", timeout=90)
        d["reachable_ingest"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"reachable_ingest": False, "error": str(e)[:200]}


@router.get("/push/verify")
def push_verify() -> dict:
    """Compare this machine's store against the data centre BEFORE anything is sent.

    Read-only on both sides. Proxied for the same reason as the push itself: ingest owns both
    connections and the pushed_at watermark, so the orchestrator carries the button, not the logic.
    Given a longer timeout than the other proxies — it counts rows on the far side of an SSH tunnel
    and checks blob existence, which is slower than a status ping.
    """
    import httpx
    base = get_settings().ingest_api.rstrip("/")
    try:
        r = httpx.get(f"{base}/api/live/verify", timeout=180)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reachable_ingest": False, "verdict": str(e)[:200]}
    if r.status_code >= 400:
        try:
            return {"ok": False, "verdict": r.json().get("detail", r.text[:200])}
        except Exception:  # noqa: BLE001
            return {"ok": False, "verdict": r.text[:200]}
    return {"reachable_ingest": True, **r.json()}


@router.post("/push/run")
def push_run(limit: int = 200) -> dict:
    """One slice per press. Long enough to make progress, short enough to answer while you watch."""
    import httpx
    base = get_settings().ingest_api.rstrip("/")
    try:
        r = httpx.post(f"{base}/api/live/push?limit={limit}", timeout=180)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    if r.status_code >= 400:
        # Surface the ingest service's own reason (no DC url, no DB) instead of a bare 500 —
        # "refusing to push into the void" is something the operator can act on.
        try:
            return {"ok": False, "error": r.json().get("detail", r.text[:200])}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": r.text[:200]}
    return {"ok": True, **r.json()}


@router.get("/data/summary")
def data_summary() -> dict:
    """Real document/blob counts. `reachable:false` is reported rather than zeros — a dashboard
    showing 0 documents because a service is down looks identical to an empty store otherwise."""
    return _rollup("summary", _data_summary_live)


def _data_summary_live() -> dict:
    try:
        # 60s, not the 20s default. This aggregate measured 20.7s against a 20.0s timeout — so it
        # failed by 0.7s and the tiles rendered "timed out" while Postgres was perfectly healthy and
        # had in fact computed the answer. A timeout tighter than the work it waits on does not
        # protect anything, it just discards results that were already paid for. The real fix is to
        # stop the query detoasting the corpus (documents.text_len); this is the margin that keeps
        # the panel honest until then, and it is safe to be generous because _rollup single-flights
        # it — at most ONE request is ever waiting.
        d = _ingest_get("/api/live/summary", timeout=60.0)
        # reachable means "the numbers below are real", not merely "ingest answered the phone".
        # ingest stays up when its Postgres does not, and it now says so instead of sending zeros;
        # forwarding that as reachable:true is how a dead store rendered as an empty corpus.
        err = (d.get("postgres") or {}).get("error")
        d["reachable"] = not err
        if err:
            d["error"] = f"ingest is up but its store is not readable: {err}"
        return d
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


@router.get("/data/doc/{document_id}/text")
def data_doc_text(document_id: str) -> dict:
    """Extracted text of one stored page. Proxied — ingest owns the documents."""
    try:
        return _ingest_get(f"/api/live/doc/{quote(document_id)}/text", timeout=60.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@router.get("/data/doc/{document_id}/files")
def data_doc_files(document_id: str) -> dict:
    """PDFs / images stored for one page."""
    try:
        return _ingest_get(f"/api/live/doc/{quote(document_id)}/files", timeout=60.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "files": []}


@router.get("/data/doc/{document_id}/html")
def data_doc_html(document_id: str):
    """The SOURCE html of one page, served as text/plain.

    text/plain deliberately, matching ingest: this is a third-party page and rendering it as HTML
    inside the console's own origin would execute someone else's scripts with the operator's
    session. The point is to READ the markup, not to run it.
    """
    import httpx
    from fastapi.responses import Response
    base = get_settings().ingest_api.rstrip("/")
    try:
        r = httpx.get(f"{base}/raw-html", params={"doc_id": document_id}, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"ingest unreachable: {str(e)[:120]}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "not found")
    return Response(content=r.content, media_type="text/plain; charset=utf-8")


@router.get("/data/artifact")
def data_artifact(path: str):
    """Stream one stored file (PDF / image) from MinIO via ingest.

    The s3:// prefix check is ingest's job and it does it; this only refuses an obviously empty
    path so a mistyped link does not become a 500 on the console.
    """
    import httpx
    from fastapi.responses import StreamingResponse
    if not (path or "").strip():
        raise HTTPException(400, "path is required")
    base = get_settings().ingest_api.rstrip("/")

    def _pump():
        with httpx.Client(timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0),
                          follow_redirects=True) as c:
            with c.stream("GET", f"{base}/artifact", params={"path": path}) as r:
                if r.status_code >= 400:
                    raise HTTPException(r.status_code, "artifact not found")
                yield from r.iter_bytes(chunk_size=1 << 18)

    return StreamingResponse(_pump(), media_type="application/octet-stream",
                             headers={"Content-Disposition": "inline"})


@router.get("/data/site/{domain}/depth")
def data_site_depth(domain: str, sample: int = 12) -> dict:
    """Pages per crawl depth for one site. PROXIED — ingest owns the documents.

    Depth is the thing a page COUNT cannot tell you. A site holding 14k pages entirely at d1 has
    been walked, not crawled; one with a real d2/d3 tail has actual content. Both look identical in
    the "Pages" column this sits behind.
    """
    dom = (domain or "").strip().lower().removeprefix("www.")
    if not dom:
        raise HTTPException(400, "domain is required")
    try:
        return _ingest_get(f"/api/live/source/{quote(dom)}/depth?sample={int(sample)}", timeout=90.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "depths": [], "source_id": dom}


@router.get("/data/site/{domain}/download")
def data_site_download(domain: str):
    """Stream this site's zip (html + text + PDFs + a documents.sql dump) from ingest.

    STREAMED, not buffered. safran-group.com alone is 6.4 GB of HTML, and reading that into the
    orchestrator's memory to hand it on would be a way to OOM the console every operator uses.
    httpx.stream + an iterator keeps it at one chunk in flight, and the long read timeout is because
    the zip is BUILT on demand — the first bytes do not arrive until Postgres has been walked.
    """
    import httpx
    from fastapi.responses import StreamingResponse

    dom = (domain or "").strip().lower().removeprefix("www.")
    if not dom:
        raise HTTPException(400, "domain is required")
    base = get_settings().ingest_api.rstrip("/")
    url = f"{base}/download?host={quote(dom)}"

    def _pump():
        # connect quickly, then wait a long time: building the archive is the slow part.
        to = httpx.Timeout(connect=15.0, read=1800.0, write=60.0, pool=15.0)
        with httpx.Client(timeout=to, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise HTTPException(502, f"ingest returned HTTP {r.status_code} for {dom}")
                yield from r.iter_bytes(chunk_size=1 << 20)

    return StreamingResponse(_pump(), media_type="application/zip",
                             headers={"Content-Disposition":
                                      f'attachment; filename="mallory_{dom}.zip"'})


@router.get("/data/sources")
def data_sources(limit: int = 300) -> dict:
    # Keyed by limit: the dashboard asks for 800 and the category rollup for 5000, and serving one
    # from the other's cache would silently truncate a panel.
    return _rollup(f"sources:{limit}", lambda: _data_sources_live(limit))


def _data_sources_live(limit: int) -> dict:
    try:
        return {"reachable": True, "rows": _ingest_get(f"/api/live/sources?limit={limit}")}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200], "rows": []}


@router.get("/data/recent")
def data_recent(limit: int = 50, source: str = "", category: str = "", dtype: str = "",
                served_by: str = "", since: str = "", until: str = "",
                db: Session = Depends(get_db)) -> dict:
    """Recently stored documents, filtered by source / CATEGORY / data type / fetch tier.

    Category is resolved HERE, not in the store: `documents` has no category column, so the catalog
    turns the category into its list of domains and the store filters on those. Doing it the other
    way round — fetching a page and dropping non-matching rows — would silently under-report,
    because LIMIT applies before a client-side filter.
    """
    from urllib.parse import urlencode
    params: dict[str, str | int] = {"limit": limit}
    if source:
        params["source"] = source
    if dtype:
        params["dtype"] = dtype
    if served_by:
        params["served_by"] = served_by
    # The window that turns "this re-crawl found 4 new pages" into "…and here they are". Without it
    # the delta column states a number the operator cannot inspect, which is the same complaint the
    # cause chips answered on the site drill-down.
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if category:
        # The filter arrives as whatever the By-category panel put in the dropdown, and that is now
        # a BUCKET LABEL ("News"), not a raw taxonomy value ("trade_press"). Match either, so the
        # dropdown works and a hand-written ?category=trade_press keeps working too — a filter that
        # silently returns nothing is indistinguishable from a store that holds nothing.
        doms = [s.domain for s in db.scalars(select(Source)).all()
                if s.category == category or ui_category(s.category) == category]
        if not doms:
            # An empty domain list must NOT become an unfiltered query — that would answer a
            # question the operator did not ask with the entire store.
            return {"reachable": True, "rows": [], "filtered_by": {"category": category},
                    "note": f"no catalogued source has category '{category}'"}
        params["sources"] = ",".join(doms)
    try:
        return {"reachable": True, "rows": _ingest_get("/api/live/recent?" + urlencode(params)),
                "filtered_by": {k: v for k, v in params.items() if k != "limit"}}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200], "rows": []}


@router.get("/data/search")
def data_search(q: str = "", limit: int = 100, source: str = "", category: str = "",
                dtype: str = "", since: str = "", until: str = "",
                db: Session = Depends(get_db)) -> dict:
    """Search the corpus by title/url (always) and page body (once the tsvector index is built).

    Category is resolved to its domains HERE, same as /data/recent (`documents` has no category
    column). A blank query returns nothing — never the whole store.
    """
    from urllib.parse import urlencode
    if not (q or "").strip():
        return {"reachable": True, "rows": [], "body_ready": False}
    params: dict[str, str | int] = {"q": q, "limit": limit}
    if source:
        params["source"] = source
    if dtype:
        params["dtype"] = dtype
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if category:
        doms = [s.domain for s in db.scalars(select(Source)).all()
                if s.category == category or ui_category(s.category) == category]
        if not doms:
            return {"reachable": True, "rows": [], "body_ready": False,
                    "filtered_by": {"category": category},
                    "note": f"no catalogued source has category '{category}'"}
        params["sources"] = ",".join(doms)
    try:
        r = _ingest_get("/api/live/search?" + urlencode(params)) or {}
        return {"reachable": True, "rows": r.get("rows", []),
                "body_ready": bool(r.get("body_ready")),
                "filtered_by": {k: v for k, v in params.items() if k not in ("limit", "q")}}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200], "rows": [], "body_ready": False}


@router.post("/crawler/stop")
def crawler_stop(db: Session = Depends(get_db)) -> dict:
    """Halt the crawl on EVERY instance.

    Each instance keeps its own stop flag in its own data directory, so a stop sent to one leaves
    the others crawling — measured on the two-instance fleet: instance 1 halted, instance 2 kept
    going at full rate, and this dashboard (reading instance 1) reported "not running" over a live
    crawl. So the stop is a broadcast, and it only claims success when every instance acknowledges.
    A partial stop is reported as a failure, because believing the crawl is stopped when it is not
    is the worst outcome available here.
    """
    # Recorded BEFORE the broadcast, and durably. The crawler's own stop flag is consumed by the
    # engine as it winds down, so it says nothing once teardown finishes — at which point the
    # auto-refill watcher saw plain idleness and resumed the crawl from its saved frontier, roughly
    # a minute after the operator stopped it. This is THE stop (policy.py): it outlives the
    # teardown and only an explicit Start lifts it — no dispatch route clears it any more.
    policy.stop(db, reason="Stop pressed (crawler/stop) — every lane closed until Start")
    rows = crawler_client.fanout(crawler_client.stop_crawl)
    bad = [r for r in rows if not r.get("ok")]
    if bad and len(bad) == len(rows):
        raise HTTPException(502, f"no crawler instance answered: {bad[0].get('error')}")
    out = {"stopping": any((r.get("data") or {}).get("stopping") for r in rows if r.get("ok")),
           "halted": True, "run": "stopped",
           "instances": {"acked": len(rows) - len(bad), "total": len(rows)},
           "message": "stop signaled to every instance" if not bad else
                      f"STOP INCOMPLETE — {len(bad)} of {len(rows)} instance(s) did not acknowledge",
           "failed": [{"instance": r["instance"], "error": r.get("error")} for r in bad]}
    return out


@router.get("/crawler/resumable")
def crawler_resumable() -> dict:
    """Crawls that were cut short and still hold a saved frontier, across the whole fleet.

    Each job is tagged with the instance that holds its frontier, because that is the only instance
    that can resume it — the pending URLs and already-seen set live in that container's sqlite.
    """
    rows = crawler_client.fanout(crawler_client.resumable)
    up = [r for r in rows if r.get("ok")]
    if not up:
        # Not an empty list: "crawler is down" and "nothing to resume" must not look identical.
        return {"reachable": False, "error": str(rows[0].get("error"))[:200] if rows else "no instances",
                "count": None, "jobs": []}
    jobs = []
    for r in up:
        for j in ((r["data"] or {}).get("jobs") or []):
            jobs.append({**j, "instance": r["instance"]})
    return {"reachable": True, "count": len(jobs), "jobs": jobs,
            "instances": {"up": len(up), "down": len(rows) - len(up), "total": len(rows)}}


@router.post("/crawler/resume")
def crawler_resume(body: dict, db: Session = Depends(get_db)) -> dict:
    """Resume by job_id, routing each id back to the instance that actually holds its frontier.

    Sending a job to the wrong instance does not fail loudly — that instance has no saved frontier
    for the id, so it reports `missing_config` and the crawl silently does not restart.
    """
    # THE ONE GATE. A refused resume changes nothing — this route used to CLEAR the operator's
    # stop as its first line, which is how any placer script re-armed the whole fleet. Stopped
    # means stopped; the operator presses Start, then resumes.
    if not scheduler.lane_open(db, "resume"):
        raise HTTPException(409, "the 'resume' lane is closed: %s"
                            % scheduler.why_closed(db, "resume"))
    ids = [str(x) for x in (body.get("job_ids") or []) if str(x).strip()]
    if not ids:
        raise HTTPException(400, "job_ids is required")
    # THE MODE OWNS ITS FRONTIERS. Resuming a campaign's half-walked sites from normal mode is
    # exactly the revived backlog the mode switch exists to prevent, whoever asks. Refused whole,
    # naming the ids, rather than silently resuming the subset that fits.
    other = sorted(set(ids) - set(scheduler.owned_by_mode(db, ids)))
    if other:
        raise HTTPException(409, "%d job(s) belong to the other mode and stay parked in %s mode: %s"
                            % (len(other), policy.mode(db), ", ".join(other[:10])))
    bg, fwd = bool(body.get("background", True)), bool(body.get("forward_to_ingest", True))
    # Ask each instance what it holds, then send every id only to its owner.
    owner: dict[str, int] = {}
    for r in crawler_client.fanout(crawler_client.resumable):
        if r.get("ok"):
            for j in ((r["data"] or {}).get("jobs") or []):
                owner.setdefault(str(j.get("job_id")), r["instance"])
    bases = crawler_client.pool()
    by_inst: dict[int, list[str]] = {}
    unknown = [i for i in ids if i not in owner]
    for i in ids:
        if i in owner:
            by_inst.setdefault(owner[i], []).append(i)
    resumed, errors = [], []
    for inst, batch in by_inst.items():
        try:
            d = crawler_client.resume(batch, background=bg, forward_to_ingest=fwd,
                                      base=bases[inst])
            # An instance that REFUSED the resume must not be counted as resumed. The crawler answers
            # 409 when its previous batch still owns the engine (a wedged teardown), and that instance
            # then never clears its .crawl_stop file — so it sits idle holding real work while this
            # endpoint reports success. Counting `d["resumed"]` unconditionally is what hid that for
            # four separate cycles; the ids come back listed even when nothing started.
            # TWO shapes, because the fleet can be mid-rollout. A patched crawler answers 409 (which
            # httpx raises, landing in the except below) or returns top-level `started: False`; an
            # UNPATCHED one returns 200 with the refusal buried in `batch.started`. Reading only the
            # top level would keep missing the bug on every instance not yet rebuilt.
            nested = (d.get("batch") or {}) if isinstance(d.get("batch"), dict) else {}
            if d.get("started") is False or nested.get("started") is False:
                errors.append({"instance": inst, "jobs": len(batch),
                               "error": d.get("error") or nested.get("error")
                               or "instance refused the resume"})
                continue
            resumed.extend(d.get("resumed") or [])
        except Exception as e:  # noqa: BLE001 — one bad instance must not lose the rest
            errors.append({"instance": inst, "jobs": len(batch), "error": str(e)[:200]})
    if not resumed and errors:
        raise HTTPException(502, f"resume failed on every instance: {errors[0]['error']}")
    return {"resumed": resumed, "missing_config": unknown, "errors": errors,
            # Which instances actually took work, vs which were asked. A caller that only reads
            # `resumed` cannot tell that an instance was skipped.
            "instances": sorted(i for i in by_inst if i not in {e["instance"] for e in errors}),
            "refused": [e["instance"] for e in errors]}


@router.post("/crawler/resumable/clear")
def crawler_resumable_clear(body: dict | None = None) -> dict:
    """Forget saved frontiers. `job_ids` omitted/empty = all of them (the Delete all button).

    Broadcast: a "Delete all" that cleared one instance and left the others would leave the Resume
    tab still listing jobs the operator believes they just removed.
    """
    ids = [str(x) for x in ((body or {}).get("job_ids") or []) if str(x).strip()]
    rows = crawler_client.fanout(crawler_client.clear_resumable, ids or None)
    bad = [r for r in rows if not r.get("ok")]
    if bad and len(bad) == len(rows):
        raise HTTPException(502, f"no crawler instance answered: {bad[0].get('error')}")
    cleared = sum(int((r.get("data") or {}).get("cleared") or 0) for r in rows if r.get("ok"))
    return {"cleared": cleared, "instances": {"acked": len(rows) - len(bad), "total": len(rows)},
            "failed": [{"instance": r["instance"], "error": r.get("error")} for r in bad]}


@router.get("/crawler/camofox")
def crawler_camofox() -> dict:
    """C3 CamoFox status, plus what the browser is ACTUALLY doing.

    The dashboard's "browser pool 96 · 12 browsers × 8 tabs" is the crawler's CONFIGURED intent
    (CRAWLER_BROWSERS × CRAWLER_TABS_PER_BROWSER), not a measurement — there is one camoufox
    process, and while C1 is serving the crawl it holds zero tabs. Presenting a config constant
    where a live number belongs is the same failure as any other unmeasured figure on this page.

    So this asks CamoFox directly. It answers `/` with browserRunning/browserConnected and `/tabs`
    with the live list. A browser that is up but holding nothing is the normal, healthy state for a
    C1-served crawl: only C3 opens tabs.
    """
    try:
        d = crawler_client.get_camofox()
        d["reachable"] = True
    except Exception:  # noqa: BLE001
        return {"reachable": False, "enabled": False, "healthy": False}

    import os

    import httpx

    base = (d.get("url") or os.environ.get("CAMOFOX_URL", "http://camofox:9377")).rstrip("/")
    try:
        r = httpx.get(f"{base}/tabs", timeout=6.0)
        if r.status_code < 400:
            body = r.json()
            tabs = body.get("tabs")
            d["tabs_open"] = len(tabs) if isinstance(tabs, list) else None
            d["browser_running"] = bool(body.get("running"))
    except Exception:  # noqa: BLE001 — the dot must survive a slow browser
        d["tabs_open"] = None
    try:
        r = httpx.get(base + "/", timeout=6.0)
        if r.status_code < 400:
            b = r.json()
            d["browser_connected"] = bool(b.get("browserConnected"))
            d["browsers_live"] = 1 if b.get("browserRunning") else 0
    except Exception:  # noqa: BLE001
        pass
    return d


def _crawled_domains(db: Session) -> set[str]:
    """Every domain we have actually crawled, from ALL the signals we have — not just this service's
    own dispatch bookkeeping.

    Source.last_crawled is only written when a dispatched job carries a source_id that resolves to a
    catalog row, so a site crawled by a direct batch (or by an older path) stayed last_crawled=None
    forever and kept reappearing under "only never-crawled sites" — measured: 240/240 sources read as
    never-crawled while the store held hundreds of pages for several of them. The ingest store is the
    ground truth for "we have pages for this host", so it decides; local bookkeeping only adds to it.
    """
    out = {s.domain for s in db.scalars(select(Source)).all() if s.last_crawled is not None}
    out |= {str(d).lower() for d in db.scalars(select(SiteMeasurement.domain).distinct()).all() if d}
    try:
        for r in _ingest_get("/api/live/sources?limit=5000"):
            h = str(r.get("source_id") or "").lower().removeprefix("www.")
            if h and int(r.get("pages") or 0) > 0:
                out.add(h)
    except Exception:  # noqa: BLE001 — ingest down: fall back to local signals, don't hide everything
        pass
    return out


@router.post("/crawler/batch")
def crawler_batch(body: dict, db: Session = Depends(get_db)) -> dict:
    """Manual batch. `parallel` = how many sites crawl AT A TIME (0/absent = all at once).

    Runs on a background thread that dispatches the list chunk by chunk and records every site the
    moment its chunk closes (JobRun + SiteMeasurement + cycle log). The old passthrough fired the
    whole list with background:true and recorded nothing, so the Logs tab stayed empty for the whole
    crawl and the recrawl delta never moved. Poll /api/crawler/batch/status for progress.
    """
    # THE ONE GATE. A refused batch changes nothing — this route used to CLEAR the operator's stop
    # on its way in. A pasted batch is campaign work: it needs campaign mode AND a running pipeline.
    if not scheduler.lane_open(db, "manual"):
        return {"started": False, "error": "the 'manual' lane is closed: %s"
                                           % scheduler.why_closed(db, "manual")}

    if not isinstance(body, dict) or not body.get("jobs"):
        raise HTTPException(400, "body must be {\"jobs\": [ ... ]}")
    jobs = list(body.get("jobs") or [])
    try:
        parallel = int(body.get("parallel") or 0)
    except (TypeError, ValueError):
        parallel = 0
    if orchestrate.manual_status().get("running"):
        return {"started": False, "error": "a manual batch is already running"}
    # Off means off, including for work started by hand. Without this the master switch gated only
    # the scheduler, so "Run all now" happily crawled a pipeline the operator had just stopped.
    if not scheduler.master_enabled(db):
        return {"started": False,
                "error": "the master switch is off — turn it on before starting a crawl"}
    Thread(target=orchestrate.run_manual_batch, args=(jobs, parallel), daemon=True).start()
    return {"started": True, "total": len(jobs), "parallel": parallel,
            "mode": "all at once" if parallel <= 0 else f"{parallel} site(s) at a time"}


@router.get("/crawler/fleet/health")
def crawler_fleet_health() -> dict:
    """Is the fleet's work actually DISTRIBUTED, or is one instance holding it all?

    The fault this answers is silent and expensive: a domain's frontier lives inside one container's
    sqlite, so when an instance finishes the domains it owns it simply stops, while another sits on a
    six-figure backlog it has no way to share. Measured repeatedly — six of twelve idle at 4.13
    pages/s with 111,131 pending URLs on instance 0 — and every other number on the dashboard looked
    healthy the whole time, which is why this needs its own verdict rather than a tile someone has to
    interpret.

    `verdict` is deliberately coarse: ok / idle_instances / unbalanced / not_crawling. The remedy for
    the first two is the same (stop, move_frontier, resume) and is NOT run from here — moving a
    frontier out from under a live engine races it, so this reports and the operator decides.
    """
    rows = crawler_client.fanout(crawler_client.get_metrics)
    up = [r for r in rows if r.get("ok") and isinstance(r.get("data"), dict)]
    if not up:
        return {"reachable": False, "verdict": "no_instances",
                "detail": "no crawler instance answered"}

    per = []
    for r in up:
        d = r["data"]
        lanes = d.get("lanes") or {}
        batch = d.get("batch") or {}
        pending = int(lanes.get("frontier") or 0) + int(lanes.get("render_q") or 0)
        per.append({"instance": r["instance"], "crawling": bool(batch.get("running")),
                    "pending": pending, "jobs": int(batch.get("total") or 0)})

    crawling = [p for p in per if p["crawling"]]
    idle = [p for p in per if not p["crawling"]]
    total_pending = sum(p["pending"] for p in per)
    # Idle instances holding NOTHING are the movable case; idle while holding a frontier is the
    # stale-stop-flag case, which has a different fix (resume that instance directly).
    idle_empty = [p for p in idle if p["pending"] == 0]
    idle_with_work = [p for p in idle if p["pending"] > 0]

    busiest = max(per, key=lambda p: p["pending"]) if per else None
    # A fleet is unbalanced when one instance holds a disproportionate share while others are idle.
    share = (busiest["pending"] / total_pending) if (busiest and total_pending) else 0.0

    if not crawling:
        verdict, detail = "not_crawling", "no instance is running a batch"
    elif idle_with_work:
        verdict = "idle_with_work"
        detail = (f"{len(idle_with_work)} instance(s) hold queued URLs but are not crawling — "
                  f"resume them directly (a stale stop flag does this)")
    elif idle_empty and total_pending:
        verdict = "idle_instances"
        detail = (f"{len(idle_empty)} instance(s) have no work while {total_pending:,} URL(s) are "
                  f"queued elsewhere — work does not move between instances on its own")
    elif share > 0.6 and len(per) > 2:
        verdict = "unbalanced"
        detail = (f"instance {busiest['instance']} holds {share:.0%} of the queued work")
    else:
        verdict, detail = "ok", "every instance is crawling and the work is spread"

    return {"reachable": True, "verdict": verdict, "detail": detail,
            "instances": {"total": len(rows), "up": len(up),
                          "crawling": len(crawling), "idle": len(idle)},
            # Reported so the dashboard can tell "held back by the cap" from "out of work". Without
            # it the fleet tile accuses the crawlers of having nothing to do while 480 sites are due.
            "concurrency_cap": scheduler.MAX_BUSY_INSTANCES or None,
            "pending_total": total_pending,
            "busiest": busiest, "idle_empty": [p["instance"] for p in idle_empty],
            "idle_with_work": [p["instance"] for p in idle_with_work],
            "per_instance": sorted(per, key=lambda p: -p["pending"]),
            # The fix is a deliberate operator action, not something to fire on a timer.
            "remedy": ("stop the crawl, run scripts/move_frontier.py --apply, apply the "
                       "domain_shards updates, then resume")
            if verdict in ("idle_instances", "unbalanced") else None}


@router.get("/autopilot")
def autopilot_status() -> dict:
    """What the health autopilot has been doing, so it is visible rather than merely running.

    An automation nobody can see is indistinguishable from one that died three days ago — and this
    one exists precisely because the faults it fixes are silent. The history is what lets an
    operator answer "was the fleet healthy overnight" without reading logs.
    """
    from . import autopilot
    return autopilot.status()


@router.post("/autopilot/run")
def autopilot_run_now() -> dict:
    """Run a pass now. Same code path as the timer, so the button cannot drift from the automation."""
    from . import autopilot
    return {**autopilot.run_once(), "status": autopilot.status()}


@router.post("/crawler/fleet/assign")
def crawler_fleet_assign(db: Session = Depends(get_db)) -> dict:
    """ONE button: check every instance, and give work to the ones sitting idle.

    Until now the three pieces of this existed but nothing on the dashboard reached them, so the
    answer to "instance 4 is idle" was a shell session. They also have to run in this order, because
    an idle instance is idle for one of two unrelated reasons and the cheap fix must go first:

      1. idle but HOLDING a frontier  -> it just needs resuming. A stale `stop_pending` from a
         teardown that finished long ago is the usual cause, and refill skips those on the timer to
         protect a deliberate stop -- pressing this button IS the decision to override it, so it is
         called with force. The durable operator halt is still obeyed and still wins.
      2. idle holding NOTHING         -> it has finished everything it owns. Resuming does nothing;
         it needs domains, which is a rebalance.

    Doing 2 before 1 wastes a rebalance on a problem a single resume fixes. Doing only 2 leaves
    instances idle on top of a full frontier, which is the state that was measured at 4.13 pages/s.

    Reports before/after from the same health check rather than claiming success, so an instance
    that stayed idle is visible instead of being averaged away.
    """
    from . import refill

    before = crawler_fleet_health()
    if not before.get("reachable"):
        return {"ok": False, "before": before, "detail": before.get("detail"),
                "summary": "no crawler instance answered — nothing to assign"}

    res = refill.refill_once(force=True)
    if res.get("halted"):
        # Not a failure and not something to route around: the operator stopped the fleet on
        # purpose and this button must not undo that quietly.
        return {"ok": False, "halted": True, "before": before, "refill": res,
                "summary": "the crawl is halted by the master switch (the pipeline is stopped) — "
                           "press Start first"}

    # Only when someone is idle with an empty frontier is there anything a rebalance can fix, and it
    # refuses outright if an instance is unreachable, so a failure here is reported, not raised.
    reb = None
    if before.get("idle_empty"):
        try:
            reb = crawler_fleet_rebalance(db)
        except HTTPException as e:
            reb = {"error": str(e.detail)}
        except Exception as e:  # noqa: BLE001
            reb = {"error": str(e)[:200]}

    # 3. STILL idle? Then it owns no unfinished domain and resuming cannot help — but it may well
    #    own catalogued sites the fleet has never crawled at all. Measured here: 213 of 807 sites
    #    had no pages, 29 of them owned by the two idle instances, whose next scheduled tick was six
    #    days out. Leaving them parked for six days is not "assign work", so dispatch them.
    #
    #    NEVER-CRAWLED ONLY, deliberately. Re-dispatching a finished domain to fill an idle slot
    #    would re-walk a site that is already done — busywork that costs real fetches and buries the
    #    cadence signal.
    #
    #    "Never crawled" is asked of the CORPUS, and neither of the two local shortcuts works:
    #    `sources.last_crawled` is null on 651 rows that plainly have pages, and `job_runs` holds
    #    only 160 domains against 594 with pages (it records dispatches, not resumes), so diehl.com
    #    and its 7,725 pages both read as never crawled. Whether a site has pages is a fact only the
    #    store holds. If the store cannot be reached this step is SKIPPED rather than guessed —
    #    guessing wrong here means re-walking finished sites.
    after = crawler_fleet_health()
    dispatched: dict | None = None
    idle_now = [p["instance"] for p in (after.get("per_instance") or [])
                if not p["crawling"] and not p["pending"]]
    if idle_now:
        if orchestrate.manual_status().get("running"):
            dispatched = {"skipped": "a manual batch is already running"}
        elif not scheduler.master_enabled(db):
            dispatched = {"skipped": "the master switch is off — the pipeline is stopped"}
        elif not scheduler.lane_open(db, "scheduled"):
            # First-crawls of never-crawled CATALOGUE sites are the system's own work, not a
            # campaign's: normal mode only.
            dispatched = {"skipped": "the 'scheduled' lane is closed: %s"
                                     % scheduler.why_closed(db, "scheduled")}
        else:
            from . import shard
            try:
                rows = _ingest_get("/api/live/sources?limit=100000", timeout=120.0) or []
                have = {str(r.get("source_id") or "").lower().lstrip("www.")
                        for r in rows if (r.get("pages") or 0) > 0}
            except Exception as e:  # noqa: BLE001
                have = None
                dispatched = {"skipped": f"the store did not answer, so 'never crawled' could not "
                                         f"be established: {str(e)[:120]}"}
            n_pool = len(crawler_client.pool())
            # BOTH signals, and each does a different job. The corpus answers "does this site have
            # pages"; `job_runs` answers "have we already tried". A site with no pages AND no
            # attempt is fresh work. A site with no pages but an attempt behind it is one we have
            # already been to — the 29 sites dispatched first time round came back DNS-dead,
            # connection-refused or geo-blocked (`kept=0`, every one), and without this the button
            # re-dispatches that same dead list on every single press.
            tried = {d for (d,) in db.execute(select(JobRun.domain).distinct()) if d}
            want = [] if have is None else [
                s for s in db.scalars(select(Source)).all()
                if s.domain and s.domain.lower().lstrip("www.") not in have
                and s.domain not in tried
                and shard.owner_of(db, s.domain, n_pool) in idle_now]
            # Built here rather than via jobgen.generate, which returns NOTHING for these: it only
            # emits a manufacturer_ir job when the Source carries seed_urls, and no row in this
            # catalogue does (0 of 807) — the live crawl comes from scheduled_jobs, whose job JSON
            # is this shape. Reusing that shape is what makes the dispatched job identical to the
            # one the scheduler would have produced six days later.
            jobs = [{"job_id": "firstcrawl_" + s.domain.replace(".", "_"),
                     "job_type": jobgen.job_type_for(s.category),
                     # The VERIFIED seed when the import probe found one — MBDA answers only on
                     # https://www., NORINCO and CASIC only on http://www. Falling back to the
                     # conventional apex keeps every un-probed row behaving exactly as before.
                     "seed_urls": (s.seed_urls or ["https://%s/" % s.domain]),
                     "source_id": s.domain, "same_domain_only": True,
                     "max_depth": -1, "max_pages": 2000} for s in want]
            if jobs:
                # Routed dispatch sends each job to the instance that owns its domain, so these
                # land on the idle instances by construction rather than by being asked to.
                Thread(target=orchestrate.run_manual_batch, args=(jobs, 0, "scheduled"),
                       daemon=True).start()
                dispatched = {"instances": idle_now, "sites": len(want), "jobs": len(jobs),
                              "domains": sorted(s.domain for s in want)[:20]}
            elif have is not None:
                dispatched = {"instances": idle_now, "sites": 0, "jobs": 0,
                              "skipped": "they own no site that is both uncrawled and untried"}

    resumed = int(res.get("resumed") or 0)
    moved = int((reb or {}).get("moved") or 0)
    bits = []
    if resumed:
        bits.append(f"resumed {resumed} saved job(s) on {res.get('idle')} idle instance(s)")
    if res.get("lane_closed"):
        bits.append("nothing resumed: the 'resume' lane is closed: %s" % res.get("reason"))
    if res.get("parked"):
        bits.append("%d saved job(s) belong to the other mode and stay parked" % res["parked"])
    if moved:
        bits.append(f"moved {moved} finished domain(s) onto idle instances — they start on the "
                    f"next dispatch")
    if (reb or {}).get("error"):
        bits.append(f"rebalance refused: {reb['error']}")
    started = int((dispatched or {}).get("jobs") or 0)
    if started:
        bits.append(f"dispatched {dispatched['sites']} never-crawled site(s) to instance(s) "
                    f"{', '.join(str(i) for i in dispatched['instances'])}")
    elif (dispatched or {}).get("skipped"):
        bits.append(f"nothing new dispatched: {dispatched['skipped']}")
    if not bits:
        bits.append("every instance already had work — nothing to assign")

    return {"ok": True, "before": before, "refill": res, "rebalance": reb, "after": after,
            "dispatched": dispatched, "resumed": resumed, "moved": moved,
            "started_sites": (dispatched or {}).get("sites") or 0,
            "summary": "; ".join(bits)}


@router.get("/data/coverage")
def data_coverage(site: str = "", db: Session = Depends(get_db)) -> dict:
    """Is a site FINISHED, or did the crawl just run out of links to follow?

    "598 of 690 sources have pages" was never an answer to that. lockheedmartin.com walked its
    frontier to exhaustion and stored 614 pages; its own sitemap lists 2,263, and the 1,457 it never
    fetched include every /capabilities/electronic-warfare/ page. The crawl did not fail — the links
    simply were not on the pages it saw.

    The sitemap is read to CROSS-CHECK and never to seed: Lockheed publishes 27 of them, one per
    locale, and feeding those to the frontier would pour six languages of the same pages into a
    corpus we had just removed 48,278 duplicate URL records from.

    Deliberately NOT routed by domain shard. All twelve crawl histories live on one shared volume
    and the endpoint reads every one of them, because a site's history is not necessarily on the
    instance that owns its domain — today's frontier moves left several spread across two. Routing
    this to the "owner" reported "2,263 of 2,263 never fetched" for a site holding 614 pages.
    """
    site = (site or "").strip().lower().removeprefix("www.")
    if not site:
        raise HTTPException(400, "site is required")
    try:
        got = crawler_client.coverage(site)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "site": site, "error": str(e)[:200],
                "summary": "the crawler did not answer the coverage check"}

    v = got.get("verdict")
    total, cov, extra = got.get("sitemap_total") or 0, got.get("covered") or 0, got.get("extra") or 0
    # One sentence per verdict, because the number alone is what made this misleading.
    summary = {
        "no_sitemap": "this site publishes no sitemap — coverage cannot be measured, which is NOT "
                      "the same as zero",
        "covered": "%s of %s sitemap URLs held — this site is covered" % (f"{cov:,}", f"{total:,}"),
        "gaps": "%s of %s sitemap URLs were never fetched" % (f"{total - cov:,}", f"{total:,}"),
        "sitemap_partial": "we hold %s URLs this sitemap does not list (it lists %s) — its index is "
                           "partial, so the shortfall is not a crawl problem"
                           % (f"{extra:,}", f"{total:,}"),
        "different_urls": "the site serves the same pages under two URL spaces — %s listed, %s held,"
                          " only %s in common. Needs a human to say which is canonical"
                          % (f"{total:,}", f"{got.get('crawled_total') or 0:,}", f"{cov:,}"),
    }.get(v, "")
    return {"ok": True, **got, "summary": summary}


GAPFILL_MAX_SEEDS = 5000


@router.post("/data/coverage/fill")
def data_coverage_fill(site: str = "", apply: bool = False,
                       db: Session = Depends(get_db)) -> dict:
    """Fetch the pages a site publishes that link-following never reached.

    THE OPERATOR RULE IS INTACT: the crawler engine still never reads a sitemap, and a test asserts
    it never imports the module. What happens here is that the ORCHESTRATOR hands the crawler a list
    of URLs, exactly as the manual-batch box and the scheduler already do. The crawl itself still
    only follows links on pages it is given.

    Four guardrails, because the reason this was refused the first time was corpus pollution:

      * ONLY a `gaps` verdict. `sitemap_partial` means we already hold more than the site lists and
        `different_urls` means the same pages under two URL spaces — seeding either would fetch
        pages we have, under a second address, which is how 48,278 duplicate URL records happened.
      * ONLY the ONE sitemap the check already compares against. lockheedmartin declares 28, one per
        locale; the other 27 are the same pages in five more languages.
      * `max_depth: 0` — fetch exactly the missing URLs and follow nothing. Their children are
        already in the same sitemap, so depth adds no coverage and only adds surprise.
      * The junk filter runs first. A sitemap can list calendar exports as happily as a nav bar.

    Dry by default; `apply=true` dispatches. Nothing here re-fetches a page we already hold — the
    seed list IS the set difference the coverage check computed.
    """
    site = (site or "").strip().lower().removeprefix("www.")
    if not site:
        raise HTTPException(400, "site is required")
    got = data_coverage(site=site, db=db)
    if not got.get("ok"):
        return got
    v = got.get("verdict")
    if v != "gaps":
        return {"ok": False, "site": site, "verdict": v, "seeds": 0,
                "summary": "refusing: verdict is %r, and only 'gaps' is a real shortfall — see the "
                           "coverage check" % v}

    # The check returns at most `missed` URLs for display; ask the crawler for the full list.
    try:
        full = crawler_client.coverage(site, missed=GAPFILL_MAX_SEEDS)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "site": site, "error": str(e)[:200],
                "summary": "could not re-read the coverage detail"}

    # The crawler already filtered these through its OWN `_is_action_link` before returning them,
    # which is why there is no junk rule here: a second copy in this service would drift, and the
    # one that matters is the one the enqueue path enforces.
    seeds = ["https://" + u if "://" not in u else u
             for u in (full.get("missed") or [])][:GAPFILL_MAX_SEEDS]
    junk = int(full.get("missed_junk") or 0)

    src = db.scalars(select(Source).where(Source.domain == site)).first()
    job = {"job_id": "gapfill_" + site.replace(".", "_").replace("-", "_"),
           "job_type": jobgen.job_type_for(src.category if src else ""),
           "seed_urls": seeds,
           "source_id": site,
           "same_domain_only": True,
           "max_depth": 0,                 # the seeds ARE the work; follow nothing
           "max_pages": len(seeds)}
    out = {"ok": True, "site": site, "verdict": v, "seeds": len(seeds), "junk_skipped": junk,
           "sitemap_total": full.get("sitemap_total"), "already_held": full.get("covered"),
           "sample": seeds[:8], "applied": bool(apply),
           "truncated": len(full.get("missed") or []) >= GAPFILL_MAX_SEEDS}
    if not seeds:
        out["summary"] = "nothing to fetch — every missing URL was filtered as junk"
        return out
    if not apply:
        out["summary"] = ("%s URL(s) this site lists and we do not hold — dry run, nothing dispatched"
                          % f"{len(seeds):,}")
        return out
    if not scheduler.master_enabled(db):
        return {**out, "ok": False, "summary": "the master switch is off"}
    if not scheduler.lane_open(db, "manual"):
        return {**out, "ok": False, "summary": "the 'manual' lane is closed by the dispatch policy"}

    # THE OWNING INSTANCE HAS TO BE FREE, and this is checked rather than attempted. run_manual_batch
    # routes by domain shard and the crawler refuses a second concurrent batch — but it refuses
    # WITHOUT raising, on a daemon thread nobody is reading, so the first live run of this reported
    # "dispatched 1,457 missing URL(s)" and did absolutely nothing. No JobRun, no log line, no pages.
    # A dispatch that cannot happen must say so to the caller's face.
    from . import shard
    owner = shard.owner_of(db, site, len(crawler_client.pool()))
    if owner is not None and owner not in set(scheduler.free_instances()):
        return {**out, "ok": False, "blocked_by": owner,
                "summary": "instance %s owns %s and is mid-crawl — nothing was dispatched. Re-run "
                           "when it is free (the fleet check shows which are)" % (owner, site)}

    Thread(target=orchestrate.run_manual_batch, args=([job], 0), daemon=True).start()
    out["instance"] = owner
    out["summary"] = ("dispatched %s missing URL(s) for %s to instance %s — depth 0, so exactly "
                      "these pages" % (f"{len(seeds):,}", site, owner))
    return out


@router.post("/crawler/fleet/maintenance")
def crawler_fleet_maintenance(on: bool = True, wait_s: float = 180.0,
                              db: Session = Depends(get_db)) -> dict:
    """Hold the fleet still so frontiers can be moved, then let it go again.

    Turning it ON stops every instance AND stops anything putting them back: auto-refill and the
    autopilot both stand down while the flag is set. Turning it OFF resumes whatever each instance
    holds. The durable frontier is never dropped — that is what separates this from the master
    switch, which drops it, and is why "just turn the crawler off" was never the right answer.

    `wait_s` is how long to wait for teardown before reporting. Instances are NOT force-killed here:
    the caller is told which are still winding down, because a wedged instance is a fact worth
    seeing rather than a container to restart behind the operator's back.
    """
    scheduler.set_maintenance(db, bool(on))
    if not on:
        # Lifting the hold used to ALSO run a forced refill — measured resuming 64 jobs at once,
        # an auto-dispatch nobody asked for. It is now the refill TOGGLE's decision (policy.AUTOS):
        # on, and the fleet picks up as before; off, and the operator presses Assign when ready.
        if not policy.auto_on(db, "refill"):
            return {"ok": True, "maintenance": False, "resumed": 0,
                    "summary": "maintenance off — nothing resumed: the 'refill' toggle is off "
                               "(or the pipeline is stopped); press Assign to resume saved work"}
        from . import refill
        res = refill.refill_once(force=True)
        return {"ok": True, "maintenance": False, "resumed": res.get("resumed", 0),
                "summary": ("maintenance off — nothing resumed: the 'resume' lane is closed: %s"
                            % res.get("reason") if res.get("lane_closed") else
                            "maintenance off — resumed %s saved job(s)" % res.get("resumed", 0))}

    for r in crawler_client.fanout(crawler_client.stop_crawl):
        pass                                   # errors surface in the status poll below, not here

    deadline = time.monotonic() + max(0.0, float(wait_s))
    busy: list[int] = []
    while True:
        busy = [r["instance"] for r in crawler_client.fanout(crawler_client.batch_status)
                if r.get("ok") and (r.get("data") or {}).get("running")]
        if not busy or time.monotonic() >= deadline:
            break
        time.sleep(5)

    return {"ok": True, "maintenance": True, "still_running": busy,
            "summary": ("maintenance on — every instance stopped and refill is held off"
                        if not busy else
                        "maintenance on, but instance(s) %s are still winding down; they hold "
                        "workers inside the item watchdog. Wait, or restart those containers — "
                        "the frontier is durable in sqlite and survives a restart"
                        % ", ".join(str(i) for i in busy))}


@router.post("/crawler/fleet/prune")
def crawler_fleet_prune(apply: bool = False) -> dict:
    """Clear queued junk out of all twelve frontiers — calendar exports, login recursion.

    THE FAULT THIS FIXES is a fleet that looks busy and produces nothing. Measured 2026-07-31: four
    instances each held 128 of 128 render-lane slots with ZERO renders in flight, load average was
    79 on a 40-core box, and the corpus had grown by two pages in an hour. 14,049 of 19,014 queued
    URLs were software artefacts — `?ical=1` calendar exports that return a file rather than a page,
    and a login form whose `?back=` parameter re-encoded its own URL for ever. Each was fetched
    through a BROWSER, timed out on the 240s item watchdog, and requeued itself.

    Tightening the enqueue filter alone would not have moved a thing: it governs discovery, and a
    crawl works its queue long before it discovers anything new. The already-queued junk had to go
    too, which is what this does.

    `apply` defaults to false — the dry run reports what it would take, per host, and writes
    nothing. An instance mid-batch REFUSES the write (409) because those rows are its resume point;
    that instance is reported as skipped rather than failing the sweep, so pruning the idle ones
    still happens.
    """
    rows = crawler_client.fanout(crawler_client.prune_frontier, apply=apply)
    by_host: dict[str, int] = {}
    pending = junk = 0
    ok = busy = failed = 0
    per: list[dict] = []
    for r in rows:
        if r.get("ok") and isinstance(r.get("data"), dict):
            d = r["data"]
            ok += 1
            pending += int(d.get("pending") or 0)
            junk += int(d.get("junk") or 0)
            for h, n in (d.get("by_host") or {}).items():
                by_host[h] = by_host.get(h, 0) + int(n)
            per.append({"instance": r["instance"], "pending": d.get("pending"),
                        "junk": d.get("junk")})
        else:
            # 409 = that instance is crawling. A real distinction: it is a refusal to race a live
            # engine, not a broken instance, and reporting them the same way would send an operator
            # looking for a fault that is not there.
            err = str(r.get("error") or "")
            if "409" in err or "batch is running" in err:
                busy += 1
                per.append({"instance": r["instance"], "skipped": "crawling"})
            else:
                failed += 1
                per.append({"instance": r["instance"], "error": err[:160]})

    verb = "removed" if apply else "would remove"
    bits = [f"{verb} {junk:,} queued URL(s) of {pending:,}"]
    if by_host:
        top = sorted(by_host.items(), key=lambda kv: -kv[1])[:3]
        bits.append("worst: " + ", ".join(f"{h} {n:,}" for h, n in top))
    if busy:
        bits.append(f"{busy} instance(s) skipped — still crawling")
    if failed:
        bits.append(f"{failed} instance(s) failed")
    return {"ok": failed == 0, "applied": bool(apply), "pending": pending, "junk": junk,
            "instances": {"pruned": ok, "busy": busy, "failed": failed},
            "by_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])[:20]),
            "per_instance": per, "summary": "; ".join(bits)}


@router.post("/crawler/fleet/refill")
def crawler_fleet_refill() -> dict:
    """Put idle instances back to work now, without waiting for the timer.

    The same thing the auto-refill loop does every minute: any instance that is not crawling but
    still holds saved frontiers resumes its own. Exposed as a button because "why is that instance
    idle" is a question an operator asks in the moment.
    """
    from . import refill
    return {**refill.refill_once(), "auto_enabled": refill.enabled()}


@router.post("/crawler/fleet/rebalance")
def crawler_fleet_rebalance(db: Session = Depends(get_db)) -> dict:
    """Spread finished domains across a fleet that just grew.

    Adding instances does nothing on its own: every catalogued domain already has an owner, and the
    assignment is deliberately never changed, so new containers would sit empty for ever. This moves
    exactly the domains that are safe to move — the ones with no pending frontier anywhere — and
    holds the rest, because a domain mid-crawl keeps its progress inside its current container.

    Safe to run while crawling: held domains are untouched, and a moved domain has nothing in
    flight by definition.
    """
    from . import shard

    # Everything with pending work, asked of every instance. If an instance cannot be reached we
    # must NOT rebalance — its domains would look finished and be moved away from the frontier they
    # still own. Refusing is the only safe answer.
    rows = crawler_client.fanout(crawler_client.resumable)
    down = [r for r in rows if not r.get("ok")]
    if down:
        raise HTTPException(503, f"{len(down)} instance(s) unreachable — refusing to rebalance, "
                                 f"their in-flight domains would look finished and be moved")
    busy: set[str] = set()
    for r in rows:
        for j in ((r["data"] or {}).get("jobs") or []):
            h = shard.host_of("https://" + str(j.get("host") or "")) or str(j.get("host") or "")
            if h:
                busy.add(h)
    n = len(crawler_client.pool())
    out = shard.rebalance(db, n, busy)
    out["note"] = ("moved domains take effect on the NEXT dispatch — nothing in flight is "
                   "interrupted") if out["moved"] else "nothing safe to move"
    return out


@router.get("/crawler/batch/sites")
def crawler_batch_sites(site: str = "", reason: str = "",
                        db: Session = Depends(get_db)) -> dict:
    """Per-site outcome of the running (or last) batch: fetched / kept / retryable / terminal, and
    the reasons behind the last two.

    This is the only place the crawler can answer "what happened on THIS site" — /v1/metrics is a
    single set of batch-wide totals, and a site crawling badly is invisible inside them. `fetched`
    is derived here rather than read: the crawler reports the three outcome buckets, and their sum
    IS the number of URLs it finished with, so computing it once here keeps every caller from
    inventing its own definition.

    Reachability is explicit. An empty site list because the crawler is down and an empty one
    because nothing has been crawled are different facts, and a panel that renders them the same
    way is the exact bug this dashboard's ONE RULE exists to prevent.
    """
    # Without `host` this is the TABLE: every site's counters, and NO url lists. The lists are what
    # made it expensive — 60 URLs x 624 sites, built and then discarded by a panel showing one row,
    # at 23 seconds a click across a 12-instance fleet. With `host` it is the DRILL-DOWN: one site,
    # its URLs included. Measured after: 0.5s for the table, 0.7s for a site.
    urls_wanted = 60 if site else 0
    # ASK ONE INSTANCE, not twelve. Routing guarantees a host lives on exactly one container, so a
    # fan-out for a single site makes eleven containers build a full batch report that is then
    # discarded — and because fanout waits for all of them, the click costs the SLOWEST instance's
    # report, not the relevant one's. Measured on the live fleet: 30.3s for one row.
    #
    # Only when the shard table already knows the owner. An unassigned domain falls back to the
    # fan-out: slow, but it cannot answer "nothing here" about a site that has plenty.
    from . import shard          # deferred, like every other use of it here (see test_deferred_imports)
    bases = crawler_client.pool()
    owner = shard.owner_of(db, site, len(bases)) if site else None
    rows = []
    if owner is not None and owner < len(bases):
        rows = [crawler_client.fanout_one(owner, bases[owner], crawler_client.batch_report,
                                          host=site, urls=urls_wanted, reason=reason)]
    # FALL BACK WHEN THE OWNER HAS NOTHING. The single-instance route assumes a host lives on
    # exactly one container — true for the FRONTIER, and no longer true for HISTORY once
    # move_frontier has run: the pending URLs move, but rows already crawled stay under the old
    # instance's run id, so the new owner reports the site as empty. Measured right after a
    # 237-job rebalance: owner_of(saab.com) said instance 1, which had 0 rows, while instance 4
    # held the whole report — and the panel said "no per-URL report for this site yet" about a site
    # with 24 unresolved URLs. Costs one wasted round trip only in the case that was broken before.
    if not any((r.get("data") or {}).get("sites") for r in rows if r.get("ok")):
        rows = crawler_client.fanout(crawler_client.batch_report, host=site, urls=urls_wanted,
                                     reason=reason)
    up = [r for r in rows if r.get("ok")]
    if not up:
        return {"reachable": False, "error": str(rows[0].get("error"))[:200] if rows else "no instances",
                "sites": [], "totals": {}, "site": site or None}
    # CONCATENATE, do not sum. Routing guarantees a host lives on exactly one instance, so two rows
    # for the same host would mean the partition broke — merging them by addition would hide that.
    sites, run_ids, seen = [], [], set()
    for r in up:
        rep = r["data"] or {}
        run_ids.extend(rep.get("run_ids") or [])
        for s in (rep.get("sites") or []):
            # `h`, NOT `host`: this loop used to reassign the route's own parameter, so by the time
            # the response was built the requested site had been overwritten by the last row read —
            # the endpoint answered "?site=diehl.com" with `"site": "zala-aero.com"`. The filter
            # itself ran before the loop and was fine; the echo was not, and an endpoint that
            # misreports what it was asked is impossible to debug against.
            h = s.get("host") or ""
            kept = int(s.get("kept") or 0)
            retry = int(s.get("retryable") or 0)
            term = int(s.get("terminal") or 0)
            # Reported on the ROW, not to a logger this module does not have. The logger call here
            # raised NameError on every request, so the whole per-site drill-down 500'd and the
            # panel said "the crawler is not answering" — blaming the crawler for an orchestrator
            # bug. Surfacing it in the payload also puts it where it can be acted on: a host on two
            # instances means the domain partition broke, and only this response can show that.
            dup = h in seen
            seen.add(h)
            sites.append({
                "host": h,
                "fetched": kept + retry + term,
                "kept": kept, "retryable": retry, "terminal": term,
                "instance": r["instance"],
                "duplicate_instance": dup,
                "reasons": s.get("reasons") or {},
                # The drill-down list is capped crawler-side at 500 per site; trimmed harder here
                # because this rides a live poll and a 500-row payload per site is a page freeze.
                "urls": (s.get("urls") or [])[:60],
            })
    tot = {"kept": sum(s["kept"] for s in sites), "retryable": sum(s["retryable"] for s in sites),
           "terminal": sum(s["terminal"] for s in sites),
           "dropped_by_gate": sum(int(((r["data"] or {}).get("totals") or {}).get("dropped_by_gate") or 0)
                                  for r in up),
           "fetched": sum(s["fetched"] for s in sites)}
    return {"reachable": True, "run_ids": run_ids, "sites": sites, "totals": tot,
            "site": site or None, "reason": reason or None,
            "instances": {"up": len(up), "down": len(rows) - len(up), "total": len(rows)}}


@router.get("/crawler/batch/status")
def crawler_batch_status() -> dict:
    # 4s, not 30s: this answers "is a crawl running" and the Watchfloor polls it every 3s, so it has
    # to stay live — the cache is here only to collapse simultaneous pollers into one fan-out.
    return _rollup("batch_status", _crawler_batch_status_live, ttl=4.0)


def _crawler_batch_status_live() -> dict:
    """Live progress of the crawl — whoever started it.

    `manual_status()` only knows about batches THIS service dispatched. A Resume goes straight to
    the crawler, so during one the orchestrator reported `running: false, done 0/0` while the
    crawler was working through 1,566 URLs: the dashboard said "nothing crawling right now" on top
    of a live crawl, which is the worst kind of wrong a status panel can be.

    So when our own bookkeeping is idle we ask the crawler and report what IT is doing. The hosts
    come from the batch report, which is the only per-site view of an in-flight run.
    """
    st = orchestrate.manual_status()
    if st.get("running"):
        return st
    # Across the fleet: running is any(), totals add up, and the host lists concatenate. Asking only
    # instance 0 would report "nothing crawling" whenever the work happened to be routed elsewhere.
    rows = crawler_client.fanout(crawler_client.batch_status)
    up = [r for r in rows if r.get("ok")]
    if not up:  # every instance down: our own view is all there is
        return st
    cs = {"running": any((r["data"] or {}).get("running") for r in up),
          "total": sum(int((r["data"] or {}).get("total") or 0) for r in up),
          "done": sum(int((r["data"] or {}).get("done") or 0) for r in up),
          "hosts": [h for r in up for h in ((r["data"] or {}).get("hosts") or [])],
          "run_ids": [x for r in up for x in ([(r["data"] or {}).get("run_id")]
                                              if (r["data"] or {}).get("run_id") else [])]}
    if not cs["running"]:
        return st

    # Deduplicated on the www-STRIPPED key, not the literal string. The two sources spell the same
    # site differently — the report names the host as fetched (www.cyientdlm.com) and the run names
    # the seed host (cyientdlm.com) — so an exact-match check listed both and the live table showed
    # 10 rows for 8 sites, each one twice with different numbers. The report's spelling wins because
    # it is the host pages actually came from.
    hosts: list[str] = []
    seen: set[str] = set()

    def _add(h: str) -> None:
        h = str(h or "").lower()
        k = h.removeprefix("www.")
        if h and k not in seen:
            seen.add(k)
            hosts.append(h)

    # urls=0. This loop wants HOST NAMES and nothing else, but batch_report defaults to urls=500 —
    # so every 3s poll had all twelve instances build 500 unresolved-URL records per site and ship
    # them, for a caller that reads s["host"] and discards the rest. Measured: 30.4s per poll, on
    # the endpoint the live-site table depends on, which is why that table sat empty over a crawl
    # doing 14.7 pages/s. The same report with urls=0 is the cheap per-site table: ~0.5s.
    for r in crawler_client.fanout(crawler_client.batch_report, urls=0):
        if r.get("ok"):
            for s in ((r["data"] or {}).get("sites") or []):
                _add(s.get("host") or "")
    # The report only knows a host once a URL there has FINISHED, so for the opening minutes of a
    # crawl it is empty and the panel said "nothing crawling right now" over a working batch. The
    # crawler now publishes the run's seed hosts as soon as it has them; those are the sites in the
    # run whether or not any of them has landed a page yet.
    for h in (cs.get("hosts") or []):
        _add(h)
    return {**st, "running": True, "source": "crawler",
            "total": cs.get("total") or len(hosts) or None,
            "done": cs.get("done") or 0,
            "parallel": st.get("parallel") or len(hosts) or None,
            "current_domains": hosts,
            "current": cs.get("run_ids") or []}


# ── Keyword bank ──────────────────────────────────────────────────────────────

@router.get("/keywords")
def list_keywords(db: Session = Depends(get_db)) -> list[dict]:
    return [_kw_dto(k) for k in db.scalars(select(Keyword).order_by(Keyword.id.desc())).all()]


@router.post("/keywords")
def add_keywords(body: KeywordCreate, db: Session = Depends(get_db)) -> dict:
    terms = list(body.terms or [])
    if body.term:
        terms.append(body.term)
    terms = [t.strip() for t in terms if t and t.strip()]
    existing = {k.term.lower() for k in db.scalars(select(Keyword)).all()}
    added, skipped = [], 0
    for t in terms:
        if t.lower() in existing:
            skipped += 1
            continue
        k = Keyword(term=t, group_label=body.group or "general", created_at=scheduler.utcnow())
        db.add(k)
        existing.add(t.lower())
        added.append(k)
    db.commit()
    for k in added:
        db.refresh(k)
    return {"added": len(added), "skipped": skipped, "keywords": [_kw_dto(k) for k in added]}


@router.delete("/keywords/{kid}")
def delete_keyword(kid: int, db: Session = Depends(get_db)) -> dict:
    k = db.get(Keyword, kid)
    if not k:
        raise HTTPException(404, "no such keyword")
    db.delete(k)
    db.commit()
    return {"deleted": kid}


# ══ Dashboard tab ═════════════════════════════════════════════════════════════
# Hardware + fetch-tier health. Both come from the CRAWLER process, not this one: it is the
# container actually under load, so its psutil reading is the number that means something. The
# orchestrator sits idle and would report a reassuring 2% CPU while the crawler pegs 24 cores.

@router.get("/host")
def host_metrics() -> dict:
    """Hardware utilisation and live crawl progress — the FAST poll (~0.3s, psutil-bound).

    Tier health deliberately lives at /api/tiers instead of here. Folding it in cost 10s per call
    on a cache miss (three real network probes), which the dashboard polls every 3s — so slow
    requests stacked up and the hardware tiles froze behind a Wayback timeout.

    Reports `reachable:false` rather than zeros. A dashboard that renders a dead crawler as an idle
    one is worse than a blank panel — it looks like everything is fine.
    """
    # FLEET-MERGED, like /api/crawler/metrics. This called get_metrics() with no base, so it always
    # described INSTANCE 0 ALONE — one engine's pages, one engine's frontier, one engine's browser
    # pool — while /api/crawler/metrics next to it reported all twelve. Whichever panel happened to
    # read this one showed a twelve-container fleet as a single crawler, and it looked like the
    # dashboard "reverting to one instance" at random: it was not random, it was which endpoint the
    # view used. `instances` is carried through so a real fleet shrink still looks different from a
    # merge bug.
    m = _merge_metrics(crawler_client.fanout(crawler_client.get_metrics))
    if not m.get("reachable", True):
        return m
    return {"reachable": True,
            "cpu_percent": m.get("cpu_percent"), "cpu_count": m.get("cpu_count"),
            "ram": m.get("ram"), "disk": m.get("disk"), "pool": m.get("pool"),
            "adaptive": m.get("adaptive"), "progress": m.get("progress"),
            "batch": m.get("batch"), "paid_proxy": m.get("paid_proxy"),
            "container_ram": m.get("container_ram"), "lanes": m.get("lanes"),
            "issues": m.get("issues"), "instances": m.get("instances")}


@router.get("/tiers")
def tier_health() -> dict:
    """C1/C2/C3/C4 fetch-tier health — the SLOW poll. Real network probes, cached 30s by the crawler."""
    try:
        return crawler_client.get_tiers()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@router.get("/data/types")
def data_types() -> dict:
    """Stored documents broken down by DATA TYPE (html/text/pdf) and by FETCH TIER (served_by)."""
    return _rollup("types", _data_types_live)


def _data_types_live() -> dict:
    try:
        d = _ingest_get("/api/live/types", timeout=60.0)   # same aggregate, same reason as summary
        d["reachable"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


@router.get("/data/quality")
def data_quality() -> dict:
    """Dedupe + cleanliness audit of the corpus — what "Check data quality" asks for.

    Cached like the other rollups: it is a full-table scan, and the button is next to a 3s poll.
    """
    return _rollup("quality", _data_quality_live)


def _data_quality_live() -> dict:
    try:
        d = _ingest_get("/api/live/quality", timeout=120.0)
        if d.get("error"):
            return {"reachable": False, "error": str(d["error"])[:200]}
        d["reachable"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


@router.post("/data/dedupe")
def data_dedupe(apply: bool = False) -> dict:
    """Collapse pages whose bytes already exist under another URL of the same site.

    22,823 of them when this was written, and they are not a scattering of near-misses: one login
    form on zentauron.de carrying `?back=<its own escaped url>` accounted for 3,304, with 78
    distinct URLs under a single content hash. The crawler no longer follows that shape, so the
    number stops growing; this is what clears what it already made.

    Content is never lost — every deleted row's bytes stay under the URL that is kept (the shortest
    in each group, which is reliably the canonical one) and MinIO is not touched at all, because
    blobs are content-addressed and shared with rows that survive.

    Dry unless `apply=true`, and the count is RE-READ from the store afterwards rather than inferred
    from what was deleted.
    """
    import httpx
    base = get_settings().ingest_api.rstrip("/")
    try:
        # Minutes, not seconds: this is a delete across the whole corpus, and a client timeout that
        # fires mid-statement leaves the operator unable to tell whether it ran.
        r = httpx.post(f"{base}/api/live/dedupe", params={"apply": apply}, timeout=600.0)
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reachable": False, "error": str(e)[:200],
                "summary": "the store did not answer — nothing was changed"}
    if d.get("error"):
        return {"ok": False, "reachable": False, "error": str(d["error"])[:200],
                "summary": "the store reported: %s" % str(d["error"])[:120]}

    n = int(d.get("redundant_urls") or 0)
    if not n:
        d["summary"] = "no duplicate content — every page's bytes are unique to its URL"
    elif d.get("applied"):
        d["summary"] = ("removed %s duplicate URL(s) across %s site(s); %s left"
                        % (f"{d.get('rows_deleted', n):,}", d.get("sources"),
                           f"{int(d.get('duplicate_content_after') or 0):,}"))
        # The quality and per-source rollups are stale by construction now — leaving them would
        # show the pre-delete duplicate count for up to a TTL and read as "the button did nothing".
        _rollup_cache.clear()
    else:
        d["summary"] = ("%s duplicate URL(s) across %s site(s) in %s group(s) — nothing deleted"
                        % (f"{n:,}", d.get("sources"), f"{int(d.get('groups') or 0):,}"))
    # Said out loud rather than buried: these rows are duplicates the pass deliberately KEPT because
    # they are the only reference to a stored PDF. Without this line the counts look like an
    # arithmetic error — "48,278 duplicates, 48,014 removed" with nothing explaining the gap.
    if d.get("kept_for_attachments"):
        d["summary"] += (" · %s kept — they hold a PDF no other copy references"
                         % f"{int(d['kept_for_attachments']):,}")
    return {"ok": True, "reachable": True, **d}


@router.get("/data/categories")
def data_categories(db: Session = Depends(get_db)) -> dict:
    """Stored documents rolled up by CATALOG CATEGORY.

    `documents` has no category column, so this is a join done here rather than in SQL: ingest
    reports pages per source_id (a bare registered domain), and this service's catalog knows each
    domain's category. Domains the catalog has never seen are reported under `uncatalogued` instead
    of being dropped — a rollup that silently loses rows understates the store, which is precisely
    the class of bug that made the old panel untrustworthy.
    """
    # Shares the per-source cache entry with /data/sources?limit=5000 — same scan, same answer, and
    # the Watchfloor polls both.
    got = _rollup("sources:5000", lambda: _data_sources_live(5000))
    if not got.get("reachable"):
        return {"reachable": False, "error": got.get("error", "ingest unreachable"),
                "categories": [], "unmatched": []}
    rows = got["rows"]

    cat_of: dict[str, str] = {}
    for s in db.scalars(select(Source)).all():
        cat_of[s.domain.lower()] = s.category
        cat_of[(s.source_id or "").lower()] = s.category

    def _category_for(sid: str) -> tuple[str | None, bool]:
        """(category, was_inferred). Walks up the domain before giving up.

        The catalogue is keyed on the registered domain, but a crawl stores pages under the host it
        actually fetched from — so `new.diehl.com`, `eu.glock.com`, `automation.honeywell.com` and
        `corporate.arcelormittal.com` all missed an exact match and were filed as "uncatalogued",
        which read as 35 unknown sites when every one of them is a catalogued manufacturer reached
        through its own subdomain or a redirect. Walking parent-wards is the actual attribution, not
        a cosmetic relabel: `a.b.example.com` -> `b.example.com` -> `example.com`.
        """
        sid = sid.removeprefix("www.")
        if sid in cat_of:
            return cat_of[sid], False
        parts = sid.split(".")
        for i in range(1, len(parts) - 1):
            parent = ".".join(parts[i:])
            if parent in cat_of:
                return cat_of[parent], True
        return None, False

    buckets: dict[str, dict] = {}
    unmatched: list[dict] = []
    inferred = 0
    for r in rows:
        sid = str(r.get("source_id") or "").lower()
        cat, was_inferred = _category_for(sid)
        inferred += 1 if was_inferred else 0
        if cat is None:
            # Everything stored got here from a seed in the catalogue, so a host with no parent match
            # is a redirect target, not a new kind of thing. Filing it under the majority category
            # keeps the five buckets exhaustive — the operator asked for exactly five and no
            # "uncatalogued" — while `attributed` below keeps the inference visible instead of
            # passing a guess off as catalogue truth.
            cat = _majority_category(cat_of) or "manufacturer_ir"
            unmatched.append({"source_id": r.get("source_id"), "pages": r.get("pages", 0),
                              "filed_as": cat})
        # Group by the BUCKET, not by the raw taxonomy. Grouping by raw and labelling afterwards put
        # "News" on screen twice — `trade_press` and `aggregator` are two rows that mean one thing —
        # and split that category's page count across both, so neither number answered "how much
        # news have we stored". `uncatalogued` has no bucket and keeps its own name.
        key = ui_category(cat) or cat
        b = buckets.setdefault(key, {"category": key, "category_label": key,
                                     "sites": 0, "pages": 0,
                                     "html_bytes": 0, "with_html": 0})
        b["sites"] += 1
        b["pages"] += int(r.get("pages") or 0)
        b["with_html"] += int(r.get("with_html") or 0)
        b["html_bytes"] += int(r.get("html_bytes") or 0)
    out = sorted(buckets.values(), key=lambda x: -x["pages"])
    return {"reachable": True, "categories": out,
            # Still reported, still countable — but as "how these were attributed", not as a bucket
            # on screen. Dropping the field entirely would hide a guess; keeping a visible
            # `uncatalogued` slice was what the operator asked to be rid of.
            "unmatched": sorted(unmatched, key=lambda x: -int(x.get("pages") or 0))[:50],
            "attributed": {"by_parent_domain": inferred, "by_majority": len(unmatched)},
            "total_pages": sum(b["pages"] for b in out)}


def _majority_category(cat_of: dict[str, str]) -> str | None:
    """The catalogue's most common category — the sane home for a redirect target nothing claims."""
    if not cat_of:
        return None
    return Counter(v for v in cat_of.values() if v).most_common(1)[0][0]


# ══ Sources tab — CSV / Excel import ══════════════════════════════════════════
# The file arrives as a RAW body, not multipart: FastAPI needs `python-multipart` for UploadFile
# and this service's dependency list is deliberately five packages long. `fetch(url, {body: file})`
# posts the bytes directly, which costs nothing and adds no dependency.

@router.post("/sources/import")
async def import_preview(request: Request, filename: str = "upload.csv") -> dict:
    """Parse an uploaded CSV/XLSX and return what it CONTAINS. Commits nothing — the operator sees
    their own columns, the resolved category, and any bad rows before anything touches the catalog."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    try:
        columns, raw_rows = importer.parse(data, filename)
    except Exception as e:  # noqa: BLE001 — a malformed spreadsheet is operator error, not a 500
        raise HTTPException(422, f"could not parse {filename}: {str(e)[:200]}")
    if not raw_rows:
        return {"filename": filename, "format": importer.sniff_format(data, filename),
                "columns": columns, "count": 0, "rows": [], "errors": []}

    rows, errors = [], []
    for i, norm in enumerate(importer.normalise(raw_rows), 1):
        if not norm.get("url"):
            errors.append({"row": i, "error": "no usable URL column", "raw": norm.get("_raw", {})})
            continue
        res = resolve_source(norm["url"])
        rows.append({
            "row": i, "url": norm["url"],
            # The spreadsheet's own category wins when it names a real taxonomy value; otherwise the
            # heuristic resolver decides. Reported either way so the operator can see which happened.
            "category": norm.get("category") if norm.get("category") != "unknown" else res.category,
            "category_label": ui_category(norm.get("category")
                                          if norm.get("category") != "unknown" else res.category),
            "category_from": "file" if norm.get("category") != "unknown" else res.resolved_by,
            "domain": res.domain, "source_id": res.source_id, "tier": res.tier,
            # Catalog budget rule: under 100 -> 100, else +50 (354 -> 404). Applied HERE, at the
            # boundary where a sheet row becomes a catalog entry, for two reasons: the preview then
            # shows exactly what the catalog will hold, and it is applied exactly ONCE — /commit
            # stores the reviewed rows verbatim, so repeating it there would add 50 twice (454).
            "max_pages": importer.catalog_max_pages(norm.get("max_pages")),
            "max_depth": norm.get("max_depth"),
            "frequency": norm.get("frequency"), "region": norm.get("region"),
            "notes": norm.get("notes"), "render_js": norm.get("render_js"),
            "raw": norm.get("_raw", {}),
        })
    return {"filename": filename, "format": importer.sniff_format(data, filename),
            "columns": columns, "count": len(rows), "rows": rows, "errors": errors}


class ImportRow(BaseModel):
    url: str
    category: str | None = None
    max_pages: int | None = None
    max_depth: int | None = None
    frequency: str | None = None
    region: str | None = None
    notes: str | None = None
    render_js: bool | None = None
    raw: dict = {}


class ImportCommit(BaseModel):
    rows: list[ImportRow]
    source_file: str | None = None


@router.post("/sources/import/commit")
def import_commit(body: ImportCommit, db: Session = Depends(get_db)) -> dict:
    """Commit reviewed import rows: upsert the Source, keep the operator's original row verbatim in
    the side-car, and create the adaptive-cadence policy so the site is schedulable immediately."""
    if not body.rows:
        raise HTTPException(400, "no rows")
    now = scheduler.utcnow()
    added, errors = [], []
    for r in body.rows:
        try:
            src = orchestrate.upsert_source(db, r.url, frequency=(r.frequency or "daily"),
                                            category=r.category, region=r.region)
            db.merge(SourceRow(
                domain=src.domain, raw=r.raw or {},
                params={"max_pages": r.max_pages, "max_depth": r.max_depth,
                        "frequency": r.frequency, "region": r.region, "notes": r.notes,
                        "render_js": r.render_js},
                imported_at=now, source_file=body.source_file))
            added.append(src.domain)
        except Exception as e:  # noqa: BLE001
            errors.append({"url": r.url, "error": str(e)[:200]})
    db.commit()

    # VERIFY THE SEED BEFORE THE SITE IS SCHEDULABLE. Every seed here used to be built as
    # `https://<apex>/`, and that one shape is wrong for a large minority of real sites: of the 92
    # catalogued sources holding zero pages, 28 were alive and simply had the wrong URL — MBDA,
    # NORINCO, CASIC, CSSC, Almaz-Antey, Rosoboronexport among them, most needing http:// and/or
    # www. A site must not enter the catalogue with a seed nobody has confirmed answers.
    #
    # Best-effort and non-fatal: a domain that does not answer keeps the conventional
    # https://<apex>/ (seed_urls stays null) and is crawled exactly as before, so a bad network
    # minute during an import can never permanently rewrite a good seed.
    probed = 0
    try:
        from . import seedprobe
        for dom, url in seedprobe.resolve_many(added).items():
            if not url:
                continue
            s = db.get(Source, dom)
            if s is not None and url.rstrip("/") != ("https://" + dom).rstrip("/"):
                s.seed_urls = [url]
                probed += 1
        db.commit()
    except Exception as e:  # noqa: BLE001 — an import must never fail on a probe
        log.warning("seed probe failed during import: %s", e)

    policies = tiering.ensure_site_schedules(db)
    return {"imported": len(added), "policies_created": policies, "seeds_corrected": probed,
            "domains": added, "errors": errors}


# Where the rotating recheck below is up to. Module-level and deliberately not persisted: losing it
# on a restart costs one repeated batch, and the alternative is a schema migration for a cursor.
_SEED_RECHECK_AT = 0
SEED_RECHECK_TIMEOUT_S = 10.0

# The SLOW lane: sites holding one substantial page. Most are genuinely finished and their answer
# will never change, but some are a landing page for a company that has moved (faudi.com), and only
# a probe separates the two. Every 40th call at the 15-minute autopilot cadence is ~once every ten
# hours, so a settled site costs a handful of requests a day and a move is still noticed within one.
SETTLED_RECHECK_EVERY = 40
SETTLED_RECHECK_BATCH = 8
_SETTLED_RECHECK_AT = 0
_SEED_RECHECK_TICKS = 0


# A single page carrying at least this much text is a finished site, not a stuck one. Same
# threshold seedprobe uses to decide a response is a page at all, deliberately: one number, one
# meaning.
SETTLED_TEXT_CHARS = 500


def _pages_by_domain() -> dict[str, tuple[int, int]] | None:
    """host -> (stored page count, total text chars). None when the store could not be asked.

    A COUNT, not a boolean, because "has any pages" and "was actually crawled" are different
    questions and the second one is what matters. Measured: 89 catalogued sites hold exactly ONE
    page, and in every case it is the seed itself — the crawl stored the homepage and followed
    nothing.

    AND THE TEXT SIZE, because "holds one page" then splits again, and the halves need opposite
    treatment. Checked by hand against the live corpus:

        tisaaerospace.com     3,002 chars  a real single-page company site — FINISHED
        faudi.com             3,941 chars  real content — finished (it has moved, separately)
        kalashnikovgroup.ru      19 chars  a stub — genuinely stuck
        starkdefence.com        927 chars  "Parked at Loopia" — the company is gone

    Re-probing a finished one-page site achieves nothing and would repeat forever, four requests
    per rotation, for as long as the catalogue exists.
    """
    try:
        rows = _ingest_get("/api/live/sources?limit=5000")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows, list):
        return None
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        h = str(r.get("source_id") or "").lower().removeprefix("www.")
        if not h:
            continue
        n, chars = int(r.get("pages") or 0), int(r.get("text_chars") or 0)
        if n >= out.get(h, (0, 0))[0]:
            out[h] = (n, chars)
    return out


def _domains_with_pages() -> set[str] | None:
    """Hosts the STORE actually holds pages for. None when the store could not be asked.

    Deliberately NOT `_crawled_domains`, which answers a wider question — "have we been here" — by
    unioning `Source.last_crawled` with every `SiteMeasurement` row. A site that was dispatched,
    failed, and stored nothing counts as crawled there. That is exactly right for "do not
    re-dispatch this" and exactly wrong for "which sites still hold no pages": measured live, it put
    the zero-page set at 0 while 73 catalogued sites held nothing at all, which would have made the
    recheck a permanent no-op.

    None, not an empty set, when ingest cannot be reached — an empty set reads as "no site holds any
    pages" and would send the probe at the entire catalogue.
    """
    try:
        rows = _ingest_get("/api/live/sources?limit=5000")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows, list):
        return None
    return {h for r in rows
            if (h := str(r.get("source_id") or "").lower().removeprefix("www."))
            and int(r.get("pages") or 0) > 0}


@router.post("/sources/seeds/recheck")
def seeds_recheck(limit: int = 25, apply: bool = True, apply_moves: bool = False,
                  db: Session = Depends(get_db)) -> dict:
    """Re-probe the seeds of catalogued sites that still hold ZERO pages, and re-point the ones that
    answer somewhere else.

    WHY THIS RECURS RATHER THAN RUNNING ONCE. A seed probe is a point-in-time measurement, not a
    permanent verdict. Measured the same afternoon the import probe shipped: `casic.com.cn` answered
    on `http://www.casic.com.cn/` during the batch and was unreachable twenty minutes later. A site
    that happened to be down at enrolment keeps the conventional `https://<apex>/`, and nothing ever
    asks again — so it stays invisible for as long as nobody notices, which for the 92 zero-page
    sources was months.

    ZERO-PAGE SITES ONLY, which is what makes this safe to run unattended: it cannot disturb a
    working crawl, because a site with a working seed is by definition not in the set.

    BOTH PLACES, or it changes nothing. A schedule carries a FROZEN copy of its job JSON, and that
    copy — not the catalogue row — is the seed the crawl actually uses. Correcting only
    `Source.seed_urls` fixes the never-crawled dispatch path and leaves the scheduled path pointed at
    the dead URL for ever. And `next_run` comes forward with it: a correction that first takes effect
    a week from now is not a fix.
    """
    global _SEED_RECHECK_AT, _SETTLED_RECHECK_AT, _SEED_RECHECK_TICKS
    pages = _pages_by_domain()
    if pages is None:
        return {"ok": False, "checked": 0, "corrected": [], "zero_page_sites": 0,
                "summary": "the store could not be asked which sites hold pages — nothing probed"}

    # THREE STATES, not two. "Holds one page" splits again, and the split is not decidable from the
    # stored page alone:
    #
    #   stuck    0 pages, or 1 page under SETTLED_TEXT_CHARS (kalashnikovgroup.ru: 19 chars)
    #   settled  1 page with real content — EITHER a genuine one-page company site
    #            (tisaaerospace.com, 3,002 chars) OR a landing page for a company that has moved
    #            (faudi.com serves 3,941 real chars while the company is at faudi.de)
    #   crawled  2+ pages — never touched here
    #
    # Excluding `settled` outright was wrong: it made the automatic path blind to exactly the
    # faudi.com case, which is the kind this action exists to catch. Only a probe can tell the two
    # apart, so settled sites are still probed — just on a much slower rotation, because for the
    # genuine one-page sites the answer will never change.
    def _held(src: Source) -> tuple[int, int]:
        """Pages held for this catalogue entry, under its own domain OR its seed's.

        A MOVED site stores nothing under its catalogue key. The crawler stamps source_id from the
        URL it fetched, so once maxar.com is re-pointed at vantor.com the 296 pages land under
        `vantor.com` and `maxar.com` still reads one page. Counting only the catalogue key would
        re-probe a site we have just successfully crawled, for ever.
        """
        keys = {(src.domain or "").lower().removeprefix("www.")}
        for u in (src.seed_urls or []):
            h = (urlsplit(str(u)).hostname or "").lower().removeprefix("www.")
            if h:
                keys.add(h)
        n = chars = 0
        for k in keys:
            a, b = pages.get(k, (0, 0))
            n, chars = n + a, chars + b
        return n, chars

    def _classify(src: Source) -> str:
        n, chars = _held(src)
        if n == 0 or (n == 1 and chars < SETTLED_TEXT_CHARS):
            return "stuck"
        return "settled" if n == 1 else "crawled"

    srcs = [s for s in db.scalars(select(Source)).all() if s.domain]
    zero = sorted(s.domain for s in srcs if _classify(s) == "stuck")
    settled = sorted(s.domain for s in srcs if _classify(s) == "settled")
    if not zero and not settled:
        return {"ok": True, "checked": 0, "corrected": [], "zero_page_sites": 0,
                "summary": "every catalogued site holds pages — nothing to re-probe"}

    def _rotate(items: list[str], take: int, start: int) -> tuple[list[str], int]:
        """A window over a ring. Bounded per call so this never becomes a load generator, and
        rotating so it cannot re-probe the same few for ever while the rest are never asked."""
        if not items or take <= 0:
            return [], start
        take = min(take, len(items))
        start %= len(items)
        return (items + items)[start:start + take], (start + take) % len(items)

    n = max(1, int(limit or 25))
    batch, _SEED_RECHECK_AT = _rotate(zero, n, _SEED_RECHECK_AT)

    # The slow lane. At the 15-minute autopilot cadence this reaches a settled site roughly once a
    # day, against once every couple of hours for a stuck one — enough to notice a company that has
    # moved, cheap enough that a finished site costs four requests a day for ever.
    _SEED_RECHECK_TICKS += 1
    if settled and _SEED_RECHECK_TICKS % SETTLED_RECHECK_EVERY == 0:
        slow, _SETTLED_RECHECK_AT = _rotate(settled, SETTLED_RECHECK_BATCH, _SETTLED_RECHECK_AT)
        batch = batch + [d for d in slow if d not in batch]

    corrected = []
    for dom, url in seedprobe.resolve_many(batch, timeout=SEED_RECHECK_TIMEOUT_S).items():
        # None means nothing answered, and the caller KEEPS its current seed. Never clear one on a
        # failed probe — a recheck running through a bad network minute would otherwise erase the
        # very corrections an earlier pass made.
        if not url or url.rstrip("/") == ("https://" + dom).rstrip("/"):
            continue
        s = db.get(Source, dom)
        if s is None or (s.seed_urls or [""])[0].rstrip("/") == url.rstrip("/"):
            continue                              # already corrected on an earlier pass
        # A re-point onto a DIFFERENT registered domain is not a URL fix — it is a claim about who
        # the company now is, and it is NOT safe to apply unattended.
        #
        # Measured on the first live pass: 35 of 44 corrections were cross-domain, and about eight
        # of those were wrong in ways no size or status check can see. pgz.pl — the Polish Armaments
        # Group — resolved to a perfume shop; sentekglobal.com to deloitte.com; rheintech.de to an
        # ad-tracking redirect; sparta.com to a for-sale page the parked-host list had not seen.
        # An expired domain bought by an unrelated business answers 200 with a large, real page.
        #
        # So: same-domain corrections apply (a locale path or a www prefix is still the same site);
        # cross-domain ones are PROPOSED and wait for a human. That is the rule this module already
        # states for moving a domain's ownership, applied to moving its identity.
        # A SUBDOMAIN IS NOT A MOVE. `eu.glock.com` and `new.kspz.ru` are the same company's own
        # site; a strict host-equality test flags them for review alongside maxar.com -> vantor.com
        # and buries the real moves in noise.
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        root = dom.lower().removeprefix("www.")
        moved = not (host == root or host.endswith("." + root))
        corrected.append({"domain": dom, "seed": url, "moved": moved})
        if not apply or (moved and not apply_moves):
            continue
        s.seed_urls = [url]
        for sj in db.scalars(select(ScheduledJob)).all():
            hosts = {(urlsplit(str(u)).hostname or "").lower().removeprefix("www.")
                     for u in ((sj.job or {}).get("seed_urls") or [])}
            if dom.removeprefix("www.") not in hosts:
                continue
            job = dict(sj.job or {})
            job["seed_urls"] = [url]
            sj.job = job                          # reassigned, or the JSON mutation is not tracked
            sj.next_run = scheduler.utcnow()
    if apply and corrected:
        db.commit()

    applied = [c for c in corrected if not c["moved"] or apply_moves]
    proposed = [c for c in corrected if c["moved"] and not apply_moves]
    verb = "re-pointed" if apply else "would re-point"
    tail = ("; %d move(s) to a NEW DOMAIN awaiting review" % len(proposed)) if proposed else ""
    return {"ok": True, "checked": len(batch), "corrected": corrected,
            "applied": applied, "proposed": proposed, "moved": len(proposed),
            # `zero_page_sites` keeps its name and its meaning — the STUCK set, the number an
            # operator watches go down. `settled_sites` is reported separately because those are
            # finished, and folding them in would make the headline number look like a backlog.
            "zero_page_sites": len(zero), "settled_sites": len(settled),
            "cursor": _SEED_RECHECK_AT,
            "summary": "%s %d of %d re-probed seed(s)%s; %d site(s) stuck, %d settled on one page"
                       % (verb, len(applied), len(batch), tail, len(zero), len(settled))}


@router.get("/selfcheck")
def selfcheck_run(db: Session = Depends(get_db)) -> dict:
    """Live invariants for the faults unit tests cannot see — see selfcheck.py for the taxonomy.

    Every one of these guards a failure that has actually happened here and passed its tests: an
    accepted document that never reached the store, a shipped fetch tier nothing could reach, a cap
    that stranded due work on idle instances, a frozen seed copy that stopped agreeing with the
    catalogue, and an instance holding a running flag with an empty queue.
    """
    from . import selfcheck
    return selfcheck.run_all(db)


@router.get("/sources/catalog")
def sources_catalog(db: Session = Depends(get_db)) -> dict:
    """The Sources tab table: catalog row + cadence policy + the operator's original spreadsheet
    columns. `columns` is the union of every imported header, so the table can show their file."""
    extra = {r.domain: r for r in db.scalars(select(SourceRow)).all()}
    pols = {p.domain: p for p in db.scalars(select(SiteSchedule)).all()}
    cols: list[str] = []
    for r in extra.values():
        for k in (r.raw or {}):
            if k not in cols:
                cols.append(k)
    rows = []
    for s in db.scalars(select(Source).order_by(Source.domain)).all():
        e, p = extra.get(s.domain), pols.get(s.domain)
        rows.append({**_src_dto(s),
                     "params": (e.params if e else {}) or {},
                     "raw": (e.raw if e else {}) or {},
                     "imported_at": _iso(e.imported_at) if e else None,
                     "source_file": e.source_file if e else None,
                     "tier_cadence": p.tier if p else None,
                     "phase": p.phase if p else None,
                     "defence_score": round(float(p.defence_score or 0.0), 2) if p else None})
    return {"count": len(rows), "columns": cols, "rows": rows}


# ══ Job Generator tab — one job per catalogued site, chosen by CATEGORY ════════
# The legacy generator (jobgen.generate) builds an entity × search-template MATRIX, which is a
# different product: many query jobs per site. This one is what the operator asked for — one
# full-site crawl per source, selected by category, with every parameter taken from the spreadsheet.

# The taxonomy moved to categories.py — the scheduler and the Run button need the same mapping to
# decide which stop switch governs a job, and they cannot import this module. Re-exported under the
# old private names so every existing reference (and the drift tests) keep working unchanged.
_UI_CATEGORIES = categories.UI_CATEGORIES
_CAT_TO_JOBTYPE = categories.CAT_TO_JOBTYPE
_BUCKET_LABEL = categories.BUCKET_LABEL
_CAT_TO_BUCKET = categories.CAT_TO_BUCKET
ui_category = categories.ui_category

# Sites whose ENTIRE output is defence. The keyword gate on one of these can only throw away real
# pages, so it stays OFF and we capture the whole site. A mixed publisher (a general newspaper with
# a defence desk) needs the gate, or the crawl ingests its sport and markets sections.
_PURE_DEFENCE = {"manufacturer_ir", "defence_org", "tender_portal", "gov_primary",
                 "think_tank", "trade_press"}


def _keyword_gate(src: Source, pol: SiteSchedule | None) -> tuple[bool, str]:
    """The operator's rule: gate ON unless the site is pure defence. A MEASURED defence score beats
    the category guess — the probe has actually read the site, the taxonomy has not."""
    ds = float(pol.defence_score) if (pol and pol.defence_score) else 0.0
    if ds > 0:
        if ds >= 0.7:
            return False, f"measured defence_score {ds:.2f} — pure defence, gate off"
        return True, f"measured defence_score {ds:.2f} — mixed site, gate on"
    if src.category in _PURE_DEFENCE:
        return False, f"category '{src.category}' is pure defence, gate off"
    return True, f"category '{src.category}' is a mixed publisher, gate on"


# The capture kinds the crawler understands (crawler/models.py: CaptureType). Validated rather than
# passed through — "pdfs" instead of "pdf" would silently produce jobs that never fetch attachments,
# and nothing downstream would complain.
_CAPTURE_KINDS = ("html", "text", "images", "screenshot", "pdf", "media", "js")


@router.get("/jobs/capture-options")
def capture_options() -> dict:
    """What can be captured, and what each category captures BY DEFAULT.

    Served rather than hardcoded in the page so the Job Generator shows the real default. Every
    type now includes pdf — see jobgen._CAPTURE for why: the same sites captured brochures under
    one category name and silently did not under another.
    """
    return {
        "kinds": list(_CAPTURE_KINDS),
        # None for "all": it fans out across categories, so there is no single default to show.
        "defaults": {k: (None if cats is None else jobgen._CAPTURE.get(jt, ["html", "text"]))
                     for k, (cats, jt) in _UI_CATEGORIES.items()},
    }


class GenerateByCategory(BaseModel):
    category: str = "all"
    limit: int = 0                    # 0 = every matching source
    max_depth: int | None = None      # operator override; default is the spreadsheet's, else -1
    only_uncrawled: bool = False
    capture: list[str] | None = None  # operator override; empty/None = each job_type's default
    keyword: bool | None = None       # operator override for the keyword gate; None = decide per site
    require_gate: str | None = None   # named topical gate applied as require_keywords, e.g. "energy"


@router.post("/jobs/generate-by-category")
def generate_by_category(body: GenerateByCategory, db: Session = Depends(get_db)) -> dict:
    """Generate one full-site crawl job per catalogued source in the chosen category.

    Every job carries `_why`: which value came from the spreadsheet and which from a default. The
    operator asked for keyword-gating to be automatic, and an automatic decision nobody can inspect
    is the one that quietly ruins a crawl.
    """
    key = (body.category or "all").strip().lower().replace(" ", "_").replace("-", "_")
    if key not in _UI_CATEGORIES:
        raise HTTPException(422, f"category must be one of {sorted(_UI_CATEGORIES)}")
    cats, job_type = _UI_CATEGORIES[key]

    # Named topical gate (e.g. "energy" = diesel gensets for the KOEL client): applied as each job's
    # require_keywords, which REPLACES the defence corpus for that job (crawler/gate.py). A KOEL crawl
    # then keeps only genset pages without dragging in the defence watchlist, and vice-versa.
    from . import energy_gate
    require_kw = None
    rg = (body.require_gate or "").strip().lower()
    if rg == "energy":
        require_kw = energy_gate.ENERGY_KEYWORDS
    elif rg:
        raise HTTPException(422, f"unknown require_gate {rg!r}; valid: 'energy'")

    override_capture = [c.strip().lower() for c in (body.capture or []) if c and c.strip()]
    bad = [c for c in override_capture if c not in _CAPTURE_KINDS]
    if bad:
        raise HTTPException(422, f"unknown capture kind(s) {bad}; valid: {list(_CAPTURE_KINDS)}")

    extra = {r.domain: (r.params or {}) for r in db.scalars(select(SourceRow)).all()}
    pols = {p.domain: p for p in db.scalars(select(SiteSchedule)).all()}
    srcs = [s for s in db.scalars(select(Source).order_by(Source.domain)).all()
            if cats is None or s.category in cats]
    if body.only_uncrawled:
        crawled = _crawled_domains(db)
        srcs = [s for s in srcs if s.domain not in crawled]

    jobs = []
    for s in srcs:
        pol = pols.get(s.domain)
        params = extra.get(s.domain, {})
        auto_gate, why_gate = _keyword_gate(s, pol)
        gate = auto_gate if body.keyword is None else bool(body.keyword)
        if body.keyword is not None:
            why_gate = (f"operator set the gate {'on' if gate else 'off'} for every site "
                        f"(automatic read was {'on' if auto_gate else 'off'}: {why_gate})")
        if require_kw:
            why_gate = (f"energy gate ({energy_gate.ENERGY_GATE_LABEL}) — keep only diesel-genset "
                        f"pages via require_keywords ({len(require_kw)} terms); replaces the corpus")
        # The page/depth/freshness budget deliberately follows the AUTOMATIC read, not the override.
        # Those two decisions only look like one: "pure defence" earns 100k pages BECAUSE the whole
        # site is on-topic, and "mixed publisher" earns 4k + a freshness window BECAUSE it is not.
        # Tying the budget to the checkbox would mean ticking "capture everything" on a general news
        # site silently authorised a 100,000-page crawl of it.
        dflt = _MIXED_DEFAULTS if auto_gate else _PURE_DEFAULTS
        jt = job_type if cats is not None else _CAT_TO_JOBTYPE.get(s.category, "news")

        # `is not None`, NOT `or`: a spreadsheet max_pages of 0 is falsy, and `or` silently turned
        # "crawl nothing from this site" into the 100,000-page pure-defence default. importer.py is
        # careful to distinguish absent from present-and-zero; throwing that away here wasted it.
        mp = params["max_pages"] if params.get("max_pages") is not None else dflt["max_pages"]
        md = body.max_depth if body.max_depth is not None else (
            params.get("max_depth") if params.get("max_depth") is not None else -1)
        seeds = s.seed_urls or [f"https://{s.domain}/"]
        job = {
            "job_id": f"cat_{key}_{s.source_id}".lower(),
            "job_type": jt,
            "seed_urls": seeds,
            # NEVER a per-URL keyword list: the crawler's gate evaluates against the GLOBAL corpus,
            # so the `keyword` bool is the entire control. Populating `keywords` here would imply a
            # per-site corpus that does not exist.
            "keywords": [],
            "keyword": gate,
            # The spreadsheet's answer wins when it gave one. False (not None) otherwise: the
            # crawler escalates to a browser on its own when a page needs it, so defaulting to
            # true would pay browser cost on every static site.
            "render_js": bool(params.get("render_js")) if params.get("render_js") is not None else False,
            "max_pages": mp,
            "max_depth": md,
            "same_domain_only": True,
            "freshness_days": dflt["freshness_days"],
            "capture": list(override_capture) if override_capture
                       else jobgen._CAPTURE.get(jt, ["html", "text"]),
            "source_id": s.source_id, "source_tier": s.tier, "source_type": s.category,
        }
        if jt == "tender":
            job["max_pdfs_per_page"] = 0     # never truncate a tender listing's RFP table
        if require_kw:
            # Keeps a page iff it names a genset term; corpus hits are still recorded when present.
            job["require_keywords"] = list(require_kw)
        jobs.append({
            "domain": s.domain, "category": s.category,
            "category_label": ui_category(s.category), "job": job,
            "frequency": params.get("frequency") or "tiered",
            "_why": {
                "keyword": why_gate,
                "max_pages": "spreadsheet" if params.get("max_pages") else
                             f"default ({'mixed' if auto_gate else 'pure'})",
                "max_depth": "operator override" if body.max_depth is not None else (
                    "spreadsheet" if params.get("max_depth") is not None else "default (-1 unlimited)"),
                # seed_urls can come from a seed file as well as an import, so this says where the
                # URL came from rather than claiming a spreadsheet the operator may never have uploaded
                "seed": "catalog seed_urls" if s.seed_urls else "domain root",
                "capture": "operator override" if override_capture else f"default for {jt} jobs",
                "render_js": "spreadsheet" if params.get("render_js") is not None
                             else "default (crawler escalates when a page needs a browser)",
            },
        })
        if body.limit and len(jobs) >= body.limit:
            break
    return {"category": key, "count": len(jobs),
            "eligible_sources": len(srcs), "jobs": jobs}


# ══ Scheduler tab ═════════════════════════════════════════════════════════════

class SchedulePatch(BaseModel):
    frequency: str | None = None
    enabled: bool | None = None
    max_pages: int | None = None
    max_depth: int | None = None


@router.patch("/schedule/{sid}")
def patch_schedule(sid: int, body: SchedulePatch, db: Session = Depends(get_db)) -> dict:
    """Change ONE site's cadence from the Scheduler tab.

    Changing frequency recomputes next_run from NOW rather than preserving the old phase: an
    operator moving a site from monthly to daily means "start crawling it daily", not "in 26 days".
    """
    sj = db.get(ScheduledJob, sid)
    if not sj:
        raise HTTPException(404, "no such schedule")
    changed = []
    if body.frequency is not None:
        if body.frequency not in _FREQS:
            raise HTTPException(422, f"frequency must be one of {sorted(_FREQS)}")
        sj.frequency = body.frequency
        pol = scheduler.policy_for(db, sj)
        now = scheduler.utcnow()
        sj.next_run = None if body.frequency == "once" else scheduler.compute_next_run(
            body.frequency, prev=None, now=now, policy=pol)
        changed.append(f"frequency={body.frequency}")
    if body.enabled is not None:
        sj.enabled = body.enabled
        changed.append("enabled" if body.enabled else "paused")
    if body.max_pages is not None or body.max_depth is not None:
        job = dict(sj.job)
        if body.max_pages is not None:
            job["max_pages"] = body.max_pages
            changed.append(f"max_pages={body.max_pages}")
        if body.max_depth is not None:
            job["max_depth"] = body.max_depth
            changed.append(f"max_depth={body.max_depth}")
        sj.job = job                       # reassign: SQLAlchemy does not track in-place JSON edits
    scheduler.log_event(db, "rescheduled", sid, sj.job.get("job_id"), detail=", ".join(changed))
    db.commit()
    db.refresh(sj)
    return _sched_dto(sj)


@router.post("/schedule/derive-frequency")
def derive_frequency(apply: bool = True, db: Session = Depends(get_db)) -> dict:
    """The "Get frequency" button: read each site's MEASURED change rate and put it on the cadence
    that rate implies (daily / weekly / monthly).

    Every row reports `basis`, because the honest answer for a site crawled once is "not enough data
    yet — this is a category guess". Presenting a bootstrap guess as a measurement is how an operator
    ends up trusting a cadence that was never derived from anything.
    """
    now = scheduler.utcnow()
    # Only on apply. ensure_site_schedules() COMMITS, so calling it on a dry run wrote new policy
    # rows to disk while the UI said "preview only — nothing was saved". A preview that saves is
    # worse than no preview: it is the one operation the operator trusted not to touch anything.
    created = tiering.ensure_site_schedules(db) if apply else 0
    by_sid = {}
    for sj in db.scalars(select(ScheduledJob)).all():
        by_sid.setdefault((sj.job or {}).get("source_id"), []).append(sj)

    rows, applied = [], 0
    for pol in db.scalars(select(SiteSchedule).order_by(SiteSchedule.domain)).all():
        before = pol.tier
        if apply:
            tiering.advance_phase(db, pol, now)
        else:
            src = db.get(Source, pol.domain)
            pol_tier, reason = tiering.retier(db, pol, src, now)
            pol.tier_reason = reason        # advisory only; tier itself untouched on a dry run
            pol.tier = before
        n = int(pol.measurements or 0)
        basis = ("measured" if pol.tier_reason == "measured" else
                 "mission floor" if pol.tier_reason == "floor" else
                 "cost cap (sentinel probe)" if pol.tier_reason == "cost_cap" else
                 "no change signal in 3 runs" if pol.tier_reason == "no_signal" else
                 f"category guess — only {n} measurement(s), needs {tiering.PROBE_MIN_MEASUREMENTS}")
        jobs = by_sid.get(pol.source_id, [])
        if apply:
            for sj in jobs:
                # `tiered` IS the derived cadence: the policy's tier + assigned day drive next_run,
                # so pinning a literal "weekly" here would freeze the site and stop it re-tiering.
                sj.frequency = "tiered"
                sj.next_run = tiering.next_run_for_policy(pol, sj.next_run, now)
            applied += len(jobs)
        rows.append({
            "domain": pol.domain, "source_id": pol.source_id,
            "was": before, "now": pol.tier, "changed": before != pol.tier,
            "phase": pol.phase, "reason": pol.tier_reason, "basis": basis,
            "measurements": n,
            "lambda_per_day": round(float(pol.lambda_ewma), 3) if pol.lambda_ewma is not None else None,
            "scheduled_jobs": len(jobs),
            "next_run": _iso(tiering.next_run_for_policy(pol, None, now)),
        })
    if apply:
        db.commit()
        scheduler.log_event(db, "rescheduled", None, None,
                            detail=f"derive-frequency: {sum(1 for r in rows if r['changed'])} "
                                   f"tier change(s) across {applied} job(s)")
        db.commit()
    else:
        db.rollback()
    # A dry run evaluates only sites that already HAVE a policy, so say how many it could not see
    # rather than letting the operator read a short list as "these are all my sites".
    covered = {r["domain"] for r in rows}
    unseen = sum(1 for s in db.scalars(select(Source)).all() if s.domain not in covered)
    return {"applied": apply, "sites": len(rows),
            "changed": sum(1 for r in rows if r["changed"]),
            "measured": sum(1 for r in rows if r["reason"] == "measured"),
            "jobs_updated": applied, "policies_created": created,
            "sites_without_policy": unseen, "rows": rows}


# ══ Logs tab ══════════════════════════════════════════════════════════════════
# Three services each hold part of the truth about a completed crawl, and the old panel showed one
# of them while claiming to show all — which is why "jobs and logs disagreed". Each row below states
# where its numbers came from:
#   crawler /v1/batch/report  — per-URL outcome and the ONLY source of "why did this URL not resolve"
#   crawler /v1/issues        — DURABLE unresolved log; keeps "complete" true after the report is gone
#   ingest  /api/live/sources — pages that actually reached Postgres + MinIO
#   this DB (SiteMeasurement) — scheduled-run history, and the only source of re-crawl deltas


def _row_unresolved(has_rep: bool, rep_retryable: int, rep_terminal: int,
                    stored_pages: int, issues_reachable: bool, durable_count: int
                    ) -> tuple[int | None, bool]:
    """(unresolved, used_durable) for one host's Logs row.

    The crawler's in-memory batch report is the richest source but it evaporates when the next run
    replaces it — which is why a finished host's green "complete" tag vanished on the next refresh.
    When the report is gone we fall back to the PERSISTENT issues log: pages in the store plus zero
    logged issues is a real, refresh-proof "complete"; N issues is "partial". Only when the issues
    log itself is unreachable do we return None (unknown) — never a fabricated 0 that would let an
    unmeasured host masquerade as cleanly crawled.
    """
    if has_rep:
        return rep_retryable + rep_terminal, False
    if issues_reachable and stored_pages > 0:
        return durable_count, True
    return None, False


def _site_counts(rows: list[dict]) -> dict:
    """The one rule. `complete` requires a crawl REPORT saying zero unresolved — not merely the
    absence of a complaint — and excludes anything the crawler says is in flight right now."""
    live: set[str] = set()
    try:
        st = crawler_batch_status()
        if st.get("running"):
            live = {str(h).lower().removeprefix("www.") for h in (st.get("current_domains") or [])}
    except Exception:  # noqa: BLE001 — an unknown live set must not invent completions
        live = set()
    complete = crawling = unresolved = partial = unverified = 0
    for r in rows:
        host = str(r.get("host") or "").lower().removeprefix("www.")
        if host in live:
            crawling += 1
        elif r.get("unresolved") is None:
            unverified += 1                    # pages exist, but nothing verified the crawl finished
        elif r.get("unresolved"):
            unresolved += 1
        # AN EXHAUSTED FRONTIER IS PART OF BEING FINISHED. `unresolved == 0` only means nothing
        # FAILED in the run the crawler still holds in memory — it says nothing about URLs the
        # crawl discovered and never reached. Counting those as complete claimed 564 of 621 sites
        # were done while 103,964 URLs sat queued across 403 of them.
        elif r.get("pending"):
            partial += 1
        elif r.get("stored_pages"):
            complete += 1
        else:
            unverified += 1                    # a clean report with nothing stored is not a success
    return {"total": len(rows), "complete": complete, "crawling": crawling,
            "unresolved": unresolved, "partial": partial, "unverified": unverified}


def _shift_iso(iso: str | None, seconds: float) -> str | None:
    """`iso` moved by `seconds`, or None if there was no timestamp to move.

    None in, None out — deliberately. A missing run time must not become "now minus the duration",
    which would invent a start for a run nobody recorded.
    """
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


@router.get("/logs/sites")
def logs_sites(limit: int = 300, db: Session = Depends(get_db)) -> dict:
    """One row per site that has actually been crawled, with fetched / stored / error / dedupe tags."""
    report: dict = {}
    report_err = None
    # FLEET-WIDE. This called batch_report() with no base, so it only ever saw INSTANCE 0's run:
    # eleven twelfths of the crawl had no per-URL outcomes, and every host routed elsewhere showed
    # "stored, not verified" however cleanly it had finished. urls=0 keeps it to the cheap per-site
    # table — the URL lists are fetched per host when a row is expanded.
    try:
        rows_r = crawler_client.fanout(crawler_client.batch_report, urls=0)
        if not any(r.get("ok") for r in rows_r):
            report_err = str((rows_r[0].get("error") if rows_r else "no instance answered"))[:200]
        for r in rows_r:
            if not r.get("ok"):
                continue
            for s in ((r.get("data") or {}).get("sites") or []):
                report[str(s.get("host", "")).lower().removeprefix("www.")] = s
    except Exception as e:  # noqa: BLE001
        report_err = str(e)[:200]

    # HOW MUCH WORK IS STILL QUEUED for each host. Without this the table could only report what
    # FAILED, never what was never reached — and "nothing failed" was being read as "finished".
    # Measured: 332 of 564 hosts called complete still held pending URLs, sigsauer.com among them
    # with 3,839 queued behind a green "complete" badge.
    pending: dict[str, int] = {}
    try:
        for r in crawler_client.fanout(crawler_client.resumable, limit=5000):
            if not r.get("ok"):
                continue
            for j in ((r.get("data") or {}).get("jobs") or []):
                h = str(j.get("host") or "").lower().removeprefix("www.")
                if h:
                    pending[h] = pending.get(h, 0) + int(j.get("pending") or 0)
    except Exception:  # noqa: BLE001 — an unreadable frontier must not invent completions
        pending = {}

    stored: dict[str, dict] = {}
    stored_err = None
    try:
        for r in _ingest_get("/api/live/sources?limit=5000"):
            stored[str(r.get("source_id") or "").lower().removeprefix("www.")] = r
    except Exception as e:  # noqa: BLE001
        stored_err = str(e)[:200]

    # Durable unresolved counts from the PERSISTENT issues log, so a finished host still reads
    # "complete" after its in-memory report is gone (the "complete vanishes on refresh" bug). Keyed
    # like every other host here: lowercased, www. stripped. issues_reachable stays False if the
    # crawler is down, so we return unknown (?) rather than a fabricated clean 0.
    durable_unres: dict[str, int] = {}
    issues_reachable = False
    try:
        by_host = (crawler_client.issues(limit=1).get("summary", {}) or {}).get("by_host", {}) or {}
        for hk, n in by_host.items():
            # ACCUMULATE. The crawler reports www.bhel.com and bhel.com as separate hosts; both
            # normalise to the same key here, so a plain assignment made the second silently
            # replace the first. Measured on 2026-07-28: bhel.com 2,634 + www.bhel.com 1,812, and
            # this panel showed 1,812 — under-reporting the site's unresolved count by 59%.
            k = str(hk).lower().removeprefix("www.")
            durable_unres[k] = durable_unres.get(k, 0) + int(n)
        issues_reachable = True
    except Exception:  # noqa: BLE001
        pass

    cat_of = {s.domain.lower(): s for s in db.scalars(select(Source)).all()}
    pols = {p.domain.lower(): p for p in db.scalars(select(SiteSchedule)).all()}

    # Per-domain rollup of this service's own measurement history.
    runs: dict[str, dict] = {}
    for m in db.scalars(select(SiteMeasurement).order_by(SiteMeasurement.at)).all():
        d = str(m.domain or "").lower()
        r = runs.setdefault(d, {"runs": 0, "kept": 0, "fetched": 0, "unchanged": 0,
                                "duplicate": 0, "gate_dropped": 0, "errors": 0,
                                "last_at": None, "last_duration_s": 0.0})
        r["runs"] += 1
        r["kept"] += m.kept
        r["fetched"] += m.fetched
        r["unchanged"] += m.skipped_unchanged + m.not_modified_304
        r["duplicate"] += m.skipped_duplicate
        r["gate_dropped"] += m.dropped_by_gate
        r["errors"] += m.errors
        r["last_at"] = _iso(m.at)
        r["last_duration_s"] = round(float(m.duration_s or 0.0), 1)

    hosts = set(report) | set(stored) | set(runs)
    rows = []
    for h in hosts:
        rep_s, st, rn = report.get(h, {}), stored.get(h, {}), runs.get(h, {})
        src, pol = cat_of.get(h), pols.get(h)
        # NULL, not 0, when a source is silent. The crawler's batch report only covers runs still in
        # memory, so most historical hosts have no per-URL record at all — and rendering that as
        # "fetched 0, 0 unresolved" reads as "crawled cleanly" when the truth is "we did not measure
        # this run". Unknown has to look different from zero or the whole table lies quietly.
        has_rep, has_hist = bool(rep_s), bool(rn)
        stored_pages = int(st.get("pages") or 0)
        unresolved, used_durable = _row_unresolved(
            has_rep, int(rep_s.get("retryable", 0)), int(rep_s.get("terminal", 0)),
            stored_pages, issues_reachable, durable_unres.get(h, 0))
        rows.append({
            "host": h,
            "category": src.category if src else None,
            "category_label": ui_category(src.category if src else None),
            "tier_cadence": pol.tier if pol else None,
            "phase": pol.phase if pol else None,
            # what the store actually holds — the only number that proves the crawl produced data
            "stored_pages": stored_pages,
            "stored_bytes": int(st.get("html_bytes") or 0),
            "last_stored_at": st.get("last_at"),
            # what the crawler saw on its most recent run(s)
            "fetched": (int(rep_s.get("kept", 0)) + unresolved) if has_rep else (
                rn.get("fetched") if has_hist else None),
            "resolved": int(rep_s.get("kept", 0)) if has_rep else None,
            "retryable": int(rep_s.get("retryable", 0)) if has_rep else None,
            "terminal": int(rep_s.get("terminal", 0)) if has_rep else None,
            "unresolved": unresolved,
            "reasons": rep_s.get("reasons", {}) or {},
            # dedupe / gate counters, which only the scheduled path records
            "runs": rn.get("runs", 0),
            "unchanged": rn.get("unchanged") if has_hist else None,
            "duplicate": rn.get("duplicate") if has_hist else None,
            "gate_dropped": rn.get("gate_dropped") if has_hist else None,
            "errors": rn.get("errors") if has_hist else None,
            # STARTED and FINISHED as separate fields, not a start plus a duration the reader has
            # to add up. `last_at` is when the run was recorded (its end), so the start is derived
            # backwards from the measured duration — and both are null together when no run has
            # been recorded, never one without the other.
            "last_run_at": rn.get("last_at"),
            "last_duration_s": rn.get("last_duration_s"),
            "started_at": _shift_iso(rn.get("last_at"), -float(rn.get("last_duration_s") or 0.0)),
            "finished_at": rn.get("last_at"),
            # URLs this host has discovered but not yet crawled. 0 (not null) when the crawler
            # answered and holds no frontier for it — that IS "nothing left to do".
            "pending": pending.get(h, 0) if pending or not report_err else None,
            # Which of the three services contributed to this row. The UI shows it, because "we have
            # pages but no crawl record" is a genuinely different state from "the crawl was clean".
            "sources": [k for k, v in (("crawler", rep_s), ("store", st), ("history", rn)) if v]
                       + (["issues"] if used_durable else []),
        })
    rows.sort(key=lambda r: -r["stored_pages"])
    return {"count": len(rows), "rows": rows[:max(1, min(2000, limit))],
            # ONE definition of "complete", computed here and reported as a number, so the Logs
            # table and the Watchfloor tile cannot drift apart. They did: the tile counted a host as
            # finished if it merely had no entry in the failure log, and did not exclude hosts still
            # being crawled — so during a 485-site batch the tile read 618 while the table, applying
            # the honest rule, read 173. Absence of recorded problems is not proof of completion,
            # and a site mid-crawl is not finished however good its counts look so far.
            "counts": _site_counts(rows),
            "crawler_reachable": report_err is None, "crawler_error": report_err,
            "store_reachable": stored_err is None, "store_error": stored_err}


@router.get("/logs/sites/counts")
def logs_sites_counts(db: Session = Depends(get_db)) -> dict:
    """Just the completion counts — the Watchfloor tile's source.

    Separate from /logs/sites because that builds 600+ rows and takes seconds; this is the same
    computation cached, so the tile and the table are the same number by construction rather than by
    two implementations agreeing.
    """
    return _rollup("sites_counts", lambda: _logs_sites_counts_live(db))


def _logs_sites_counts_live(db: Session) -> dict:
    try:
        d = logs_sites(limit=5000, db=db)
        out = dict(d.get("counts") or {})
        out["reachable"] = bool(d.get("store_reachable"))
        # THE CATALOGUE IS THE REAL DENOMINATOR, and "has pages" must be asked of the CORPUS.
        #
        # `total` above counts sites that appear in the CRAWL LOG. The comment here used to claim
        # that was the same as "has stored at least one page" — it is not, and the gap is large: a
        # site the crawler fetched and got nothing usable from (everything dropped by the keep gate,
        # or every URL an error) appears in the log with kept=0 and no rows in `documents`. Measured
        # 2026-07-31: 665 sites in the log, **598** with pages, 690 catalogued. So the tile read
        # "665 have pages · 25 have never returned a page" when the truth was 598 and 92 — it
        # understated the never-crawled set by a factor of nearly four, which is precisely the
        # number an operator uses to decide whether the catalogue is done.
        #
        # `/api/live/sources` is the store's own per-source page count, the same source of truth the
        # idle-instance dispatch uses. If it cannot be reached the field is left None rather than
        # falling back to the crawl-log number: showing a wrong count confidently is the failure
        # this dashboard's one rule exists to prevent.
        try:
            out["catalogued"] = db.query(Source).count()
        except Exception:  # noqa: BLE001 — completion counts must survive a catalogue read failing
            out["catalogued"] = None
        out["log_sites"] = out.get("total")          # kept, correctly named: sites the crawler touched
        try:
            rows = _ingest_get("/api/live/sources?limit=100000", timeout=120.0) or []
            out["with_pages"] = sum(1 for r in rows if (r.get("pages") or 0) > 0)
        except Exception:  # noqa: BLE001
            out["with_pages"] = None
        out["never_crawled"] = (max(0, out["catalogued"] - out["with_pages"])
                                if (out["catalogued"] is not None and out["with_pages"] is not None)
                                else None)
        return out
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e)[:200]}


@router.get("/logs/unresolved")
def logs_unresolved(host: str = "", limit: int = 500) -> dict:
    """The URLs a crawl could NOT resolve, each with the crawler's own reason code.

    This is the production lesson that matters most from the scaling literature: a failed URL is
    only actionable if you kept WHY it failed. `/api/crawler/batch` can re-dispatch these.
    """
    want = host.lower().removeprefix("www.")
    rows: list[dict] = []
    seen: set[str] = set()

    # LIVE issue log first. batch_report() is only written when a batch FINISHES, so during a long
    # crawl it is empty and this tab used to show nothing at exactly the moment an operator is
    # looking. The issue log is written as pages give up, persists across restarts, and carries the
    # attempt count and the specific obstacle — so these rows lead.
    issues_err = None
    try:
        iss = crawler_client.issues(limit=limit, host=host)
        for r in iss.get("issues", []):
            h = str(r.get("host", "")).lower()
            if want and h.removeprefix("www.") != want:
                continue
            url = r.get("url")
            if url in seen:
                continue
            seen.add(url)
            rows.append({"host": h, "url": url, "status": r.get("status"),
                         "reason": r.get("cause"), "bucket": "unresolved",
                         "attempts": r.get("attempts"), "evidence": r.get("evidence", ""),
                         "action": r.get("action", ""), "last_seen": r.get("last_seen", ""),
                         "source": "issue_log"})
    except Exception as e:  # noqa: BLE001
        issues_err = str(e)[:200]

    report_err = None
    try:
        rep = crawler_client.batch_report()
    except Exception as e:  # noqa: BLE001
        rep, report_err = {}, str(e)[:200]
    for s in rep.get("sites", []):
        h = str(s.get("host", "")).lower()
        if want and h.removeprefix("www.") != want:
            continue
        for u in s.get("urls", []):
            if u.get("bucket") == "kept" or u.get("url") in seen:
                continue
            seen.add(u.get("url"))
            rows.append({"host": h, "url": u.get("url"), "status": u.get("status"),
                         "reason": u.get("reason"), "bucket": u.get("bucket"),
                         "attempts": None, "evidence": "", "action": "",
                         "last_seen": "", "source": "batch_report"})
    if issues_err and report_err:
        return {"reachable": False, "error": f"issues: {issues_err}; report: {report_err}",
                "rows": []}
    # Stamp every row with its FAMILY before it goes out, so the table can be grouped, coloured and
    # filtered by the kind of problem rather than by a reason code the reader has to decode. The raw
    # code stays on the row — it is the precise thing, and the family is the readable thing.
    for r in rows:
        r["family"], r["recoverable"] = errorclass.classify(r.get("reason"), r.get("status"))
        r["family_label"] = errorclass.FAMILY_LABEL[r["family"]]
    reasons = Counter(r["reason"] for r in rows)
    return {"reachable": True, "count": len(rows), "rows": rows[:max(1, min(5000, limit))],
            "by_reason": dict(reasons.most_common()),
            "by_family": errorclass.summarise(rows),
            "unresolved_live": sum(1 for r in rows if r["source"] == "issue_log"),
            "issues_error": issues_err, "report_error": report_err,
            "run_ids": rep.get("run_ids", [])}


@router.get("/logs/runs")
def logs_runs(limit: int = 200, domain: str = "", db: Session = Depends(get_db)) -> dict:
    """Successful runs, newest first, each tagged with its DELTA against the previous run of the
    same site: how long the re-crawl took and whether it actually brought back new data.

    Built from SiteMeasurement, which is written once per completed non-baseline run — so a row here
    means the cycle genuinely closed, not merely that a job was dispatched.
    """
    q = select(SiteMeasurement).order_by(SiteMeasurement.at.desc())
    if domain:
        q = q.where(SiteMeasurement.domain == domain.lower())
    ms = list(db.scalars(q.limit(max(1, min(2000, limit)))).all())

    # previous run per domain, for the delta — walk oldest→newest over the window we fetched
    prev: dict[str, SiteMeasurement] = {}
    deltas: dict[int, dict] = {}
    for m in sorted(ms, key=lambda x: x.at):
        p = prev.get(m.domain)
        if p is not None:
            deltas[m.id] = {
                "since_prev_days": round(float(m.interval_days or 0.0), 2),
                "duration_delta_s": round(float(m.duration_s or 0) - float(p.duration_s or 0), 1),
                "kept_delta": int(m.kept) - int(p.kept),
                "prev_at": _iso(p.at), "prev_kept": int(p.kept),
                "prev_duration_s": round(float(p.duration_s or 0.0), 1),
            }
        prev[m.domain] = m

    rows = []
    for m in ms:
        checked = int(m.checked or 0)
        rows.append({
            "id": m.id, "domain": m.domain, "at": _iso(m.at),
            "tier_at_run": m.tier_at_run, "sentinel": bool(m.is_sentinel),
            "fetched": m.fetched, "kept": m.kept, "checked": checked,
            "unchanged": m.skipped_unchanged + m.not_modified_304,
            "duplicate": m.skipped_duplicate, "gate_dropped": m.dropped_by_gate,
            "errors": m.errors, "recovered": m.recovered,
            "duration_s": round(float(m.duration_s or 0.0), 1),
            "interval_days": round(float(m.interval_days or 0.0), 2),
            "change_rate": round(float(m.change_rate or 0.0), 3),
            "changes_per_day": round(float(m.changes_per_day or 0.0), 3),
            # The badge the operator reads at a glance. `kept>0` on a re-crawl is genuinely new or
            # changed content: the crawler skips re-ingesting content-hash-identical pages.
            "outcome": "new data" if m.kept > 0 else ("no change" if checked else "nothing checked"),
            "delta": deltas.get(m.id),
        })
    return {"count": len(rows), "rows": rows}


# ── Digital twin ─────────────────────────────────────────────────────────────
# A live mirror of the backend, plus a sandbox to experiment against it. The mirror reads the
# same services the rest of this file already reads; the sandbox reads NOTHING. See twin.py.

def _twin_snapshot(db: Session | None = None) -> dict:
    """Gather the four live sources and assemble the twin.

    Takes `db` and ignores it. Every cached handler is invoked as `fn(db)` by `cache.serve`, so
    the parameter is required by that contract even though the twin reads HTTP services rather
    than the orchestrator's own tables. Omitting it raised TypeError on the first live request
    while every unit test passed, because the tests called `build_snapshot` directly and never
    went through the cache — see `test_twin_snapshot_route_matches_cache_contract`.

    Each source is fetched independently and a failure is passed through as `None` rather than
    raised: a twin that shows a dead CamoFox and a live fleet is useful, and one that 500s because
    one probe timed out is not. This is the whole reason build_snapshot takes optionals.
    """
    def _try(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    metrics = _try(lambda: _merge_metrics(crawler_client.fanout(crawler_client.get_metrics)))
    tiers = _try(crawler_client.get_tiers)
    camofox = _try(crawler_camofox)
    batch = _try(crawler_batch_status)
    return twin.build_snapshot(metrics, tiers, camofox, batch)


@router.get("/twin/snapshot")
def twin_snapshot(fresh: int = 0) -> dict:
    """The mirror. Cached briefly because the panel polls it every 5s and the fan-out across 12
    instances is not free — the TTL is well under the poll interval, so a stale read is measured
    in seconds and is labelled with its age either way."""
    # Findings are computed on the way OUT, not inside the cached builder, because the freshness
    # of the reading is itself something the twin diagnoses — and `_cache` is only attached by
    # `cache.serve` once the value is being served. A shallow copy keeps them off the cached
    # object, so a stale-cache finding can never outlive the staleness that produced it.
    snap = dict(cache.serve("twin/snapshot", _twin_snapshot, 4.0, force=bool(fresh)))
    try:
        snap["findings"] = twin.diagnose(snap)
    except Exception as exc:  # noqa: BLE001
        # Diagnosis is a convenience over the mirror. It must never be able to take the mirror
        # down with it — a broken rule costs its findings, not the whole panel.
        snap["findings"] = []
        snap["findings_error"] = str(exc)[:200]
    return snap


class TwinSimulate(BaseModel):
    instances: float | None = None
    render_share: float | None = None
    host_concurrency: float | None = None
    cpu_cores: float | None = None
    cpu_target: float | None = None
    unique_hosts: float | None = None
    c3_up: bool | None = None
    c4_up: bool | None = None
    render_seconds: float | None = None
    httpx_seconds: float | None = None
    frontier: float | None = None
    deflect_cost_s: float | None = None
    against_live: bool = True


@router.post("/twin/simulate")
def twin_simulate(body: TwinSimulate) -> dict:
    """The sandbox. Runs the capacity model and compares it against the live configuration.

    THIS ENDPOINT CHANGES NOTHING. It opens no DB session, dispatches no job and touches no
    crawler — the only live read is the cached snapshot used to derive the baseline, and that is
    the same read the panel is already doing. `sandbox: true` in the response is the assertion the
    UI surfaces to the operator.
    """
    params = {k: v for k, v in body.model_dump().items()
              if v is not None and k != "against_live"}
    baseline, assumed, calib = None, [], None
    if body.against_live:
        try:
            live = twin.live_params(cache.serve("twin/snapshot", _twin_snapshot, 4.0))
            baseline, assumed = live["params"], live["assumed"]
            calib = live.get("calibration")
            # Anything the operator did not override starts from the live value, so the sandbox
            # opens as a copy of production rather than as a blank form.
            params = {**baseline, **params}
        except Exception:  # noqa: BLE001
            pass
    out = twin.simulate(params, baseline=baseline)
    out["baseline_assumed"] = assumed
    if calib:
        out["calibration"] = calib
    return out


class TwinControl(BaseModel):
    id: str                      # must name a control in twin.CONTROLS — that registry IS the whitelist
    confirm: bool = False        # required for anything not risk:"low"


def _twin_available_routes() -> set[str]:
    """Which control routes this build actually exposes, read off the live router.

    Discovered rather than declared because the data-centre tree runs ahead of the local one — it
    has fleet/maintenance, fleet/prune and fleet/assign; this tree does not. Hardcoding the list
    would offer buttons that 404 on one deployment and hide working controls on the other.
    """
    out: set[str] = set()
    for r in router.routes:
        p = getattr(r, "path", "")
        if p.startswith("/api"):
            out.add(p[len("/api"):])
    return out


@router.get("/twin/controls")
def twin_controls(db: Session = Depends(get_db)) -> dict:
    """What the operator can actually change, and how each sandbox knob maps to a real action."""
    try:
        state = {"master": scheduler.master_enabled(db)}
    except Exception:  # noqa: BLE001
        state = {}
    return {"controls": twin.control_plan(_twin_available_routes(), state),
            "knobs": twin.knob_guidance(),
            "note": "Simulating changes nothing. Only the buttons in this list touch production."}


@router.post("/twin/control")
def twin_control(body: TwinControl, db: Session = Depends(get_db)) -> dict:
    """Apply ONE control to the running system. This is the only twin route that changes anything.

    Three guards, in order:

      1. The id must name a control in `twin.CONTROLS`. That registry is the whitelist, so this
         route cannot be talked into calling an arbitrary endpoint — an unknown id is a 404 before
         anything is dispatched.
      2. Anything above risk "low" requires `confirm: true`. The UI asks; a stray click does not
         stop a crawl.
      3. The handler must exist in THIS build. A control listed for the data-centre tree but absent
         here fails as 501 with the reason, rather than as an AttributeError.

    Handlers are dispatched in-process by NAME rather than over HTTP to ourselves: a self-call from
    a single-worker uvicorn would have to round-trip through the same event loop and the auth
    middleware, and would break the moment DASH_AUTH is turned on.
    """
    c = twin.find_control(body.id)
    if c is None:
        raise HTTPException(404, f"unknown control '{body.id}'")
    if c["risk"] != "low" and not body.confirm:
        raise HTTPException(400, f"'{c['label']}' is risk:{c['risk']} and needs confirm=true")

    # id -> (function name in this module, kwargs). Name-based so a handler missing from this tree
    # degrades to a clear 501 instead of a NameError at import time.
    plan: dict[str, tuple[str, dict]] = {
        "refill_once":     ("crawler_fleet_refill", {}),
        "rebalance":       ("crawler_fleet_rebalance", {"db": db}),
        "stop_crawl":      ("crawler_stop", {"db": db}),
        "master_on":       ("set_master", {"body": MasterToggle(enabled=True), "db": db}),
        "master_off":      ("set_master", {"body": MasterToggle(enabled=False), "db": db}),
        "maintenance_on":  ("crawler_fleet_maintenance", {"on": True, "db": db}),
        "maintenance_off": ("crawler_fleet_maintenance", {"on": False, "db": db}),
    }
    if body.id not in plan:
        raise HTTPException(501, f"control '{body.id}' has no handler wired")
    fname, kwargs = plan[body.id]
    fn = globals().get(fname)
    if fn is None:
        raise HTTPException(501, f"this build has no '{fname}' — the control exists only on the "
                                 "data-centre tree")

    # A control mutates production, so it leaves a trace. Logger is local because api.py sets
    # up no module logger of its own.
    import logging
    _log = logging.getLogger("mallory.twin")
    _log.info("control applied: %s -> %s (risk=%s)", body.id, fname, c["risk"])
    try:
        result = fn(**kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("control %s failed", body.id)
        raise HTTPException(500, f"{c['label']} failed: {exc}") from exc

    # The snapshot is cached for a few seconds; after a control lands that cached copy is stale by
    # definition, so drop it and let the next poll re-read rather than showing the operator the
    # state from BEFORE their own action.
    try:
        cache.clear("twin/snapshot")
    except Exception:  # noqa: BLE001
        pass
    return {"applied": body.id, "label": c["label"], "risk": c["risk"], "result": result}
