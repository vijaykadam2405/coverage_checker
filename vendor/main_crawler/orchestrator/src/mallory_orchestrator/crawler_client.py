"""Talk to the crawler worker (Layer 1) at :8099 — dispatch jobs and read live status.

The crawler persists crawled docs to its OWN Postgres + MinIO when the job carries
forward_to_ingest:true, so the orchestrator only dispatches and records run history —
there is no downstream L2 forwarding anymore (L2 was removed from the stack).
"""

from __future__ import annotations

import os

import httpx

from .config import get_settings

_s = get_settings()

# ── arming ──────────────────────────────────────────────────────────────────
# Once CRAWLER_ARM_TOKEN is set on a crawler instance, its crawl-STARTING routes (/v1/crawl,
# /v1/crawl/batch, /v1/crawl/resume) refuse any request that does not carry the same secret in
# this header (crawler_api/app.py `require_armed`). This module is the ONE legitimate sender, so
# every dispatcher — the scheduler tick, the daily lane, refill, the autopilot's assign, a pasted
# batch, the campaign launcher — is armed by calling the three functions below and nothing else
# has to know. No other module may talk to a crawl route (tests/test_arm_token.py pins this).
#
# Read live from the environment, not from the Settings snapshot — see config.daily_instance_list
# for why. Unset or empty = no header: fine against an instance with no token (open), a 403
# against one that has — so on rollout set THIS side first, then the instances.
ARM_HEADER = "X-Mallory-Arm"


def _arm_headers() -> dict[str, str]:
    tok = os.environ.get("CRAWLER_ARM_TOKEN", "").strip()
    return {ARM_HEADER: tok} if tok else {}


def _b(base: str | None) -> str:
    """The instance to talk to. None means instance 0 — the single crawler this module addressed
    before the fleet existed, so every un-updated caller keeps working unchanged."""
    return (base or _s.crawler_api).rstrip("/")


def pool() -> list[str]:
    """Every crawler instance, in INSTANCE ORDER.

    The index into this list is the instance number recorded in `domain_shards`, so the order is
    data, not presentation: reordering it routes sites to a container that does not hold their
    frontier. Unset (or a single entry) collapses to exactly the pre-fleet behaviour.
    """
    raw = (_s.crawler_api_pool or "").strip()
    if not raw:
        return [_b(None)]
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def fanout(fn, *args, **kwargs) -> list[dict]:
    """Call `fn` against every instance and return one row per instance, in order.

    Never raises and never silently drops an instance: a failure becomes {"ok": False, "error":...}
    for that index. A merge that quietly skipped an unreachable instance would report a SMALLER
    fleet total and read as "the crawl slowed down" rather than "a container is down" — the exact
    class of lie this dashboard's one rule exists to prevent.
    """
    bases = pool()
    if len(bases) == 1:
        try:
            return [{"instance": 0, "base": bases[0], "ok": True, "data": fn(*args, **kwargs)}]
        except Exception as e:  # noqa: BLE001
            return [{"instance": 0, "base": bases[0], "ok": False, "error": str(e)[:200]}]

    # CONCURRENT. Sequentially, a dashboard poll costs N x the slowest instance: measured at 12
    # instances, the per-site report took 43 SECONDS and the panel timed out. These are independent
    # HTTP calls to different containers, so the whole fan-out should cost one round trip, not
    # twelve. Threads (not asyncio) because every crawler_client function is synchronous httpx and
    # the callers are sync FastAPI handlers.
    from concurrent.futures import ThreadPoolExecutor

    def _one(item):
        i, base = item
        try:
            return {"instance": i, "base": base, "ok": True, "data": fn(*args, **kwargs, base=base)}
        except Exception as e:  # noqa: BLE001
            return {"instance": i, "base": base, "ok": False, "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=min(16, len(bases))) as ex:
        return list(ex.map(_one, enumerate(bases)))


def fanout_one(i: int, base: str, fn, *args, **kwargs) -> dict:
    """One instance, in the SAME row shape fanout() returns.

    So a caller that knows which instance owns the work can skip the fan-out without also needing a
    second code path to unpack the result — the row shape, including the {"ok": False} error form,
    is what every merge downstream is written against.
    """
    try:
        return {"instance": i, "base": base, "ok": True, "data": fn(*args, **kwargs, base=base)}
    except Exception as e:  # noqa: BLE001
        return {"instance": i, "base": base, "ok": False, "error": str(e)[:200]}


def dispatch(job: dict, timeout: float | None = None, base: str | None = None) -> dict:
    """POST one job to the crawler API and return its {summary, documents, ...}.

    Read timeout defaults to settings.dispatch_timeout (a crawl runs to frontier
    exhaustion — a short timeout would spuriously fail long scheduled jobs).
    """
    t = httpx.Timeout(connect=10.0, read=(timeout or _s.dispatch_timeout), write=30.0, pool=10.0)
    with httpx.Client(timeout=t) as c:
        r = c.post(f"{_b(base)}/v1/crawl", json=job, headers=_arm_headers())
        r.raise_for_status()
        return r.json()


def get_metrics(base: str | None = None) -> dict:
    """Live crawler metrics for the Control Panel (GET /v1/metrics)."""
    with httpx.Client(timeout=10.0) as c:
        r = c.get(f"{_b(base)}/v1/metrics")
        r.raise_for_status()
        return r.json()


def stop_crawl(base: str | None = None) -> dict:
    """Signal the crawler to halt the running batch (POST /v1/crawl/stop)."""
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{_b(base)}/v1/crawl/stop")
        r.raise_for_status()
        return r.json()


def drop_jobs(job_ids: list[str], base: str | None = None) -> dict:
    """Stop specific jobs without touching the rest of the batch (POST /v1/crawl/drop).

    The COMPLETE set every time — the crawler replaces rather than merges, so an instance that
    missed an earlier call still converges on the right answer at the next one."""
    with httpx.Client(timeout=15.0) as c:
        r = c.post(f"{_b(base)}/v1/crawl/drop", json={"job_ids": list(job_ids)})
        r.raise_for_status()
        return r.json()


def batch_status(base: str | None = None) -> dict:
    """Whether the crawler is mid-batch (GET /v1/batch/status) — the scheduler's busy-gate."""
    with httpx.Client(timeout=5.0) as c:
        r = c.get(f"{_b(base)}/v1/batch/status")
        r.raise_for_status()
        return r.json()


def probe_sites(urls: list[str], parallel: int = 32, timeout_s: float = 8.0, base: str | None = None) -> dict:
    """Classify sites (pure-defence vs mixed, needs-JS) BEFORE generating jobs — POST /v1/probe-sites.
    Generous read timeout: ~300 URLs probe in ~20s, but a slow batch must not fail the review step."""
    t = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    with httpx.Client(timeout=t) as c:
        r = c.post(f"{_b(base)}/v1/probe-sites",
                   json={"urls": urls, "parallel": parallel, "timeout_s": timeout_s})
        r.raise_for_status()
        return r.json()


def get_camofox(base: str | None = None) -> dict:
    """C3 CamoFox stealth-engine status (GET /v1/camofox) for the Control Panel indicator."""
    with httpx.Client(timeout=8.0) as c:
        r = c.get(f"{_b(base)}/v1/camofox")
        r.raise_for_status()
        return r.json()


def get_tiers(base: str | None = None) -> dict:
    """C1/C2/C3/C4 fetch-tier health (GET /v1/tiers) for the dashboard's tier dots.

    The crawler caches this for 30s server-side, so polling it costs nothing — the probes are real
    network calls and must not run once per dashboard refresh.
    """
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{_b(base)}/v1/tiers")
        r.raise_for_status()
        return r.json()


def batch_report(run_id: str = "", host: str = "", urls: int = 500,
                 reason: str = "", base: str | None = None) -> dict:
    """Per-host crawl outcome (GET /v1/batch/report): kept / retryable / terminal, the reason
    histogram, and the individual URLs that did not resolve. This is the ONLY place that knows why
    a URL failed, so the Logs tab is built on it."""
    with httpx.Client(timeout=30.0) as c:
        p = {"urls": urls}
        if run_id:
            p["run_id"] = run_id
        if host:
            p["host"] = host
        if reason:
            # Narrowing happens crawler-side, before it caps the list. Filtering here instead would
            # be filtering what survived the cap, which is how a 2-row reason came back empty.
            p["reason"] = reason
        r = c.get(f"{_b(base)}/v1/batch/report", params=p)
        r.raise_for_status()
        return r.json()


def issues(limit: int = 500, cause: str = "", host: str = "", base: str | None = None) -> dict:
    """Pages still unresolved after the crawler exhausted its retries (GET /v1/issues).

    Distinct from batch_report(): that is written when a batch FINISHES, so during a long crawl it
    says nothing. This log is live and survives restarts, and each row carries the attempt count
    and the specific obstacle — which is what makes the row actionable rather than just a failure."""
    params: dict = {"limit": limit}
    if cause:
        params["cause"] = cause
    if host:
        params["host"] = host
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{_b(base)}/v1/issues", params=params)
        r.raise_for_status()
        return r.json()


def resumable(limit: int = 1000, base: str | None = None) -> dict:
    """Crawls cut short that can be picked up where they left off (GET /v1/resumable)."""
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{_b(base)}/v1/resumable", params={"limit": limit})
        r.raise_for_status()
        return r.json()


def resume(job_ids: list[str], background: bool = True, forward_to_ingest: bool = True, base: str | None = None) -> dict:
    """Re-dispatch stopped crawls by job_id (POST /v1/crawl/resume) — the crawler still holds each
    job's stored definition, so nothing else has to be re-supplied."""
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{_b(base)}/v1/crawl/resume",
                   json={"job_ids": job_ids, "background": background,
                         "forward_to_ingest": forward_to_ingest},
                   headers=_arm_headers())
        r.raise_for_status()
        return r.json()


def clear_resumable(job_ids: list[str] | None = None, base: str | None = None) -> dict:
    """Forget saved frontiers (POST /v1/resumable/clear); None = all of them."""
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{_b(base)}/v1/resumable/clear", json={"job_ids": job_ids})
        r.raise_for_status()
        return r.json()


def coverage(host: str, missed: int = 25, base: str | None = None) -> dict:
    """Cross-check one site against its own sitemap (GET /v1/coverage). Read-only; seeds nothing.

    Long read timeout: this fetches robots.txt plus a sitemap index and can walk child sitemaps.
    """
    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)) as c:
        r = c.get(f"{_b(base)}/v1/coverage", params={"host": host, "missed": missed})
        r.raise_for_status()
        return r.json()


def prune_frontier(apply: bool = False, base: str | None = None) -> dict:
    """Drop queued URLs the crawler would no longer enqueue (POST /v1/frontier/prune).

    Generous read timeout: this scans every pending row in an instance's sqlite, and the instances
    worth pruning are by definition the ones holding five figures of them.
    """
    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)) as c:
        r = c.post(f"{_b(base)}/v1/frontier/prune", json={"apply": apply})
        r.raise_for_status()
        return r.json()


def dispatch_batch(payload: dict, timeout: float | None = None, base: str | None = None) -> dict:
    """POST a raw batch ({jobs:[...]}) to the crawler (POST /v1/crawl/batch) — the manual-batch box.

    Read timeout defaults to 30s because callers usually force background:true (the crawler returns
    at once). Pass `timeout` when dispatching a chunk SYNCHRONOUSLY (background:false) so the call
    can wait out a crawl that runs to frontier exhaustion — see orchestrate.run_manual_batch.
    """
    t = httpx.Timeout(connect=10.0, read=(timeout or 30.0), write=30.0, pool=10.0)
    with httpx.Client(timeout=t) as c:
        r = c.post(f"{_b(base)}/v1/crawl/batch", json=payload, headers=_arm_headers())
        r.raise_for_status()
        return r.json()
