"""The campaign launcher: paste a list, get a plan, approve it, run it — in CAMPAIGN mode.

On 2026-09-19 the operator wanted a full-force, date-windowed crawl of 49 competitor sites and had
the list in hand. The dashboard offered no way to do it: the tiering, the two-phase job shape and
the dispatch were all scripted by hand (comp_campaign.py + dc_dispatch.py). This module is that
script made a product, and it reproduces the SAME jobs — pinned by tests/fixtures/
campaign_reference_jobs.json, the 84 jobs that script emitted.

Three things it encodes, each the operator's own decision:

  INTAKE      a URL/domain list plus three options: the date window (default 2026-08-01 -> today),
              the scope (whole site + newsroom, or newsroom only) and PDFs on/off.

  TIERING     every site gets a fetch tier BEFORE anything is dispatched:
                C1  live fetch (the default; JS shells auto-escalate in the engine)
                C2  the Wayback archive — the live host hard-bans us
                C3  browser render — a JS-only shell or a WAF that C1 cannot pass
                C4  the host solver — Imperva/Incapsula or an interactive Cloudflare wall
                FLAG  a geo/Akamai ban: needs residential egress, NOT crawlable from the fleet
              Known verdicts come first (the 2026-09-19 audit, then the crawler's own
              CRAWLER_C4_HOSTS / CRAWLER_C3_HOSTS lists); an unknown site is PROBED with one cheap
              fetch and classified from what came back, and the plan says which it was.

  PHASES      phase 1 — whole-site breadth: render off, keyword off, UNLIMITED (auto-escalates)
              phase 2 — the dated newsroom: render on + click-discovery, news-path scoped,
                        unlimited. The window is enforced DOWNSTREAM from published_at
                        (freshness_days=None): over-capturing older articles is fine,
                        under-capturing is not.
              A C2 site gets one archive job over the window instead (crawler.c2). A C4 site's
              newsroom is fetched through the host solver without CamoFox render, because
              render_js on a walled host ALSO renders and stalls on the Imperva wait-wall.

RUN MODEL   plan -> approve -> run. `build_plan` dispatches nothing. `launch` is the Go button: it
            switches the control plane to campaign mode (policy.py), starts the pipeline, and
            sends each phase to the instances that own the domains — `background: true`, then
            POLL until they are idle — which is how the hand run was proven. Every poll reads
            the gate, so Stop ends the campaign and the fleet returns to a clean normal mode.

Numbers here are ESTIMATES and say so: a site's expected page count is its last measured crawl,
else the pages the corpus already holds, else "not measured". The ETA names the rate it assumed.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
import time
from threading import Thread

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crawler_client, policy, scheduler, shard
from .models import Campaign, SiteMeasurement

log = logging.getLogger("mallory.campaigns")

# ── tiering knowledge ─────────────────────────────────────────────────────────

TIERS: dict[str, str] = {
    "C1": "live fetch",
    "C2": "Wayback archive — the live host hard-bans us",
    "C3": "browser render — JS-only shell or a WAF C1 cannot pass",
    "C4": "host solver — Imperva/Incapsula or an interactive Cloudflare wall",
    "FLAG": "geo/Akamai ban — needs residential egress; not crawlable from the fleet",
}

# The 2026-09-19 competitor audit (comp_campaign.py): coverage-audit + incapsula-wall +
# wayback-recovery findings, plus local BAE/Anduril tests. Verdicts, not guesses.
KNOWN_TIERS: dict[str, str] = {
    "baesystems.com": "C4", "babcockinternational.com": "C4", "oshkoshdefense.com": "C4",
    "sigsauer.com": "C4", "uvisionuav.com": "C4",
    "rafael.co.il": "C3", "iai.co.il": "C3",
    "supacat.com": "C2",
    "northropgrumman.com": "FLAG",
}

# SigSauer: bounded whole-site (operator decision — its 30k+ pending would dominate the fleet).
BOUNDED_WHOLE_SITE: dict[str, int] = {"sigsauer.com": 5000}

NEWS_SCOPE: list[str] = ["/news", "/newsroom", "/press", "/media", "/article", "/story",
                         "/insight", "/press-release", "/press-releases", "/blog"]

DEFAULT_WINDOW_FROM = "2026-08-01"
SCOPES: dict[str, str] = {"whole_site": "whole site + dated newsroom",
                          "news_only": "dated newsroom only"}

# Wall markers, the same vocabulary as crawler/errors.py (kept as a literal because the
# orchestrator does not depend on the crawler package). A page carrying one of these is not the
# site; it is the wall in front of it.
_HOST_SOLVER_MARKERS = ("_incapsula_resource", "incapsula", "imperva", "cf-chl-", "just a moment",
                        "checking your browser before accessing", "px-captcha",
                        "captcha-delivery.com", "/cdn-cgi/challenge-platform/h/")
_GEO_BAN_MARKERS = ("<title>access denied</title>", "edgesuite.net", "errors.edgesuite.net")

# The twin's calibration: ~3.6 pages/s fleet-wide at the live operating point, twelve instances.
ASSUMED_PAGES_PER_SEC_PER_INSTANCE = 0.3


# ── intake ────────────────────────────────────────────────────────────────────

def parse_sites(raw) -> list[dict]:
    """Lines, commas or whitespace; a domain or a URL per entry; '#' comments; deduped by host,
    first spelling wins. A URL is kept as the seed — the operator may have named a section."""
    if isinstance(raw, (list, tuple)):
        text = "\n".join(str(x) for x in raw)
    else:
        text = str(raw or "")
    out: list[dict] = []
    seen: set[str] = set()
    for tok in re.split(r"[\s,;]+", text):
        tok = tok.strip().strip('"').strip("'")
        if not tok or tok.startswith("#"):
            continue
        url = tok if "://" in tok else "https://" + tok
        dom = shard.host_of(url)
        if not dom or "." not in dom or dom in seen:
            continue
        seen.add(dom)
        out.append({"domain": dom, "seed": url if "://" in tok else "https://%s/" % dom})
    return out


def default_options(today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return {"window_from": DEFAULT_WINDOW_FROM, "window_to": today.isoformat(),
            "scope": "whole_site", "pdfs": True}


def normalise_options(opts: dict | None, today: dt.date | None = None) -> dict:
    """Fill defaults and REFUSE nonsense rather than quietly widening a crawl: a bad date or an
    unknown scope is a ValueError, not a fallback."""
    out = default_options(today)
    opts = opts or {}
    for k in ("window_from", "window_to"):
        v = opts.get(k)
        if v:
            try:
                dt.date.fromisoformat(str(v))
            except ValueError:
                raise ValueError("%s must be YYYY-MM-DD, got %r" % (k, v))
            out[k] = str(v)
    if out["window_from"] > out["window_to"]:
        raise ValueError("window_from %s is after window_to %s" % (out["window_from"], out["window_to"]))
    scope = str(opts.get("scope") or out["scope"])
    if scope not in SCOPES:
        raise ValueError("scope must be one of %s" % sorted(SCOPES))
    out["scope"] = scope
    out["pdfs"] = bool(opts.get("pdfs", out["pdfs"]))
    return out


# ── tier detection ────────────────────────────────────────────────────────────

def http_probe(domain: str, timeout_s: float = 8.0) -> dict:
    """ONE cheap live fetch of the site root. Real network — injectable everywhere it is used."""
    import httpx
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36")
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": ua, "Accept": "text/html,*/*"}) as c:
            r = c.get("https://%s/" % domain)
            return {"status": r.status_code, "html": r.text[:200_000], "error": None,
                    "final_url": str(r.url)}
    except Exception as e:  # noqa: BLE001 — the verdict is the point, not the exception
        return {"status": None, "html": "", "error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                "final_url": None}


def _visible_text_len(html: str) -> int:
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", body).strip())


def classify_probe(res: dict) -> tuple[str, str]:
    """(tier, why) from one fetch. Mirrors the ladder the engine climbs at crawl time, read off a
    single response: a wall marker is C4, an Akamai/geo block is FLAG, an unreachable host is C2,
    a block status or a text-poor JS shell is C3, and a page with real text is C1."""
    err = res.get("error")
    if err:
        return "C2", "live fetch failed (%s) — archive tier" % err
    low = (res.get("html") or "").lower()
    status = int(res.get("status") or 0)
    if any(m in low for m in _HOST_SOLVER_MARKERS):
        return "C4", "wall marker in the response (HTTP %s)" % status
    if any(m in low for m in _GEO_BAN_MARKERS) and status in (403, 200):
        return "FLAG", "Akamai/geo block page (HTTP %s)" % status
    if status in (401, 403, 429, 503):
        return "C3", "HTTP %s with no challenge script — browser first" % status
    if status >= 400:
        return "C2", "HTTP %s — archive tier" % status
    text = _visible_text_len(res.get("html") or "")
    scripts = low.count("<script")
    if text < 400 and scripts >= 3:
        return "C3", "JS shell: %d chars of visible text behind %d scripts" % (text, scripts)
    return "C1", "HTTP %s, %d chars of visible text" % (status, text)


def _env_hosts(var: str) -> set[str]:
    return {h.strip().lower().removeprefix("www.")
            for h in os.environ.get(var, "").replace(";", ",").split(",") if h.strip()}


def detect_tier(domain: str, probe=None) -> tuple[str, str, str]:
    """(tier, source, note). Known verdicts first, the crawler's own straight-to-tier lists
    second, and only then a probe — so a site the operator has already classified is never
    re-probed into a different answer by a lucky fetch. `probe` None = http_probe, resolved at
    CALL time so a test can replace the module's prober."""
    probe = probe or http_probe
    dom = domain.lower().removeprefix("www.")
    if dom in KNOWN_TIERS:
        return KNOWN_TIERS[dom], "audit", "classified by the 2026-09-19 competitor audit"
    if dom in _env_hosts("CRAWLER_C4_HOSTS"):
        return "C4", "env", "listed in CRAWLER_C4_HOSTS"
    if dom in _env_hosts("CRAWLER_C3_HOSTS"):
        return "C3", "env", "listed in CRAWLER_C3_HOSTS"
    tier, why = classify_probe(probe(dom))
    return tier, "probe", why


# ── the jobs ──────────────────────────────────────────────────────────────────

def _jid(prefix: str, dom: str) -> str:
    return prefix + dom.replace(".", "_").replace("-", "_")


def _capture(opts: dict) -> list[str]:
    return ["html", "text", "pdf"] if opts.get("pdfs", True) else ["html", "text"]


def _common(dom: str, seed: str, opts: dict) -> dict:
    job = {"seed_urls": [seed], "keyword": False, "incremental": False, "max_depth": -1,
           "same_domain_only": True, "freshness_days": None, "capture": _capture(opts),
           "source_id": dom}
    if opts.get("pdfs", True):
        job["max_pdfs_per_page"] = 0          # UNLIMITED: every Download link on a page
    return job


def phase1_job(dom: str, seed: str, opts: dict) -> dict:
    """Whole-site breadth: render off (the engine auto-escalates JS shells), keyword off,
    unlimited — except the sites the operator bounded by hand."""
    return {"job_id": _jid("compbreadth_", dom), "job_type": "profile", **_common(dom, seed, opts),
            "render_js": False, "max_pages": BOUNDED_WHOLE_SITE.get(dom, 0), "path_scope": []}


def phase2_job(dom: str, seed: str, opts: dict, tier: str) -> dict:
    """Newsroom completeness: render on + click-discovery so load-more is CLICKED, news-scoped,
    unlimited; the window is enforced downstream. On a C4 host the render is left OFF — CamoFox
    on a walled host hits the Imperva wait-wall and stalls; the host solver renders on its own."""
    job = {"job_id": _jid("compnews_", dom), "job_type": "news", **_common(dom, seed, opts),
           "max_pages": 0, "path_scope": list(NEWS_SCOPE)}
    if tier == "C4":
        job.update(render_js=False, click_discovery=False)
    else:
        job.update(render_js=True, click_discovery=True)
    return job


def archive_job(dom: str, seed: str, opts: dict) -> dict:
    """C2: the state of the site when the window opened plus every changed capture through its
    end — the archive tier's own semantics (crawler.models.Job.archive_date/archive_to)."""
    job = {"job_id": _jid("comparchive_", dom), "job_type": "news", **_common(dom, seed, opts),
           "render_js": False, "max_pages": 2000,   # archive mode REQUIRES max_pages>0 (crawler models.py:241)
           "path_scope": list(NEWS_SCOPE) if opts.get("scope") == "news_only" else [],
           "archive_date": opts["window_from"], "archive_to": opts["window_to"]}
    return job


def phase_jobs(dom: str, seed: str, tier: str, opts: dict) -> list[dict]:
    """[{phase, job, note}] for one site. Phase 1 carries breadth AND the archive replay (the
    crawler runs its archive tier before the live phase of a batch); phase 2 is the newsroom."""
    if tier == "FLAG":
        return []
    if tier == "C2":
        return [{"phase": 1, "job": archive_job(dom, seed, opts),
                 "note": "Wayback replay of the window; no live fetch"}]
    out = []
    if opts.get("scope") == "whole_site":
        cap = BOUNDED_WHOLE_SITE.get(dom, 0)
        out.append({"phase": 1, "job": phase1_job(dom, seed, opts),
                    "note": "whole-site breadth" + (" (bounded to %d pages)" % cap if cap else "")})
    out.append({"phase": 2, "job": phase2_job(dom, seed, opts, tier),
                "note": ("newsroom via the host solver — load-more is NOT clicked on a walled "
                         "host; run scripts/session_crawl.py locally for a dated listing" if tier == "C4"
                         else "newsroom: render + load-more clicks, news paths only")})
    return out


# ── estimates ─────────────────────────────────────────────────────────────────

def corpus_pages() -> dict[str, int] | None:
    """Pages the store holds per host. None when the store cannot be reached — an estimate built
    on a guess would be presented as a measurement."""
    from . import api  # deferred: api imports this module
    try:
        rows = api._ingest_get("/api/live/sources?limit=100000", timeout=60.0) or []
    except Exception:  # noqa: BLE001
        return None
    out: dict[str, int] = {}
    for r in rows:
        h = str(r.get("source_id") or "").lower().removeprefix("www.")
        if h:
            out[h] = out.get(h, 0) + int(r.get("pages") or 0)
    return out


def measured_pages(db: Session) -> dict[str, dict]:
    """The latest measured crawl per domain: what it fetched and how long it took."""
    out: dict[str, dict] = {}
    for m in db.scalars(select(SiteMeasurement).order_by(SiteMeasurement.at.desc())).all():
        d = str(m.domain or "").lower().removeprefix("www.")
        if d and d not in out:
            out[d] = {"fetched": int(m.fetched or 0), "duration_s": float(m.duration_s or 0.0),
                      "at": m.at.isoformat() if m.at else None}
    return out


def _estimate(dom: str, corpus: dict | None, measured: dict) -> tuple[int | None, str]:
    m = measured.get(dom)
    if m and m["fetched"] > 0:
        return m["fetched"], "last crawl fetched %d pages in %.0f min" % (m["fetched"], m["duration_s"] / 60)
    if corpus is not None and corpus.get(dom, 0) > 0:
        return corpus[dom], "%d pages already held in the corpus" % corpus[dom]
    return None, "not measured" if corpus is not None else "not measured (store unreachable)"


# ── the plan ──────────────────────────────────────────────────────────────────

def build_plan(sites, options: dict | None = None, *, probe=None, corpus=None,
               measured: dict | None = None, instances: int | None = None,
               today: dt.date | None = None) -> dict:
    """Tier every site, build its phase jobs, estimate, and lay the phases out. Dispatches
    nothing. `corpus` / `measured` / `instances` are injectable so the plan is testable without a
    store or a fleet; None for corpus means "ask the store" (and say so if it does not answer)."""
    opts = normalise_options(options, today)
    entries = parse_sites(sites)
    if not entries:
        raise ValueError("no site in the list — one domain or URL per line")
    if corpus is None:
        corpus = corpus_pages()
    measured = measured or {}
    n_inst = instances if instances is not None else len(crawler_client.pool())

    rows, excluded = [], []
    by_phase: dict[int, list[dict]] = {1: [], 2: []}
    for e in entries:
        dom, seed = e["domain"], e["seed"]
        tier, source, note = detect_tier(dom, probe=probe)
        if tier == "FLAG":
            excluded.append({"domain": dom, "tier": tier, "reason": TIERS["FLAG"] + " — " + note})
            continue
        phases = phase_jobs(dom, seed, tier, opts)
        for p in phases:
            by_phase[p["phase"]].append(p["job"])
        est, basis = _estimate(dom, corpus, measured)
        rows.append({"domain": dom, "seed": seed, "tier": tier, "tier_source": source,
                     "tier_note": note, "tier_label": TIERS[tier],
                     "phases": [{"phase": p["phase"], "job_id": p["job"]["job_id"], "note": p["note"]}
                                for p in phases],
                     "est_pages": est, "est_basis": basis,
                     "corpus_pages": (corpus or {}).get(dom) if corpus is not None else None})

    known = sum(r["est_pages"] or 0 for r in rows)
    unmeasured = sum(1 for r in rows if r["est_pages"] is None)
    parallel = max(1, min(n_inst, len(rows))) if rows else 1
    eta_h = (known / (ASSUMED_PAGES_PER_SEC_PER_INSTANCE * parallel) / 3600.0) if known else None
    by_tier: dict[str, int] = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    for x in excluded:
        by_tier[x["tier"]] = by_tier.get(x["tier"], 0) + 1
    phases = [{"phase": 1, "label": "whole-site breadth (+ archive replay for C2)",
               "jobs": by_phase[1]},
              {"phase": 2, "label": "dated newsroom — render + load-more, news paths only",
               "jobs": by_phase[2]}]
    phases = [p for p in phases if p["jobs"]]
    return {
        "options": opts,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sites": rows, "excluded": excluded, "phases": phases,
        "totals": {"sites": len(rows), "excluded": len(excluded),
                   "jobs": sum(len(p["jobs"]) for p in phases),
                   "est_pages": known, "sites_unmeasured": unmeasured, "by_tier": by_tier,
                   "instances": n_inst},
        "eta": {"hours": round(eta_h, 1) if eta_h is not None else None,
                "basis": ("%d estimated pages across %d site(s) at an ASSUMED %.1f pages/s per "
                          "instance on %d instance(s); %d site(s) have no estimate and are not "
                          "counted" % (known, len(rows), ASSUMED_PAGES_PER_SEC_PER_INSTANCE,
                                        parallel, unmeasured)) if known else
                         "no site has a measured or held page count — no ETA"},
    }


# ── persistence ───────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return scheduler.utcnow()


def save_plan(db: Session, name: str | None, sites, options: dict | None, plan: dict,
              camp: Campaign | None = None) -> Campaign:
    now = _now()
    raw = [e["seed"] for e in parse_sites(sites)]
    if camp is None:
        camp = Campaign(name=(name or "campaign %s" % now.strftime("%Y-%m-%d %H:%M")).strip(),
                        status="draft", options=plan["options"], sites=raw, plan=plan,
                        created_at=now, updated_at=now, runs=0)
        db.add(camp)
    else:
        if name:
            camp.name = name.strip()
        camp.options, camp.sites, camp.plan, camp.updated_at = plan["options"], raw, plan, now
        if camp.status != "running":
            camp.status = "draft"
    db.commit()
    db.refresh(camp)
    return camp


def dto(camp: Campaign, *, with_plan: bool = True) -> dict:
    iso = lambda d: (d.isoformat() + "Z") if d else None  # noqa: E731
    out = {"id": camp.id, "name": camp.name, "status": camp.status, "options": camp.options,
           "sites": camp.sites, "runs": camp.runs, "last_detail": camp.last_detail,
           "created_at": iso(camp.created_at), "updated_at": iso(camp.updated_at),
           "started_at": iso(camp.started_at), "ended_at": iso(camp.ended_at),
           "totals": (camp.plan or {}).get("totals"), "eta": (camp.plan or {}).get("eta")}
    if with_plan:
        out["plan"] = camp.plan
        out["progress"] = camp.progress
    return out


# ── the runner ────────────────────────────────────────────────────────────────
# One campaign at a time. State that must survive a poll lives on the Campaign row; the
# in-process record below is what the running thread updates between polls.

_ACTIVE: dict = {"id": None, "phase": None, "phases": 0, "started_at": None, "detail": None,
                 "instances": {}, "stop": None}
_LOCK = threading.Lock()


def active() -> dict | None:
    return dict(_ACTIVE) if _ACTIVE["id"] is not None else None


def _set_active(**kw) -> None:
    with _LOCK:
        _ACTIVE.update(kw)


def launch(db: Session, camp: Campaign, *, stop_fleet, poll_s: float = 15.0,
           thread: bool = True) -> dict:
    """GO. Refuses a second campaign; otherwise switches to campaign mode (stopping normal work in
    flight — `stop_fleet` is the API's fan-out), STARTS the pipeline (Go is the explicit start),
    marks the row running and hands the phases to a thread. Nothing here is a timer."""
    if _ACTIVE["id"] is not None and _ACTIVE["id"] != camp.id:
        raise RuntimeError("campaign %s is already running — stop it first" % _ACTIVE["id"])
    if _ACTIVE["id"] == camp.id:
        raise RuntimeError("this campaign is already running")
    plan = camp.plan or {}
    if not plan.get("phases"):
        raise RuntimeError("the plan has no jobs — every site was excluded")
    # A fleet that does not answer cannot take a campaign. Refused here, loudly, rather than
    # marking the row "running" and having the thread wait for an owner that never appears.
    answering = [r for r in crawler_client.fanout(crawler_client.batch_status) if r.get("ok")]
    if not answering:
        raise RuntimeError("no crawler instance answered — the fleet is down or unreachable")
    switched = policy.set_mode(db, "campaign")
    stop = stop_fleet() if switched else None
    policy.start(db)
    now = _now()
    camp.status, camp.runs, camp.started_at, camp.ended_at = "running", (camp.runs or 0) + 1, now, None
    camp.last_detail = "launched: %d job(s) in %d phase(s)" % (plan["totals"]["jobs"], len(plan["phases"]))
    corpus = corpus_pages()
    camp.progress = {"pages_start": {r["domain"]: (corpus or {}).get(r["domain"]) for r in plan["sites"]},
                     "phase": None, "instances": {}, "sites": {}}
    db.commit()
    scheduler.log_event(db, "campaign_start", detail="%s (#%s): %s" % (camp.name, camp.id, camp.last_detail))
    db.commit()
    _set_active(id=camp.id, phase=None, phases=len(plan["phases"]), started_at=now.isoformat(),
                detail=camp.last_detail, instances={}, stop=None)
    if thread:
        Thread(target=_run, args=(camp.id, plan, poll_s), daemon=True,
               name="campaign-%s" % camp.id).start()
    else:
        _run(camp.id, plan, poll_s)
    return {"ok": True, "campaign": dto(camp), "mode_switched": switched, "stop": stop}


def _lane_open() -> bool:
    from .db import SessionLocal
    with SessionLocal() as db:
        return scheduler.lane_open(db, "manual")


def _busy(base: str) -> bool | None:
    """True/False, or None when the instance did not answer — a silent instance must not read as
    idle, or the next phase is sent on top of a running batch."""
    try:
        return bool((crawler_client.batch_status(base=base) or {}).get("running"))
    except Exception:  # noqa: BLE001
        return None


def _dispatch_phase(phase: dict, poll_s: float) -> dict[int, list[str]] | None:
    """Route the phase's jobs to the instances that own their domains and send each bucket as ONE
    background batch (the crawler runs a batch's jobs concurrently). A busy owner is waited for,
    not skipped and not raced. None = the gate closed while waiting."""
    from .db import SessionLocal
    bases = crawler_client.pool()
    with SessionLocal() as db:
        buckets = shard.route(db, phase["jobs"], len(bases))
    sent: dict[int, list[str]] = {}
    pending = dict(sorted(buckets.items()))
    while pending:
        if not _lane_open():
            return None
        for inst in list(pending):
            base = bases[inst] if inst < len(bases) else bases[0]
            if _busy(base) is not False:
                continue
            try:
                crawler_client.dispatch_batch(
                    {"jobs": pending[inst], "background": True, "forward_to_ingest": True},
                    base=base)
            except Exception as e:  # noqa: BLE001 — a 409 means it became busy; retry next poll
                log.warning("campaign: instance %d refused the batch (%s); retrying", inst, e)
                continue
            sent[inst] = [j["job_id"] for j in pending.pop(inst)]
            _ACTIVE["instances"][inst] = {"jobs": sent[inst], "phase": phase["phase"],
                                          "sent_at": _now().isoformat()}
        if pending:
            time.sleep(poll_s)
    return sent


def _wait_idle(sent: dict[int, list[str]], poll_s: float) -> bool:
    """Poll the instances that took work until none is running. False = the gate closed."""
    bases = crawler_client.pool()
    while True:
        if not _lane_open():
            return False
        still = [i for i in sent if _busy(bases[i] if i < len(bases) else bases[0]) is not False]
        if not still:
            return True
        time.sleep(poll_s)


def _run(camp_id: int, plan: dict, poll_s: float) -> None:
    """The campaign thread: phase by phase, each behind the gate, each waited out."""
    from .db import SessionLocal
    ended = "finished"
    detail = "every phase completed"
    try:
        for phase in plan["phases"]:
            _set_active(phase=phase["phase"], detail="phase %s: dispatching %d job(s)"
                        % (phase["phase"], len(phase["jobs"])))
            sent = _dispatch_phase(phase, poll_s)
            if sent is None:
                ended, detail = "stopped", "stopped while dispatching phase %s" % phase["phase"]
                break
            _set_active(detail="phase %s: %d job(s) on %d instance(s), waiting for them to finish"
                        % (phase["phase"], sum(len(v) for v in sent.values()), len(sent)))
            if not _wait_idle(sent, poll_s):
                ended, detail = "stopped", "stopped during phase %s" % phase["phase"]
                break
    except Exception as e:  # noqa: BLE001 — the row must never be left "running" by a crash
        log.exception("campaign %s failed", camp_id)
        ended, detail = "stopped", "failed: %s" % str(e)[:200]
    with SessionLocal() as db:
        camp = db.get(Campaign, camp_id)
        if camp is not None and camp.status == "running":
            _end(db, camp, ended, detail)


def _end(db: Session, camp: Campaign, status: str, detail: str) -> None:
    """Close the campaign and RETURN TO A CLEAN NORMAL MODE: the mode switches back, the daily
    domains are pinned onto the lane once, and the campaign's frontiers stay parked (the resume
    lane is scoped by owner). The run state is left as it is — a finished campaign hands the fleet
    back to the daily lane; a Stop has already stopped it."""
    camp.status, camp.ended_at, camp.last_detail = status, _now(), detail
    prog = dict(camp.progress or {})
    prog["instances"] = dict(_ACTIVE.get("instances") or {})
    prog["phase"] = _ACTIVE.get("phase")
    camp.progress = prog
    db.commit()
    scheduler.log_event(db, "campaign_%s" % status, detail="%s (#%s): %s" % (camp.name, camp.id, detail))
    db.commit()
    _set_active(id=None, phase=None, phases=0, started_at=None, detail=None, instances={}, stop=None)
    if policy.set_mode(db, "normal"):
        try:
            from . import daily
            daily.pin_to_lane(db)
        except Exception:  # noqa: BLE001 — the mode has switched; the pin is best effort here
            log.exception("campaign end: pin_to_lane failed")


def close(db: Session, camp: Campaign, detail: str) -> Campaign:
    """Close a campaign row that says 'running' but is not the active one (a ghost left by a
    crash) — same clean-up as the real end."""
    if _ACTIVE["id"] == camp.id:
        mark_stopped(db, detail)
    elif camp.status == "running":
        _end(db, camp, "stopped", detail)
    return camp


def mark_stopped(db: Session, detail: str) -> Campaign | None:
    """The Stop button (and a mode switch to normal) ending a running campaign. The thread sees the
    closed gate on its next poll and exits without touching the row again."""
    cid = _ACTIVE["id"]
    if cid is None:
        return None
    camp = db.get(Campaign, cid)
    if camp is None:
        _set_active(id=None, phase=None, instances={})
        return None
    _end(db, camp, "stopped", detail)
    return camp


def recover(db: Session) -> list[int]:
    """At boot: a row still 'running' has no thread behind it — the process restarted mid-campaign.
    Close it honestly and return to normal mode rather than leave a ghost campaign holding the
    lanes. The saved frontiers are intact; the operator re-runs from the saved plan."""
    out = []
    for camp in db.scalars(select(Campaign).where(Campaign.status == "running")).all():
        _end(db, camp, "stopped", "orchestrator restarted mid-campaign — re-run from the saved plan")
        out.append(camp.id)
    return out


def progress(db: Session, camp: Campaign) -> dict:
    """What has happened so far, from REAL counts: per site, pages held now against pages held at
    launch; per instance, whether it is still running. Nothing here is estimated."""
    plan = camp.plan or {}
    prog = camp.progress or {}
    live = active() if _ACTIVE["id"] == camp.id else None
    instances = (live or {}).get("instances") or prog.get("instances") or {}
    corpus = corpus_pages() if camp.status in ("running", "finished", "stopped") else None
    start = prog.get("pages_start") or {}
    owner_of: dict[str, int] = {}
    for inst, rec in instances.items():
        for jid in rec.get("jobs") or []:
            owner_of[jid] = int(inst)
    bases = crawler_client.pool()
    busy = {i: _busy(bases[i]) for i in {int(i) for i in instances} if i < len(bases)} if live else {}
    sites = []
    for r in plan.get("sites") or []:
        dom = r["domain"]
        jids = [p["job_id"] for p in r.get("phases") or []]
        insts = sorted({owner_of[j] for j in jids if j in owner_of})
        if camp.status == "running":
            state = ("running" if any(busy.get(i) for i in insts) else
                     "done" if insts else "queued")
        else:
            state = "done" if camp.status == "finished" else ("stopped" if insts else "not started")
        now_pages = (corpus or {}).get(dom) if corpus is not None else None
        st = start.get(dom)
        sites.append({"domain": dom, "tier": r["tier"], "instances": insts, "state": state,
                      "pages_start": st, "pages_now": now_pages,
                      "captured": (now_pages - st) if (now_pages is not None and st is not None) else None})
    return {"status": camp.status, "phase": (live or {}).get("phase") or prog.get("phase"),
            "phases": len(plan.get("phases") or []), "detail": (live or {}).get("detail") or camp.last_detail,
            "started_at": dto(camp)["started_at"], "ended_at": dto(camp)["ended_at"],
            "instances": [{"instance": int(i), "phase": rec.get("phase"), "jobs": len(rec.get("jobs") or []),
                           "running": busy.get(int(i))} for i, rec in sorted(instances.items(), key=lambda kv: int(kv[0]))],
            "sites": sites, "corpus_reachable": corpus is not None}
