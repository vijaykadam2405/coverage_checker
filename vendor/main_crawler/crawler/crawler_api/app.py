"""Crawler HTTP API.

Endpoints
---------
POST /v1/crawl                run one crawl job (§2) -> raw page bundles (§3)
POST /v1/crawl/batch          run many jobs ({"jobs": [ ... ]}, "parallel": N)
POST /v1/check-keywords       probe one URL for keyword relevance (no crawl/ingest)
POST /v1/check-keywords/batch probe many URLs at once (filter before crawling)
POST /v1/suggest-job          build a Job with probe-adaptive auto-selected keywords
POST /v1/probe-sites          classify sites (pure vs mixed) -> recommended keyword/render_js/path_scope
GET  /v1/schema               JSON schemas for the job input + the document output
GET  /health                  liveness

Request body for /v1/crawl is a crawl job. Add ``"forward_to_ingest": true`` to
also POST each kept page to the Ingest API (the production push flow);
otherwise documents are only returned inline (pull flow, ideal for Layer 2
testing).

Run:  python run.py crawler-api      # serves on http://127.0.0.1:8099
Auth: open, EXCEPT the three routes that start a crawl, which require the orchestrator's arming
secret once CRAWLER_ARM_TOKEN is set on this instance — see `require_armed`.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from collections import Counter
from threading import Lock, Thread

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from crawler import config, issues
from crawler.async_engine import (_now_iso, adaptive_state, clear_stop, dropped_jobs, lane_state,
                                   progress_state, request_stop, run_batch_async,
                                   run_batch_multiprocess, run_state, set_dropped_jobs,
                                   stop_requested)
from crawler.jobgen import generate as generate_jobs
from crawler.keywords import get_corpus
from crawler.models import Document, Job
from crawler.seed import load_seed

from . import dashboard
from .crawlclaim import CrawlClaim

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # scripts/ on path
from scripts.check_keywords import check_keywords as _check_keywords
from scripts.check_keywords import discover_keywords as _discover_keywords
from crawler.jobgen import _candidate_pool

app = FastAPI(title="Mallory Crawler API (Layer 1)", version="0.1.0",
              docs_url="/v1/docs", redoc_url=None)

_SEED = load_seed()
_KP = get_corpus()          # global keyword-corpus trie (keep-gate), built once


# ── Fleet heartbeat ───────────────────────────────────────────────────────────
# This crawler runs on N machines that all push into ONE data centre. Without a heartbeat the
# centre can see the DOCUMENTS a machine produced but nothing about the machine — not whether it is
# still alive, not whether it is mid-crawl, not why it stopped. So every node reports itself on a
# timer into the same Postgres its documents already go to (crawler.fleet).
#
# Deliberately a background task and not part of any request path: telemetry must never be able to
# slow, block or fail a crawl. fleet.send() swallows its own errors and self-cools, so a centre
# that is down costs one log line per cooldown, not a failed page.
_FLEET_LAST: dict = {"ok": False, "at": None, "error": "not started yet"}


async def _fleet_heartbeat_loop() -> None:
    from crawler import fleet
    every = max(5, int(os.environ.get("FLEET_HEARTBEAT_S", "15")))
    if not fleet.centre_dsn():
        log.info("fleet: no FLEET_DSN/DATA_CENTRE_DSN/DATABASE_URL — heartbeat disabled")
        _FLEET_LAST.update(ok=False, error="no dsn configured")
        return
    log.info("fleet: node=%s reporting every %ss", fleet.node_id(), every)
    while True:
        try:
            m = await asyncio.to_thread(metrics)          # psutil sampling blocks ~0.3s
            pending = None
            try:
                from ingest_api import pgstore
                pending = (pgstore.push_backlog() or {}).get("pending")
            except Exception:
                pending = None                            # local store optional; not an error
            payload = await asyncio.to_thread(
                fleet.build_payload, m, push_pending=pending, agent_version=app.version)
            res = await asyncio.to_thread(fleet.send, payload)
            _FLEET_LAST.update(ok=res.get("ok"), at=_now_iso(), error=res.get("error") or "",
                               node_id=payload["node_id"], status=payload["status"])
            if not res.get("ok") and not res.get("cooling_down") and not res.get("skipped"):
                log.warning("fleet heartbeat failed: %s", res.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001
            _FLEET_LAST.update(ok=False, error=str(exc)[:200])
            log.warning("fleet heartbeat loop error: %s", exc)
        await asyncio.sleep(every)


@app.on_event("startup")
async def _start_fleet_heartbeat() -> None:
    if os.environ.get("FLEET_HEARTBEAT", "1") == "0":
        log.info("fleet: heartbeat disabled by FLEET_HEARTBEAT=0")
        return
    asyncio.create_task(_fleet_heartbeat_loop())


@app.get("/v1/fleet/self")
def fleet_self() -> dict:
    """What THIS node last reported, and whether the centre accepted it.

    Exists so an operator on the machine can tell 'my heartbeat is not arriving' apart from 'the
    dashboard is not showing me', which otherwise look identical from the far end."""
    from crawler import fleet
    dsn = fleet.centre_dsn()
    from urllib.parse import urlsplit
    label = ""
    if dsn:
        try:
            u = urlsplit(dsn.replace("postgresql+psycopg://", "postgresql://"))
            label = f"{u.hostname}:{u.port or 5432}{u.path}"     # never the credentials
        except Exception:
            label = "(unparseable dsn)"
    return {"node_id": fleet.node_id(), "reporting_to": label or None,
            "every_s": int(os.environ.get("FLEET_HEARTBEAT_S", "15")),
            "enabled": os.environ.get("FLEET_HEARTBEAT", "1") != "0",
            "last": dict(_FLEET_LAST)}

# Progress tracker for active batch runs
_batch_lock = Lock()
log = logging.getLogger(__name__)
_batch_status: dict = {"running": False, "total": 0, "done": 0, "current_job": "", "results": []}

# ONE claim for BOTH crawl entry points. See crawlclaim.py for why this is not the batch route's
# `owner.is_alive()` test copied on to /v1/crawl: that test cannot heal from an owner that is alive
# FOREVER, which is the failure this fleet actually has — a dispatch blocked on `.result()` for a
# jammed host is alive by every measure Python can take, so the instance would refuse all work
# until the container was restarted.
_crawl_claim = CrawlClaim()


# ── Arming: a crawl may START here only if the orchestrator asked for it ──────────────────────
# The orchestrator's control plane (policy.py — Start/Stop, normal/campaign mode, lane_open) is
# the one answer to "may anything crawl right now, and what". It is a decision made in ONE
# process, and a POST straight to this instance's /v1/crawl/batch — a placer script, a stale curl
# in a shell history, the batch box on this instance's own dashboard — walks past all of it. So
# the three routes that start work require a shared secret that the orchestrator sends on every
# dispatch (orchestrator/crawler_client.py puts it in the X-Mallory-Arm header).
#
# FAIL-SAFE BY DEFAULT: CRAWLER_ARM_TOKEN unset or empty on this instance = NO lock, every route
# accepts exactly as before. The image is therefore deployable mid-cycle with no flag day; nothing
# changes until the operator sets the token here — and the orchestrator's copy must be set FIRST,
# or every legitimate batch is refused for the gap.
#
# Only the routes that START a crawl are locked: /v1/crawl, /v1/crawl/batch, /v1/crawl/resume.
# Status, reports, metrics, health, stop and drop stay open — the dashboard polls them, and a
# Stop that needed a secret would be a Stop that could fail.
#
# Not to be confused with `_arm()` below, which clears the stop FILE. That is "the operator said
# start"; this is "and it really was the operator".
ARM_ENV = "CRAWLER_ARM_TOKEN"
ARM_HEADER = "X-Mallory-Arm"          # == the `x_mallory_arm` parameter name, per FastAPI's rule


def arm_token() -> str:
    """Read LIVE, not at import: the lock follows the container's environment, and a test sets
    and clears it without re-importing the app."""
    return os.environ.get(ARM_ENV, "").strip()


def require_armed(request: Request, x_mallory_arm: str | None = Header(default=None)) -> None:
    """The dependency on every crawl-starting route. Raises 403 BEFORE the route body runs, so a
    refused request touches neither the claim nor the stop file and no engine is created."""
    want = arm_token()
    if not want:
        return                                    # no token configured -> open, as before
    got = x_mallory_arm or ""
    if got and hmac.compare_digest(got.encode(), want.encode()):
        return
    log.warning("refused an un-armed crawl request: %s %s from %s (%s header %s)",
                request.method, request.url.path,
                request.client.host if request.client else "?",
                ARM_HEADER, "missing" if not got else "wrong")
    raise HTTPException(403, "crawl entry points on this instance are locked: only the "
                             "orchestrator may start a crawl (%s header missing or wrong)"
                             % ARM_HEADER)


def _crawl_progress(job_ids):
    """Has the running crawl actually MOVED? -> a snapshot, or None meaning "cannot tell".

    Two sources, unioned, because neither covers every deployment shape:

      * `progress_state()["pages"]` is the live in-process counter and costs nothing — but it lives
        in THIS process, and with CRAWLER_WORKERS>1 the pages are fetched in child processes where
        the counter is bumped and never seen from here.
      * the frontier `done` count is written to the shared history DB by whichever process did the
        work, so it moves in both shapes. It costs a query, which is why the claim only reaches it
        once a crawl is old enough to be worth judging at all.

    `_batch_status` is deliberately NOT a source: per-job counts land only at batch end (see the
    /v1/metrics docstring), so a heartbeat read off it would never tick during a legitimate
    multi-hour crawl and would declare every long crawl wedged.
    """
    snap = {}
    try:
        snap["pages"] = int((progress_state() or {}).get("pages") or 0)
    except Exception:
        pass
    try:
        from crawler.dedup import CrawlHistory
        h = CrawlHistory()
        try:
            snap["frontier"] = {j: h.frontier_counts(j).get("done", 0) for j in job_ids}
        finally:
            h.close()
    except Exception:
        pass
    return snap or None


def _refusal(claim: dict) -> str:
    """The operator-facing sentence. A wedge and a healthy long crawl must not read alike."""
    d = claim.get("detail") or {}
    if claim.get("reason") == "wedged":
        return ("a crawl is already running and has made no progress for %ss (%s, jobs %s) — "
                "retry with force=true to take it over"
                % (d.get("stalled_s"), d.get("route"), ", ".join(d.get("job_ids") or []) or "?"))
    return ("a crawl is already running (%s, jobs %s, held %ss)"
            % (d.get("route"), ", ".join(d.get("job_ids") or []) or "?", d.get("held_s")))


class CrawlRequest(Job):
    """A crawl job (§2) plus API-only knobs."""
    forward_to_ingest: bool = False


@app.get("/health")
def health() -> dict:
    # `arm_lock` says whether this instance refuses un-armed crawl starts — the one thing an
    # operator rolling the token out needs to see per instance. Never the token itself.
    return {"status": "ok", "entities": len(_SEED.entities),
            "sources": len(_SEED.sources), "keywords": len(_KP),
            "arm_lock": bool(arm_token())}


@app.get("/v1/camofox")
def camofox_status() -> dict:
    """C3 CamoFox stealth engine status for the dashboard: is it enabled
    (CAMOFOX_ENABLED=1) and is the server answering /health. Also reports
    whether the captcha solver is configured."""
    from crawler import camofox_client, captcha_solver
    solver = captcha_solver.CaptchaSolver.from_env()
    return {"enabled": camofox_client.enabled(),
            "healthy": camofox_client.health(),
            "url": camofox_client.base_url(),
            "solver_configured": solver.api_key is not None and bool(solver.api_key),
            "solver_provider": os.environ.get("CAPTCHA_PROVIDER", ""),
            "buster_solve": os.environ.get("CAMOFOX_SOLVE_RECAPTCHA", "1") == "1"}


# ── tier health (GET /v1/tiers) ──────────────────────────────────────────────
# The operator's C1/C2/C3 strip is asking "would a fetch work RIGHT NOW", so every probe runs
# through that tier's OWN code path. A hand-rolled httpx.get() here would stay green while the
# real ladder was broken — the one lie a status light must never tell.
C1_PROBE_URL = "https://example.com"          # IANA-reserved, tiny, and nobody's crawl target
# web.archive.org is the host C2 really talks to (c2.CDX_URL and c2.REPLAY both live there), and
# it is the host to probe rather than the archive.org apex or its /wayback/available API. Measured
# from this egress 2026-07-21: apex TCP:443 times out while web.archive.org connects in 265ms, and
# /wayback/available answers 404 on web.archive.org — either would paint C2 red while C2 works.
# Root, not c2.CDX_URL, for two reasons: CDX is the rate-limited surface where a 429 earns a 1-hour
# IP ban that DOUBLES per violation (crawler/c2.py "rate limiting"), and a cold CDX query measured
# >8s, so probing it would time out and report DOWN on a perfectly healthy archive.
C2_PROBE_URL = "https://web.archive.org/"
_TIER_TIMEOUT_S = 8.0
# Ceiling on ONE probe. The socket timeout alone does not bound Fetcher.fetch — its 429/503 arm
# sleeps out Retry-After before refetching — and a poll that hangs is worse than a red tile.
_TIER_DEADLINE_S = 10.0
# The dashboard polls this endpoint. Three live requests per poll is steady pointless load on
# hosts we do not own, so an answer stands for 30s and everyone inside that window is told so.
_TIER_TTL_S = 30.0
_tier_lock = Lock()
_tier_cache: dict = {"at": 0.0, "payload": None}


def _probe_c1() -> dict:
    """C1 — live direct fetch, probed through Fetcher itself (httpx + curl_cffi impersonation)."""
    if not config.allow_network():
        return {"enabled": False, "up": False, "detail": "disabled"}
    from crawler.fetcher import Fetcher
    # prefer_fixtures off: a fixture hit would report "up" with no packet leaving the box.
    # robots/delay/retries off: this measures TRANSPORT, and a robots.txt round-trip would double
    # both the probe's cost and the latency number the operator reads off the tile.
    f = Fetcher(config.FALLBACK_CAPTURE_DEFAULTS["user_agent"], timeout_s=int(_TIER_TIMEOUT_S),
                delay_s=0.0, max_retries=0, render_js=False, respect_robots=False,
                prefer_fixtures=False, allow_network=True)
    r = f.fetch(C1_PROBE_URL)
    if r.status is None:                       # transport died — no HTTP status was ever reached
        return {"enabled": True, "up": False, "detail": (r.error or "fetch failed")[:80]}
    return {"enabled": True, "up": 200 <= r.status < 400, "detail": f"HTTP {r.status}"}


def _probe_c2() -> dict:
    """C2 — the Wayback archive tier. Built with c2's own client (its UA and CRAWLER_PROXY_URL
    egress, so this probes the path C2 really takes) but NOT via c2._get: that shares the run-wide
    rate gate, and a poll issued mid-archive-job would queue behind it and stall the dashboard."""
    if not config.allow_network():
        return {"enabled": False, "up": False, "detail": "disabled"}
    from crawler import c2
    with c2._client(_TIER_TIMEOUT_S) as client:
        sc = client.head(C2_PROBE_URL).status_code   # HEAD: reachability, not the IA homepage
    return {"enabled": True, "up": 200 <= sc < 400, "detail": f"HTTP {sc}"}


def _probe_c3() -> dict:
    """C3 — CamoFox, straight through camofox_status() so this tile and /v1/camofox can never
    disagree about whether the stealth engine is up."""
    st = camofox_status()
    if not st["enabled"]:
        return {"enabled": False, "up": False, "detail": "disabled"}
    return {"enabled": True, "up": bool(st["healthy"]),
            "detail": "healthy" if st["healthy"] else "no /health response"}


def _probe_c4() -> dict:
    """C4 — the host solver, probed through hostsolver's own config so this tile cannot disagree
    with what the fetch ladder actually does.

    C4 needs a real GPU and therefore lives OFF this box: measured here 2026-08-02, Chrome in a
    container on this hypervisor reports `ANGLE (Mesa, llvmpipe)` even with /dev/dri passed
    through and vmwgfx forced, because the VM exposes no 3D at all — and llvmpipe is precisely
    what Cloudflare's interactive tier refuses. So the tier is a network hop to a workstation, and
    a network hop is a thing that dies quietly.

    IT IS NOT ENOUGH THAT SOMETHING ANSWERS. The sharpest diagnostic error in this project's
    record was blaming the ingest API for dropping documents when in fact an SSH tunnel had died
    and another local service held the same port and answered happily. So this asserts the
    response SHAPE — host_solver's /health uniquely returns `browser_open` and `solved_hosts` —
    and reports "not the solver" rather than "up" when they are missing.
    """
    from crawler import hostsolver
    if not hostsolver.enabled():
        # Off is a deployment choice, not a fault: an interactive wall is recorded and skipped.
        return {"enabled": False, "up": False, "detail": "disabled"}
    h = hostsolver.health()
    if not isinstance(h, dict):
        return {"enabled": True, "up": False, "detail": "unreachable"}
    if "browser_open" not in h or "solved_hosts" not in h:
        return {"enabled": True, "up": False, "detail": "answered but NOT the host solver"}
    return {"enabled": True, "up": True,
            "detail": "browser %s · %d host(s) solved"
                      % ("open" if h.get("browser_open") else "idle",
                         len(h.get("solved_hosts") or []))}


async def _probe_tier(label: str, probe) -> dict:
    """Run one blocking probe off the event loop and turn EVERY failure into data. A dead tier is
    the answer this endpoint exists to give, so nothing below may escape as a 500."""
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(asyncio.to_thread(probe), _TIER_DEADLINE_S)
    except asyncio.TimeoutError:
        out = {"enabled": True, "up": False, "detail": "timeout"}
    except Exception as exc:  # noqa: BLE001
        # Truncated: an operator tile has one line, and some transport errors carry a whole
        # certificate chain in their message.
        out = {"enabled": True, "up": False, "detail": (str(exc) or type(exc).__name__)[:80]}
    return {"label": label, "up": bool(out["up"]),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "detail": out["detail"], "enabled": bool(out["enabled"])}


@app.get("/v1/tiers")
async def tiers() -> dict:
    """Live up/down for every fetch tier — the dashboard's C1/C2/C3/C4 strip.

    C4 was added late and deliberately: it is the only rung that clears a Cloudflare-interactive
    wall, it is the only one that lives off this box, and until it had a light here a dead C4 was
    indistinguishable from a crawl that simply met no walls.

    The probes run CONCURRENTLY so the call costs max(probe) instead of the sum, and the answer is
    then cached for _TIER_TTL_S: the dashboard polls, and four real network probes per poll is not
    a price a status light gets to charge. The lock is not held across the gather (it is a threading
    Lock — that would stall the event loop), so two callers racing a cold cache both probe; one
    extra probe per 30s is cheaper than a blocked loop."""
    now = time.monotonic()
    with _tier_lock:
        cached = _tier_cache["payload"]
        if cached is not None and now - _tier_cache["at"] < _TIER_TTL_S:
            return {**cached, "cached": True}
    c1, c2, c3, c4 = await asyncio.gather(_probe_tier("C1 live", _probe_c1),
                                          _probe_tier("C2 Wayback", _probe_c2),
                                          _probe_tier("C3 CamoFox", _probe_c3),
                                          _probe_tier("C4 host solver", _probe_c4))
    payload = {"checked_at": _now_iso(), "cached": False, "c1": c1, "c2": c2, "c3": c3, "c4": c4}
    with _tier_lock:
        _tier_cache.update(at=time.monotonic(), payload=payload)
    return payload


def _paid_proxy_status() -> dict:
    """Bright Data gate state. Never raises: a metrics endpoint must not 500 because a cost
    counter is unreadable — and an unreadable counter already denies spend on its own."""
    try:
        from crawler import paid_proxy
        return paid_proxy.status()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": None, "error": str(exc)[:120]}


def _host_mem() -> dict | None:
    """Host memory corrected for hypervisor ballooning, or None if /proc is unreadable.

    psutil's `percent` is wrong on this box in two ways at once. This is a VMware guest and the
    balloon driver currently holds ~549 GB of its 1008 GB: those pages are pinned inside the guest
    and counted as USED, while the denominator is a total the VM does not actually have. Result:
    64% reported when real demand was ~90 GB against ~458 GB genuinely usable — 20%.

    The balloon size is (inflate - deflate) pages from /proc/vmstat; both are monotonic lifetime
    counters, so only the difference is meaningful. On a non-ballooned host they are absent and
    everything below degrades to the ordinary reading.
    """
    try:
        mi = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mi[k] = int(v.strip().split()[0]) * 1024
        total, avail = mi["MemTotal"], mi["MemAvailable"]
    except (OSError, KeyError, ValueError):
        return None

    ballooned = 0
    try:
        vm = {}
        with open("/proc/vmstat") as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                vm[k] = int(v)
        page = os.sysconf("SC_PAGE_SIZE")
        net = vm.get("balloon_inflate", 0) - vm.get("balloon_deflate", 0)
        # Negative means more has been given back than taken; that is not a debt, it is zero.
        ballooned = max(0, net) * page
    except (OSError, ValueError, AttributeError):
        ballooned = 0

    usable = max(1, total - ballooned)
    used = max(0, usable - avail)
    return {
        "usable_gb": round(usable / 1e9, 1),
        "used_gb": round(used / 1e9, 1),
        "available_gb": round(avail / 1e9, 1),
        "percent": round(100.0 * used / usable, 1),
        "provisioned_gb": round(total / 1e9, 1),
        "ballooned_gb": round(ballooned / 1e9, 1),
        # The honest split of what is actually held: anonymous memory is real demand, page cache
        # is handed back the moment anything asks. Reporting them as one number is what made a
        # co-tenant's 225 GB of disk cache look like consumption.
        "anon_gb": round(mi.get("AnonPages", 0) / 1e9, 1),
        "cache_gb": round((mi.get("Cached", 0) + mi.get("Buffers", 0)) / 1e9, 1),
    }


def _cgroup_mem() -> dict | None:
    """What THIS container is using against its own limit, or None if that cannot be read.

    psutil cannot answer this: inside a container it reads the host's /proc/meminfo, so a crawler
    holding 24 GB of a 240 GB allowance reported 64% — the whole box, co-tenants included. The
    operator reads that as the crawler about to take the machine down.

    cgroup v2 (`memory.current` / `memory.max`) first, v1 as fallback. `max` reads the literal
    string "max" when no limit is set, which is not a number and must not become one.
    """
    v2 = ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max")
    v1 = ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
          "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    for cur_p, max_p in (v2, v1):
        try:
            with open(cur_p) as fh:
                used = int(fh.read().strip())
            with open(max_p) as fh:
                raw = fh.read().strip()
        except (OSError, ValueError):
            continue
        limit = None
        if raw.isdigit():
            n = int(raw)
            # v1 reports "no limit" as a number near 2**63; treat anything above the host's own
            # RAM as unlimited rather than drawing a meter against a nonsense denominator.
            if n < (1 << 62):
                limit = n
        return {"used_gb": round(used / 1e9, 1),
                "limit_gb": round(limit / 1e9, 1) if limit else None,
                "percent": round(100.0 * used / limit, 1) if limit else None}
    return None


def _disk_usage() -> dict:
    """Root-volume usage, same shape as the ram block. Never raises: the crawler writes blobs,
    PDFs and sqlite shards, so a filling disk is exactly what this tile warns about and losing the
    whole metrics call to a psutil error would hide it. The zeroed shape carries `error` so the
    dashboard can show "unknown" rather than a convincing-looking empty disk."""
    import psutil

    try:
        du = psutil.disk_usage("/")
        return {"used_gb": round(du.used / 1e9, 1), "total_gb": round(du.total / 1e9, 1),
                "percent": du.percent}
    except Exception as exc:  # noqa: BLE001
        return {"used_gb": 0, "total_gb": 0, "percent": 0, "error": str(exc)[:120]}


@app.get("/v1/metrics")
def metrics() -> dict:
    """Live hardware + crawl metrics for the dashboard strip. CPU/RAM are the WSL2-VM view
    (psutil reads the container's /proc) — i.e. 'is the whole box busy'. batch is coarse for
    the async pool (per-job counts land only at batch end); the CPU/RAM gauges are the live
    signal that the pool is working."""
    import os

    import psutil

    vm = psutil.virtual_memory()
    with _batch_lock:
        bs = dict(_batch_status)
    agg = {"jobs": 0, "fetched": 0, "kept": 0, "sent": 0, "accepted": 0,
           "skipped": 0, "recovered": 0, "not_modified_304": 0,
           "skipped_unchanged": 0, "skipped_duplicate": 0, "dropped_by_gate": 0}
    for r in bs.get("results", []) or []:
        agg["jobs"] += 1
        for k in ("fetched", "kept", "sent", "accepted", "skipped", "recovered",
                  "not_modified_304", "skipped_unchanged", "skipped_duplicate", "dropped_by_gate"):
            agg[k] += int(r.get(k, 0) or 0)
    W = int(os.environ.get("CRAWLER_BROWSERS", "8"))
    T = int(os.environ.get("CRAWLER_TABS_PER_BROWSER", "12"))
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),   # accurate instantaneous read
        "cpu_count": psutil.cpu_count(),
        # HOST-wide, and labelled as such downstream. psutil reads /proc/meminfo, which inside a
        # container is the HOST's — so this number includes every co-tenant on the box. Reported
        # as 64% while this container was using 24 GB of its own 240 GB limit, which reads as the
        # crawler eating the machine when it is using a tenth of what it was given.
        # `host_mem` is the corrected reading and is what the dashboard renders; `ram` stays as
        # psutil reported it so the raw figure is still inspectable, but it is the wrong number on
        # a ballooned guest and nothing should draw a meter from it.
        "host_mem": _host_mem(),
        "ram": {"used_gb": round((vm.total - vm.available) / 1e9, 1),
                "total_gb": round(vm.total / 1e9, 1), "percent": vm.percent,
                "scope": "host", "uncorrected": True},
        # …and what THIS container is actually allowed and actually using. cgroup v2 first, v1
        # fallback; None when neither is readable (not containerised, or an unusual runtime) so
        # the dashboard shows "—" rather than implying the cap does not exist.
        "container_ram": _cgroup_mem(),
        "disk": _disk_usage(),
        "pool": {"engine": "async",
                 "browsers": W, "tabs_per_browser": T, "tabs": W * T,
                 # Per-host cap is adaptive (pool spread across the batch's distinct hosts),
                 # unless pinned via CRAWLER_HOST_CONCURRENCY. Report the pin or the range.
                 "host_concurrency": (
                     os.environ["CRAWLER_HOST_CONCURRENCY"]
                     if os.environ.get("CRAWLER_HOST_CONCURRENCY")
                     else f'{os.environ.get("CRAWLER_HOST_CONCURRENCY_FLOOR", "3")}'
                          f'-{os.environ.get("CRAWLER_HOST_CONCURRENCY_CEIL", "24")} adaptive')},
        "batch": {"running": bool(bs.get("running")), "total": bs.get("total", 0),
                  "done": bs.get("done", 0), "current": bs.get("current_job", ""),
                  "stopping": stop_requested() and bool(bs.get("running"))},
        "adaptive": adaptive_state(),   # CPU-feedback render-concurrency controller (live)
        # Lane occupancy: render work is queued separately from httpx work and only a bounded share
        # of workers may be inside a render, so httpx always has capacity. httpx_reserve is the
        # number of workers structurally unavailable to renders — if it reads 0 the split is broken.
        "lanes": lane_state(),
        "progress": progress_state(),   # live page counter → dashboard pages/sec speed tile
        "issues": _issues_summary(),    # unresolved pages, by cause (see /v1/issues)
        # Paid egress spend, surfaced so it is visible rather than discovered on an invoice.
        "paid_proxy": _paid_proxy_status(),
        "totals": agg,
    }


def _issues_summary() -> dict:
    """Counts only — the metrics strip should never carry the whole table."""
    try:
        return issues.log().summary()
    except Exception:
        return {"total": 0, "by_cause": {}, "by_host": {}}


@app.get("/v1/resumable")
def resumable(limit: int = 1000) -> dict:
    """Crawls that stopped before finishing and can be picked up where they left off.

    A job appears here when it still has `pending` rows in the durable frontier — i.e. URLs it had
    discovered but never processed. Restarting such a job normally re-walks the whole site: the
    content hash keeps duplicates out of the corpus, so the data stays correct, but the crawl pays
    the full fetch cost again, which on a large site is hours.

    `limit` is now a parameter and defaults to 1000, not the store's silent 100. On a 7-instance
    fleet the old cap under-reported the backlog by up to 7x and, worse, a "resume everything"
    built from this list resumed only the first hundred jobs per instance while reporting success —
    so the rest sat pending and the fleet looked simply slow. `truncated` says when the cap bit,
    because a list that is quietly short is the failure this endpoint exists to prevent.
    """
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    try:
        rows = h.resumable_jobs(limit=max(1, int(limit)))
        for r in rows:
            r["has_config"] = bool(r.get("has_config"))
        return {"count": len(rows), "jobs": rows, "limit": limit,
                "truncated": len(rows) >= max(1, int(limit))}
    finally:
        h.close()


@app.post("/v1/resumable/clear")
def clear_resumable(job_ids: list[str] | None = Body(None, embed=True)) -> dict:
    """Forget saved crawl frontiers — omit `job_ids` to clear every one (the dashboard's Delete all).

    Only the resume bookkeeping is deleted. Crawled pages already live in Postgres + MinIO and are
    not touched, so this costs re-fetch time on a future run, never data.

    Refused while a batch is running: those rows ARE the live crawl's resume point, and deleting
    them mid-run would silently turn a later stop into a full re-walk."""
    if _batch_status.get("running"):
        raise HTTPException(409, "a batch is running — stop it before clearing resume state")
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    try:
        cleared = h.forget_resumable(job_ids)
    finally:
        h.close()
    return {"cleared": sorted(cleared), "jobs": len(cleared), "rows": sum(cleared.values())}


@app.get("/v1/coverage")
def coverage(host: str, missed: int = 25) -> dict:
    """Cross-check one site against its OWN published sitemap. Read-only, seeds nothing.

    Answers the question "pages > 0" cannot: is this site FINISHED, or did the crawl simply run out
    of links? lockheedmartin.com walked its frontier to exhaustion at 950 URLs and stored 614 — and
    its sitemap lists 2,263, of which 1,457 were never fetched, including every
    /capabilities/electronic-warfare/ page. Nothing failed; the links were not on the pages we saw.

    The verdict matters more than the percentage — see crawler.sitemap.coverage for the four
    different situations that all produce "36%".
    """
    import os
    import sqlite3

    from crawler import sitemap as sm

    # EVERY instance's history, not this one's. All twelve databases sit on the same shared volume
    # (/app/data for instance 0, /app/data/iN for the rest), a site's history can be on any of them,
    # and today's frontier moves spread several across two — `crawl_pages` is deliberately NOT
    # deleted from the source when a job moves, precisely so the seen-set survives. Asking one
    # instance returned "2,263 of 2,263 never fetched" for a site holding 614 pages.
    reg = sm.norm_url(host)
    root = os.environ.get("CRAWLER_DATA_ROOT", "/app/data")
    dbs = [root] + ["%s/i%d" % (root, i) for i in range(2, 13)]
    crawled: set[str] = set()
    for d in dbs:
        p = os.path.join(d, "crawl_history.sqlite")
        if not os.path.exists(p):
            continue
        try:
            c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            crawled.update(sm.norm_url(u) for (u,) in c.execute(
                "SELECT canonical_url FROM crawl_pages WHERE canonical_url LIKE ?", ("%" + reg + "%",)))
            c.close()
        except sqlite3.Error:
            continue                     # a locked or half-written sibling must not fail the check
    out = sm.coverage(host, crawled)
    out["instances_read"] = len(dbs)

    # A sitemap lists whatever its generator felt like listing, calendar exports included. Filtering
    # HERE keeps one definition of junk — the same `_is_action_link` the enqueue and fetch paths use
    # — so nothing downstream needs its own copy, and a "missing pages" count can never be inflated
    # by URLs the crawler would refuse on sight.
    from crawler.async_engine import _is_action_link
    missed_all = out["missed"]
    keep = [u for u in missed_all if not _is_action_link("https://" + u if "://" not in u else u)]
    out["missed_junk"] = len(missed_all) - len(keep)
    out["missed_total"] = len(keep)
    out["missed"] = keep[:max(0, int(missed))]
    out["declared_sitemaps"] = sm.declared_sitemaps(host)
    return out


@app.post("/v1/frontier/prune")
def prune_frontier(apply: bool = Body(False, embed=True)) -> dict:
    """Drop queued URLs the crawler would no longer enqueue — calendar exports, login recursion.

    A tightened link filter is retroactive for nothing: it governs discovery, while the damage is
    done by what is ALREADY in the queue, and a crawl empties its queue before it discovers
    anything. So this applies today's rule to yesterday's frontier.

    The predicate is `async_engine._is_action_link` itself — the exact function the enqueue path
    calls. Handing the storage layer its own copy of "what junk looks like" is how a purge ends up
    deleting URLs the crawler cheerfully re-discovers on the next page.

    Dry by default: `{"apply": true}` to write. Refused while a batch is running, for the reason
    /v1/resumable/clear is — those rows are the live crawl's resume point, and taking them out from
    under the engine races it.
    """
    if apply and _batch_status.get("running"):
        raise HTTPException(409, "a batch is running — stop it before pruning the frontier")
    from crawler.async_engine import _is_action_link
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    try:
        return h.frontier_prune(_is_action_link, apply=bool(apply))
    finally:
        h.close()


@app.post("/v1/crawl/resume")
def resume_crawl(job_ids: list[str] = Body(..., embed=True),
                 background: bool = Body(True, embed=True),
                 forward_to_ingest: bool = Body(True, embed=True),
                 armed: None = Depends(require_armed)) -> dict:
    """Resume stopped crawls by job_id — no other input needed.

    Armed like the other two starts: a resume IS a crawl start, and this route reaches
    `crawl_batch` by a direct call, so the header is only checkable here.

    The job's own definition was stored when it started, so this replays exactly the same crawl
    with resume=True: the pending frontier is reloaded, already-processed URLs go straight into
    `seen` (so they are never re-fetched), and the spent page budget is restored so a resumed job
    cannot overshoot max_pages."""
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    jobs, missing = [], []
    try:
        for jid in job_ids:
            st = h.job_state(jid)
            cfg = (st or {}).get("job_json")
            if not cfg:
                missing.append(jid)
                continue
            try:
                d = json.loads(cfg)
            except Exception:
                missing.append(jid)
                continue
            d["resume"] = True
            jobs.append(d)
        counts = {j["job_id"]: h.frontier_counts(j["job_id"]) for j in jobs}
    finally:
        h.close()
    if not jobs:
        raise HTTPException(400, f"no resumable job config found for: {', '.join(job_ids)}")
    res = crawl_batch(BatchRequest(jobs=[Job(**j) for j in jobs], background=background,
                                   forward_to_ingest=forward_to_ingest))
    # DID IT ACTUALLY START? `crawl_batch` refuses when a previous batch still owns the engine and
    # returns {"started": False, "error": "a batch is already running"} — it does not raise. This
    # used to report every job in `resumed` regardless, so a refused resume looked identical to a
    # successful one.
    #
    # That is the whole "stale stop_pending" bug, seen four separate times: an instance still winding
    # down from a stop holds the batch, the resume is refused, `_arm()` therefore never runs, the
    # .crawl_stop FILE is never cleared — and the file outlives the batch. When the teardown finally
    # ends, the instance sits at running=false / stop_pending=true holding real work, while the
    # orchestrator has already reported "671 resumed, 0 errors". The crawl was never restarted and
    # nothing said so.
    #
    # `background=False` returns {"jobs": n, "results": [...]} and has no `started` key, so absence
    # means it ran synchronously — only an explicit False is a refusal.
    started = res.get("started", True) is not False
    if not started:
        raise HTTPException(409, f"resume refused: {res.get('error') or 'engine busy'} — "
                                 f"the previous batch has not finished on this instance")
    return {"resumed": [j["job_id"] for j in jobs], "missing_config": missing,
            "pending": counts, "started": True, "batch": res}


@app.get("/v1/issues")
def list_issues(limit: int = 200, cause: str | None = None, host: str | None = None) -> dict:
    """Pages the crawler could NOT get, after exhausting its retries — with the specific obstacle.

    Deliberately not the same thing as the per-reason error counters. Those cover everything that
    failed once and was retried; this covers only what is still unresolved after the full ladder,
    which is the list an operator can actually act on. Each row carries the attempt count, so
    "tried 4 times" is distinguishable from "failed once and gave up".

    Filter by `cause` (antibot / captcha / render_timeout / auth_wall / soft_404 …) or `host`."""
    try:
        lg = issues.log()
        return {"summary": lg.summary(), "issues": lg.recent(limit=limit, cause=cause, host=host)}
    except Exception as exc:
        raise HTTPException(500, f"issue log unavailable: {exc}")


@app.post("/v1/issues/clear")
def clear_issue(url: str = "", confirm: str = "") -> dict:
    """Drop one URL from the unresolved list (e.g. after fixing it by hand).

    ?confirm=FLUSH with no url clears the WHOLE list — what the dashboard's Flush button calls.
    Requiring the token rather than treating an empty url as "all" is deliberate: a caller whose
    url variable came back empty would otherwise wipe the list by accident."""
    if confirm == "FLUSH":
        return {"cleared_all": True, "count": issues.log().clear_all()}
    if not url:
        raise HTTPException(400, "pass ?url=… for one URL, or ?confirm=FLUSH to clear the whole list")
    return {"cleared": issues.log().resolve(url), "url": url}


@app.get("/v1/audit")
def audit(limit: int = 100) -> dict:
    """Recent crawl-audit rows for careful (gov/mil) hosts — a provable record of polite crawling:
    what URL, when, as which UA, under which robots decision. Append-only (crawl_audit table)."""
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    try:
        rows = h._conn.execute(
            "SELECT url, host, fetched_at, ua, robots_decision, status, reason, careful "
            "FROM crawl_audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return {"count": len(rows), "rows": [dict(r) for r in rows]}
    finally:
        h.close()


@app.get("/v1/batch/report")
def batch_report(run_id: str = "", host: str = "", urls: int = 500, reason: str = "") -> dict:
    """Per-site (+ per-URL) completion breakdown for a finished batch, read straight from crawl_pages
    scoped by crawl_run_id: kept (error_category NULL) / errored-RETRYABLE / dropped-TERMINAL, bucketed
    by errors.bucket_reason. Unions every crawl_history*.sqlite shard (multiprocess). Blank run_id → the
    last batch's run ids. Read-only open so it never contends with the writer's WAL."""
    import sqlite3
    from urllib.parse import urlsplit

    from crawler import errors

    with _batch_lock:
        run_ids = [run_id] if run_id else list(_batch_status.get("run_ids") or [])
        gate_total = sum(int(r.get("dropped_by_gate", 0) or 0)
                         for r in (_batch_status.get("results") or []))
    if not run_ids:
        # _batch_status only learns its run_ids in the `finally` after the batch returns, so asking
        # it mid-run gets nothing and this endpoint answered "no sites" for the entire life of a
        # crawl. The engine publishes the live one as soon as it has it.
        live = run_state().get("run_id")
        if live:
            run_ids = [live]
    if not run_ids:
        return {"run_ids": [], "sites": [], "totals": {}}

    ph = ",".join("?" * len(run_ids))
    sites: dict = {}
    for dbp in sorted(config.DB_PATH.parent.glob("crawl_history*.sqlite")):
        con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            # fail_count = how many times this URL was actually attempted. Without it the report
            # says "retryable" with no evidence any retry happened, which reads as "gave up on the
            # first try" even when the full C1 -> impersonate -> C3 ladder ran.
            rows = con.execute(
                "SELECT canonical_url, last_status, error_category, "
                "COALESCE(fail_count, 0) AS fail_count, failed_at, "
                # error_detail is newer than some databases; COALESCE keeps an old one working.
                "COALESCE(error_detail, '') AS error_detail FROM crawl_pages "
                f"WHERE crawl_run_id IN ({ph})", run_ids).fetchall()
        except sqlite3.OperationalError:
            rows = []                                    # DB predates the crawl_run_id column
        finally:
            con.close()
        want = (host or "").strip().lower().removeprefix("www.")
        want_reason = (reason or "").strip()
        for r in rows:
            h = (urlsplit(r["canonical_url"]).hostname or "").lower()
            # `host` narrows the whole report to ONE site. The dashboard's drill-down asks about the
            # row that was clicked, and building every site's URL list to answer that cost 23
            # seconds across a 12-instance fleet with 624 sites — for a payload the panel throws
            # away. Matched www-insensitively, because that is one server.
            if want and h.removeprefix("www.") != want:
                continue
            s = sites.setdefault(h, {"host": h, "kept": 0, "retryable": 0,
                                     "terminal": 0, "reasons": {}, "urls": []})
            bucket = errors.bucket_reason(r["error_category"])
            s[bucket] += 1
            if bucket != "kept":
                # COUNTED FIRST, ALWAYS. The histogram has to describe the whole site whatever the
                # caller asked to look at, or clicking a reason would rewrite the numbers beside it.
                s["reasons"][r["error_category"]] = s["reasons"].get(r["error_category"], 0) + 1
                # FILTER BEFORE THE CAP, which is the whole point of this parameter. The URL list is
                # the first N non-kept rows in database order, so a reason with a handful of rows is
                # pushed out by whichever reason happens to dominate: zf.com reported
                # `consent_wall - 2` beside 143 host_busy, and every one of the 60 URLs the panel
                # received was host_busy, so clicking the chip said "0 of 0 URLs in this sample".
                # Filtering here means the cap applies to the rows actually wanted.
                if want_reason and (r["error_category"] or "") != want_reason:
                    continue
                if len(s["urls"]) < max(0, urls):         # cap the drill-down payload per site
                    s["urls"].append({"url": r["canonical_url"], "status": r["last_status"],
                                      "reason": r["error_category"], "bucket": bucket,
                                      "attempts": r["fail_count"], "last_tried": r["failed_at"],
                                      "detail": errors.reason_detail(r["error_category"],
                                                                     r["last_status"]),
                                      # What actually went wrong, verbatim. The category tells you
                                      # the policy; only this tells you the cause.
                                      "error": r["error_detail"] or None})
    out = sorted(sites.values(), key=lambda x: -(x["retryable"] + x["terminal"]))
    tot = {"kept": sum(s["kept"] for s in out), "retryable": sum(s["retryable"] for s in out),
           "terminal": sum(s["terminal"] for s in out), "dropped_by_gate": gate_total}
    return {"run_ids": run_ids, "sites": out, "totals": tot}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    # no-store so the browser always fetches the current dashboard JS (avoids stale-cache
    # bugs where an old page reported wrong counts / ignored the freshness toggle).
    return HTMLResponse(
        dashboard.render(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/v1/generate-jobs")
def generate_jobs_endpoint() -> dict:
    jobs = generate_jobs(_SEED)
    by_type = Counter(j.job_type for j in jobs)
    return {
        "count": len(jobs),
        "by_type_summary": ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())),
        "jobs": [j.model_dump() for j in jobs],
    }


def _arm(job_ids: list[str] | None = None) -> None:
    """Clear any stop left over from a previous run. Called by the /v1/crawl* entry points ONLY —
    an operator asking for a crawl is the one unambiguous "start", and it is the boundary the
    sequential-loop guards below are measured from.

    This has to happen before _dispatch, not inside the runner. The stop signal is a FILE
    (config.DATA_DIR/.crawl_stop) so that child worker processes can see it, which means it outlives
    the container: a stop pressed last week is still set at boot. `_dispatch` refuses to start the
    live phase while a stop is pending, so without this a perfectly ordinary batch would be skipped
    in silence on a freshly started crawler-api. Verified: a restarted container reported
    stop_pending=true with nothing running and no crawl since.

    Same argument applies to the per-job drop set, which is also a file and also outlives the
    container: dispatching a job IS the operator saying "run this one", so it takes that job out of
    the drop set. Without this, a category switched off in March would silently swallow a manual
    batch of the same sites in April, and nothing on screen would say why it captured nothing.
    """
    clear_stop()
    if job_ids:
        remaining = dropped_jobs() - {str(j) for j in job_ids}
        if remaining != dropped_jobs():
            set_dropped_jobs(remaining)


def _dispatch(jobs: list[Job], *, forward: bool) -> list[dict]:
    """Route each job to its tier. A job carrying archive_date IS a C2 job and goes to the archive
    runner; everything else goes to the live engine (C1 + C3).

    Exclusivity is STRUCTURAL, not a flag: a C2 job never reaches AsyncEngine, so no browser is
    launched and C1/C3 cannot run for it even by mistake. Archive jobs are run separately rather
    than interleaved because C2 is hard-limited by archive.org's ~24 req/min cap while the live pool
    wants every core — mixing them in one engine would mean 192 tabs idling on a rate limit.
    """
    c2_jobs = [j for j in jobs if j.is_c2()]
    live_jobs = [j for j in jobs if not j.is_c2()]
    out: list[dict] = []
    if c2_jobs:
        from crawler.c2 import run_c2_batch
        out += run_c2_batch(c2_jobs, forward=forward, seed=_SEED, kp=_KP)
    if live_jobs and not stop_requested():
        # `not stop_requested()` because the two phases are SEQUENTIAL and the live runner clears the
        # stop flag as it starts. A stop pressed during the archive phase would otherwise be wiped on
        # the way into the live phase and the batch would carry on — the operator sees "stopping…",
        # the archive jobs halt, and then a browser pool spins up anyway.
        # run_batch_multiprocess falls back to run_batch_async for 1 worker / 1 job, so this is the
        # single dispatch path for every live job.
        out += run_batch_multiprocess(live_jobs, forward=forward,
                                      seed=_SEED, kp=_KP)
    # Restore REQUEST order — splitting by tier reorders, and callers (and the batch-status table)
    # line results up against the jobs they sent.
    pos = {j.job_id: i for i, j in enumerate(jobs)}
    out.sort(key=lambda r: pos.get(r.get("job_id"), len(pos)))
    return out


@app.post("/v1/crawl")
def crawl(req: CrawlRequest, force: bool = False, armed: None = Depends(require_armed)) -> dict:
    """Single job via async engine (same backend as /v1/crawl/batch).

    `armed` is the lock (see require_armed) — evaluated by FastAPI before this body runs.

    Single-flight, sharing ONE claim with /v1/crawl/batch. This route used to have no guard at
    all while calling `_arm()`, which clears the stop FILE — so a second single crawl started
    beside a running one and pressing Stop during the overlap halted nothing.
    """
    job = Job(**req.model_dump(exclude={"forward_to_ingest"}))
    _claim = _crawl_claim.acquire(route="/v1/crawl", job_ids=[job.job_id], force=force,
                                  probe=lambda: _crawl_progress([job.job_id]))
    if not _claim["granted"]:
        raise HTTPException(409, _refusal(_claim))
    # _arm AFTER the claim is won, never before: a refused request must not clear the stop file
    # out from under the crawl that is actually running.
    _arm([job.job_id])
    # Mark running so the dashboard STOP button lights up for single-job / Run-All-Jobs runs too
    # (Run All loops single crawls). The stop signal itself works for any run — the engine polls it.
    with _batch_lock:
        _batch_status.update(running=True, total=1, done=0, current_job=job.job_id)
    try:
        results = _dispatch([job], forward=req.forward_to_ingest)
    finally:
        _crawl_claim.release()      # FIRST and alone — nothing that can raise before it
        with _batch_lock:
            _batch_status.update(running=False, done=1)
    return results[0] if results else {"job_id": job.job_id, "summary": {}, "documents": []}


class BatchRequest(BaseModel):
    jobs: list[Job]
    forward_to_ingest: bool = False
    background: bool = False   # dispatch on a daemon thread + return at once (poll /v1/batch/status)


@app.post("/v1/crawl/batch")
def crawl_batch(req: BatchRequest, force: bool = False,
                armed: None = Depends(require_armed)) -> dict:
    """Batch crawl via async engine (8 browsers × 12 tabs = 96 concurrent pages).

    `armed` is the lock (see require_armed), evaluated by FastAPI before this body runs. It is a
    parameter rather than a route-level `dependencies=[...]` so `resume_crawl` can keep calling
    this function directly (it passes the lock at its own route); the default is then inert.

    background=True dispatches on a daemon thread and returns immediately. Crawls now run to
    frontier-exhaustion (no wall-clock chop), which can take hours — poll /v1/batch/status or
    /v1/metrics for progress and POST /v1/crawl/stop to halt. Single-flight: one batch at a time,
    because the engine stop flag and _batch_status are process globals (valid while run.py runs a
    single uvicorn worker)."""
    _ids = [j.job_id for j in req.jobs]
    _claim = _crawl_claim.acquire(route="/v1/crawl/batch", job_ids=_ids, force=force,
                                  probe=lambda: _crawl_progress(_ids))
    if not _claim["granted"]:
        # RETURNED, not raised: resume_crawl reads `started is False` to tell a refusal from a
        # synchronous run and raises its own 409 with the job list attached. Changing this to a
        # raise would make every refused resume look like a transport error instead.
        return {"started": False, "error": _refusal(_claim),
                "reason": _claim["reason"], "detail": _claim["detail"]}
    with _batch_lock:
        # `_batch_status` is now bookkeeping for the DASHBOARD only. It is no longer the thing
        # that decides whether a second crawl may start — `_crawl_claim` above is, and it covers
        # /v1/crawl too, which this flag never did.
        #
        # The history is worth keeping, because each step was a real outage:
        #   * the guard was once gated on `req.background`, so the synchronous path — the one the
        #     orchestrator's manual batch actually uses (`run_manual_batch` sends background:False)
        #     — walked straight past it. Two concurrent sync batches share one `_batch_status`:
        #     the second overwrites the first's totals, and each calls `_arm()`, which CLEARS THE
        #     STOP FILE, so pressing stop during the overlap halted nothing.
        #   * guarding on the flag alone was worse: a flag left set by a thread that died refused
        #     every future batch until the container restarted. Observed twice.
        #   * so it became `owner.is_alive()` — which fixed the dead thread and could not see the
        #     live one. A dispatch blocked on `.result()` for a jammed host is alive for ever, and
        #     that guard has no way back from it. See crawlclaim.py.
        _batch_status.update(running=True, total=len(req.jobs), done=0,
                             current_job="(async pool)", results=[],
                             owner=threading.current_thread())
    _arm(_ids)   # after the claim is won: a rejected request must not disarm a live crawl

    def _run() -> list[dict]:
        results: list[dict] = []
        try:
            # Routes archive (C2) jobs to the archive runner and live jobs to the browser pool
            # (multi-process when CRAWLER_WORKERS>1, else single-process).
            results = _dispatch(req.jobs, forward=req.forward_to_ingest)
        finally:
            # Clear `running` FIRST and on its own.
            #
            # This used to be one update() whose `results=[...]` comprehension was evaluated as an
            # ARGUMENT — so a single result missing summary["fetched"] raised before the flag was
            # cleared, the thread died silently, and the API refused every subsequent batch with
            # "a batch is already running" until the container was restarted. Observed twice: a
            # stopped crawl sat at running=True with no crawl traffic in the logs for minutes.
            # The flag is the thing that must never be left set, so nothing that can raise is
            # allowed to happen before it is cleared.
            _crawl_claim.release()   # FIRST and alone — see crawlclaim.release()
            with _batch_lock:
                _batch_status.update(running=False, done=len(req.jobs))
            try:
                rows = []
                for r in results:
                    s = (r or {}).get("summary") or {}
                    rows.append({"job_id": (r or {}).get("job_id", ""),
                                 "fetched": s.get("fetched", 0), "kept": s.get("kept", 0),
                                 "sent": s.get("sent", 0), "accepted": s.get("accepted", 0),
                                 "skipped": s.get("skipped", 0),
                                 "recovered": s.get("recovered", 0),
                                 "not_modified_304": s.get("not_modified_304", 0),
                                 "skipped_unchanged": s.get("skipped_unchanged", 0),
                                 "skipped_duplicate": s.get("skipped_duplicate", 0),
                                 "dropped_by_gate": s.get("dropped_by_gate", 0)})
                run_ids = sorted({((r or {}).get("summary") or {}).get("crawl_run_id")
                                  for r in results
                                  if ((r or {}).get("summary") or {}).get("crawl_run_id")})
                with _batch_lock:
                    _batch_status.update(run_ids=run_ids, results=rows)
            except Exception:
                log.exception("batch summary build failed — status flag already cleared")
        return results

    if req.background:
        t = Thread(target=_run, daemon=True)
        # Record the OWNER before starting it, so a status poll that lands between start() and the
        # thread's first instruction still sees a live owner rather than a stale-looking flag.
        with _batch_lock:
            _batch_status["owner"] = t
        # ...and hand the CLAIM to the same thread, or it looks stale the instant this request
        # returns and the next crawl is granted straight past a live batch.
        _crawl_claim.set_owner(t)
        t.start()
        return {"started": True, "background": True, "total": len(req.jobs)}
    results = _run()
    return {"jobs": len(results), "results": results}


@app.post("/v1/history/flush")
def history_flush(confirm: str = "") -> dict:
    """Forget the crawl history for EVERY host — content hashes and fetched URLs alike.

    Separate from /v1/history/forget on purpose. That one refuses an empty host ("refusing to
    forget everything") because forgetting one site is routine and forgetting all of them is not;
    collapsing the two would make a typo in a host name silently wipe the machine. This endpoint
    exists so the dashboard's Flush button has an honest, explicit way to ask for exactly that, and
    it demands ?confirm=FLUSH.

    What this costs if you get it wrong: every URL is re-fetched from scratch on the next crawl,
    because "unchanged" is decided by comparing against the content_hash stored here. That is the
    POINT after a data wipe — deleting the documents without this leaves the crawler convinced it
    already has every page, so the next crawl stores nothing.

    Clears `crawl_pages` (the canonical-url → content_hash memory) and `crawl_audit` (the per-fetch
    log). It deliberately does NOT touch `host_render` and `host_careful`: those are hints the
    crawler LEARNED about how a host must be fetched — needs a browser, goes weak under load — not
    records of what was collected. Dropping them makes the next crawl slower and no more correct,
    and they are not what "flush the data" means. `crawl_frontier`/`crawl_job_state` are the resume
    position and have their own endpoint (/v1/resumable/clear), which the Flush button also calls.

    Returns per-table counts. A machine that has never crawled has no tables yet; that is "nothing
    to forget", not an error.
    """
    import sqlite3

    from crawler import config

    if confirm != "FLUSH":
        raise HTTPException(400, "pass ?confirm=FLUSH — this forgets every URL this machine has crawled")
    wiped: dict[str, int] = {}
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        for table in ("crawl_pages", "crawl_audit"):
            try:
                wiped[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                wiped[table] = 0
        conn.commit()
    finally:
        conn.close()
    n = wiped.get("crawl_pages", 0)
    return {"forgotten": n, "tables": wiped,
            "detail": f"forgot {n} URL(s) — the next crawl re-fetches everything"}


@app.post("/v1/history/forget")
def history_forget(host: str = "", apply: bool = False) -> dict:
    """Forget a host's crawl history so the next crawl re-fetches it from scratch.

    Deleting a site's documents is not enough on its own: crawl_pages still holds each URL's
    content_hash, so the next crawl classifies every page "unchanged" and skips it — you would
    delete a site and then be unable to re-collect it. This is the other half of that operation.

    `apply=false` (the default) is a dry run returning the count, so the caller can show the
    operator a real number before anything is destroyed.

    Matching is by parsed hostname, not a LIKE on the URL: 'bmw.com' as a pattern would also match
    'notbmw.com/...' and 'evil.com/?u=bmw.com', silently wiping unrelated history.
    """
    import sqlite3
    from urllib.parse import urlsplit

    from crawler import config

    want = (host or "").strip().lower().removeprefix("www.")
    if not want:
        raise HTTPException(400, "host is required — refusing to forget everything")

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        try:
            rows = conn.execute("SELECT canonical_url FROM crawl_pages").fetchall()
        except sqlite3.OperationalError as exc:
            # A machine that has never crawled has no history table yet — which is exactly the
            # state every freshly-provisioned node is in. "Nothing to forget" is the honest
            # answer; a 500 here reads as a broken endpoint on a perfectly healthy new box.
            if "no such table" not in str(exc).lower():
                raise
            rows = []
        doomed = []
        for (u,) in rows:
            h = (urlsplit(u).hostname or "").lower().removeprefix("www.")
            if h == want or h.endswith("." + want):
                doomed.append(u)
        if apply and doomed:
            for i in range(0, len(doomed), 500):          # sqlite caps variables per statement
                chunk = doomed[i:i + 500]
                q = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM crawl_pages WHERE canonical_url IN ({q})", chunk)
            conn.commit()
    finally:
        conn.close()
    return {"host": want, "matched": len(doomed), "forgotten": len(doomed) if apply else 0,
            "applied": apply}


@app.get("/v1/auth/sessions")
def auth_sessions() -> dict:
    """Which hosts the crawler currently holds a login for.

    Cookie VALUES are session credentials and are never returned — only a count, the account
    address, and whether the session is still inside its TTL. Check this before believing a
    tender crawl was authenticated: a portal that rejected the signup leaves an `error` here and
    the crawl silently continues anonymously (by design — one portal must not fail a batch).
    """
    from crawler import siteauth
    return {"sessions": siteauth.all_sessions()}


@app.post("/v1/auth/session")
async def auth_session(url: str, force: bool = False) -> dict:
    """Register + log in to a portal now, instead of waiting for the next crawl to do it.

    `force=true` discards a stored session and registers a NEW account. Use it when a portal has
    expired or banned the old one — not routinely: every force leaves another dead account behind
    on the portal, which is what gets an IP blocked.
    """
    from crawler import siteauth
    if not (url or "").strip():
        raise HTTPException(400, "url is required")
    rec = await siteauth.establish(url) if force else (
        {"host": siteauth.host_of(url), "reused": True}
        if siteauth.is_fresh(siteauth.get_session(url)) else await siteauth.establish(url))
    return {"ok": siteauth.is_fresh(siteauth.get_session(url)), "session": rec}


class CredentialsIn(BaseModel):
    host: str
    email: str
    password: str


@app.post("/v1/auth/credentials")
def auth_credentials(body: CredentialsIn) -> dict:
    """Pre-seed an account for a host, so the crawler logs in instead of registering.

    This is the path that actually works on the big Indian tender aggregators: they do not offer
    a self-service email signup (tender18's /register is a sales enquiry form with no password
    box; tenderdetail answers every unknown path with its listing page). Give it a login you were
    issued and the crawl authenticates from the next job onward.

    Body, not query params, deliberately — a password in a URL lands in access logs.
    """
    from crawler import siteauth
    if not (body.host and body.email and body.password):
        raise HTTPException(400, "host, email and password are all required")
    return siteauth.set_credentials(body.host, body.email, body.password)


@app.delete("/v1/auth/session")
def auth_forget(host: str, confirm: str = "") -> dict:
    """Drop a stored session. Needs ?confirm=FORGET — the next crawl of that host then registers
    a fresh account, which is a visible action on someone else's site, not a local cache clear."""
    from crawler import siteauth
    if confirm != "FORGET":
        raise HTTPException(400, "pass ?confirm=FORGET — this makes the next crawl register anew")
    return {"host": host, "forgotten": siteauth.forget(siteauth.host_of(host))}


@app.post("/v1/crawl/stop")
def crawl_stop() -> dict:
    """Halt the running crawl — signals the shared engine to tear down. Stops BOTH the live
    C1 render pool and the C3 CamoFox fallback (the engine drives every tier), so once pressed
    all crawling ceases. Idempotent: safe to call when nothing is running. The in-flight batch
    returns whatever it captured before the stop."""
    request_stop()
    with _batch_lock:
        was_running = bool(_batch_status.get("running"))
        if was_running:
            _batch_status["current_job"] = "(stopping…)"
    return {"stopping": was_running, "message": "stop signaled" if was_running else "no active batch"}


@app.post("/v1/crawl/drop")
def crawl_drop(job_ids: list[str] = Body(default=[], embed=True)) -> dict:
    """Stop working on SPECIFIC jobs while the rest of the batch carries on — the per-category stop.

    `job_ids` is the complete set, not a delta: send it in full every time and an empty list clears
    it. Idempotent by construction, which matters because the caller (the orchestrator's category
    switch) recomputes "every job in a switched-off category" from its own database and has no way
    to know what this instance was told last.

    Dropped URLs are released UNPROCESSED, so their durable frontier rows stay `pending` and the
    jobs end `stopped` rather than `completed` — switching the category back on resumes them where
    they were. A job the operator explicitly dispatches leaves the set (see `_arm`).
    """
    s = set_dropped_jobs(job_ids)
    running = bool(_batch_status.get("running"))
    return {"dropped": sorted(s), "count": len(s), "batch_running": running,
            "message": ("%d job(s) will stop as their in-flight pages finish; their queued URLs "
                        "stay pending and resumable" % len(s)) if s and running
                       else ("drop set cleared" if not s else
                             "%d job(s) held off; nothing is running here right now" % len(s))}


@app.get("/v1/crawl/drop")
def crawl_drop_get() -> dict:
    s = dropped_jobs()
    return {"dropped": sorted(s), "count": len(s)}


@app.get("/v1/batch/status")
def batch_status() -> dict:
    with _batch_lock:
        out = dict(_batch_status)
    # `owner` is a Thread object held only to tell a live batch from a stale flag. It is not JSON
    # and must never reach a caller — leaving it in makes this whole endpoint 500, which takes the
    # dashboard down with it.
    out.pop("owner", None)
    # The claim is what actually refuses a second crawl; showing it makes a wedge visible on the
    # dashboard instead of only in a 409 nobody sees.
    out["claim"] = _crawl_claim.status()
    # The RAW stop flag, ungated by `running` (unlike /v1/metrics' `stopping`, which is only true
    # while a batch is actually up). Between two jobs of a Run-All loop nothing is running, so the
    # gated form reads false and the loop launches the next job — which is the one place a stop
    # signalled from OUTSIDE the crawler dashboard (the orchestrator's master switch posts straight
    # to /v1/crawl/stop) could be missed.
    out["stop_pending"] = stop_requested()
    # A PARTIAL stop is a real state and has to be visible. Without this the fleet view shows a
    # crawl "running" with no hint that half its jobs were switched off by category, which reads as
    # the switch having done nothing — the exact confusion this endpoint's caller is trying to end.
    out["dropped_jobs"] = sorted(dropped_jobs())
    # The seed hosts of the LIVE run. Without these a caller can only learn which sites are in a
    # batch from the per-site report, which needs at least one FINISHED url per host — so for the
    # opening minutes of every crawl the honest answer to "what is it crawling" was an empty list,
    # and the dashboard rendered that as "nothing crawling right now".
    rs = run_state()
    out["run_id"], out["hosts"] = rs["run_id"], rs["hosts"]
    return out


class CheckKeywordsRequest(BaseModel):
    url: str
    keywords: list[str]
    render_js: bool = False


@app.post("/v1/check-keywords")
def check_keywords(req: CheckKeywordsRequest) -> dict:
    """Fetch one URL and report which keywords appear on it — the same
    word-boundary FlashText matching the Stage-2 gate uses. A relevance
    probe only: no BFS crawl, no dedup writes, no asset capture, no ingest
    POST. See scripts/check_keywords.py."""
    result = _check_keywords(req.url, req.keywords, render_js=req.render_js)
    result.pop("_text", None)
    result.pop("_title", None)
    return result


class CheckKeywordsBatchRequest(BaseModel):
    urls: list[str]
    keywords: list[str]
    render_js: bool = False
    parallel: int = 6


@app.post("/v1/check-keywords/batch")
def check_keywords_batch(req: CheckKeywordsBatchRequest) -> dict:
    """Probe MANY URLs against one keyword set in a single call — cheaply filter
    a candidate seed-URL list before committing crawl budget. Runs the same
    single-URL probe concurrently; results in input order."""
    from concurrent.futures import ThreadPoolExecutor

    def _probe(u: str) -> dict:
        r = _check_keywords(u, req.keywords, render_js=req.render_js)
        r.pop("_text", None)
        r.pop("_title", None)
        return r

    urls = req.urls
    if not urls:
        return {"count": 0, "results": []}
    workers = max(1, min(req.parallel, len(urls)))
    if workers == 1:
        results = [_probe(u) for u in urls]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_probe, urls))
    return {"count": len(results),
            "relevant": [r["url"] for r in results if r.get("matched")],
            "results": results}


class ProbeSitesRequest(BaseModel):
    urls: list[str]
    parallel: int = 32
    timeout_s: float = 8.0


@app.post("/v1/probe-sites")
def probe_sites(req: ProbeSitesRequest) -> dict:
    """Classify a list of candidate sites in ONE cheap fetch each, and recommend the two Job
    params the operator otherwise has to guess: `keyword` (pure site -> false/capture everything,
    mixed publisher -> true) and `render_js`, plus `path_scope` when the seed URL already names a
    section. See crawler/probe.py for the measurement rationale — one fetch caps accuracy near
    ~86%, so every result carries a confidence band and `review_required` for operator triage.

    Sweep-cheap: no robots, no delay, no retries, no PDF, no Playwright, no dedup, no ingest.
    Measured: 66 URLs in 4.1s at parallel=32. Results in input order."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from crawler.probe import probe_site

    urls = req.urls
    if not urls:
        return {"count": 0, "elapsed_ms": 0, "results": []}

    def _one(u: str) -> dict:
        return probe_site(u, corpus_kp=_KP, timeout_s=req.timeout_s)

    t0 = time.monotonic()
    workers = max(1, min(req.parallel, len(urls)))
    if workers == 1:
        results = [_one(u) for u in urls]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_one, urls))
    return {"count": len(results), "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "results": results}


class SuggestJobRequest(BaseModel):
    url: str
    target_entity: str | None = None
    job_type: str = "news"
    render_js: bool = False
    max_pages: int = 40
    max_depth: int = 2
    hunt_mode: str | None = None   # "focused" (probe-then-crawl) | "exhaustive"; None = manual budget


@app.post("/v1/suggest-job")
def suggest_job(req: SuggestJobRequest) -> dict:
    """Build a ready-to-run Job with probe-adaptive keywords: assemble a broad
    candidate pool from the seed (entity aliases + products + tech-domain terms),
    probe the URL, and keep only the pool terms that actually appear there. The
    returned job can be POSTed straight to /v1/crawl. If nothing hits, the seed
    is irrelevant for this pool — `keywords` comes back empty and `relevant` is
    false (skip the crawl)."""
    pool = _candidate_pool(_SEED, req.target_entity, req.job_type)
    disc = _discover_keywords(req.url, pool, render_js=req.render_js)
    selected = disc["selected_keywords"]
    job_kwargs: dict = dict(
        job_id=f"suggested_{req.job_type}_{(req.target_entity or 'x').lower()}",
        job_type=req.job_type, seed_urls=[req.url],
        keywords=selected, target_entity=req.target_entity, render_js=req.render_js,
    )
    if req.hunt_mode:
        # Let the hunt-mode preset own the crawl budget (don't pin max_pages/depth,
        # or model_fields_set would block the preset from filling them).
        job_kwargs["hunt_mode"] = req.hunt_mode
    else:
        job_kwargs["max_pages"] = req.max_pages
        job_kwargs["max_depth"] = req.max_depth
    job = Job(**job_kwargs)
    return {
        "relevant": bool(selected),
        "pool_size": disc["pool_size"],
        "selected_keywords": selected,
        "probe": {"status": disc["status"], "title": disc.get("title"),
                  "error": disc["error"]},
        "job": job.model_dump(),
    }


@app.get("/v1/schema")
def schema() -> dict:
    """The wire contract Layer 2 integrates against: the job input shape and
    the document output shape (JSON Schema)."""
    return {
        "job_input": Job.model_json_schema(),
        "document_output": Document.model_json_schema(),
        "ingest_endpoint": config.INGEST_API_PREFIX + "/page",
        "ingest_bundle": "{ \"document\": {...} }",
    }
