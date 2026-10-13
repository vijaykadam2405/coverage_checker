"""LIVENESS - is every moving part of the crawler stack up and reachable?

This is the foundational area. Everything the other suites assert (tiers, coverage,
delta, dashboards, logic) is meaningless if the services underneath are down: a
freshness check against an unreachable Postgres, or a tier probe against a dead
crawler-api, produces noise, not a diagnosis. So `services` runs first and its
truly-fundamental checks are marked ``critical=True`` - when ingest, the
orchestrator, or Postgres is down, a failure here is meant to shout, and the
runner treats a critical failure as failing the whole run so the downstream
red is understood as consequence rather than cause.

What each check proves maps to a real endpoint or column, read from the source:

  * ingest      -> GET /health (crawler/ingest_api/app.py) + GET /stats
  * orchestrator-> GET /health (orchestrator/.../main.py - in the auth _OPEN allowlist,
                  so it answers before any login and is the cheapest real endpoint)
  * crawler-api -> GET /health and GET /v1/metrics (crawler/crawler_api/app.py)
  * Postgres    -> SELECT 1 / count(*) via hc.pg (the ONE `documents` table,
                  crawler/ingest_api/pgstore.py - never the TOAST body columns)
  * MinIO       -> the raw blob bucket exists (docker-compose.yml: mallory-raw)
  * CamoFox     -> GET /health on the C3 render server (crawler/.../camofox_client.py)

Rules obeyed here: read-only only; never SELECT main_text/html; every call is
bounded by a timeout; anything optional or not-configured raises hc.Skip rather
than FAIL, so a legitimately ndjson-only or CamoFox-less deployment reads green.
"""
from __future__ import annotations

import socket

from .. import hc


# -- ingest landing (Postgres + ndjson + dashboard) ----------------------------
@hc.check(area="services", name="ingest_health", critical=True)
def _ingest_health() -> str:
    """The ingest API - the endpoint every crawl POSTs each kept page to - answers
    /health with status ok. If this is down, nothing captured is landing anywhere."""
    d = hc.http_json(f"{hc.INGEST_BASE}/health", timeout=10)
    status = d.get("status")
    hc.need(status == "ok", f"/health status={status!r} (expected 'ok'): {d}")
    # accepted/rejected are lifetime in-process counters - reset on restart, so a 0
    # is not an alarm; they are reported only as a sign the process is really serving.
    return f"status=ok accepted={d.get('accepted')} rejected={d.get('rejected')}"


@hc.check(area="services", name="ingest_stats", critical=False)
def _ingest_stats() -> str:
    """/stats is reachable and parses as the since-restart accept-rate document L2
    tunes source tiers from. Confirms the counter surface, not the store."""
    d = hc.http_json(f"{hc.INGEST_BASE}/stats", timeout=10)
    hc.need(isinstance(d, dict) and "accepted" in d and "by_source" in d,
            f"/stats missing expected keys: {str(d)[:160]}")
    return (f"accepted={d.get('accepted')} rejected={d.get('rejected')} "
            f"sources={len(d.get('by_source') or {})} session_only={d.get('session_only')}")


# -- orchestrator (control plane + operator dashboard) -------------------------
@hc.check(area="services", name="orchestrator_health", critical=True)
def _orchestrator_health() -> str:
    """The orchestrator answers /health. Chosen because it is in the auth _OPEN
    allowlist (main.py), so it is the one endpoint reachable without a session and
    the cheapest honest 'is the control plane up' signal."""
    d = hc.http_json(f"{hc.ORCH_BASE}/health", timeout=10)
    status = d.get("status")
    hc.need(status == "ok", f"/health status={status!r} (expected 'ok'): {d}")
    return f"{d.get('service')} v{d.get('version')} status=ok"


# -- crawler-api (the engine + tier probes) ------------------------------------
@hc.check(area="services", name="crawler_api_health", critical=True)
def _crawler_api_health() -> str:
    """The crawler API answers /health with the loaded seed + keyword-corpus sizes.
    A non-ok here means no crawl can be dispatched at all."""
    d = hc.http_json(f"{hc.CRAWLER_API_BASE}/health", timeout=10)
    status = d.get("status")
    hc.need(status == "ok", f"/health status={status!r} (expected 'ok'): {d}")
    return (f"status=ok entities={d.get('entities')} "
            f"sources={d.get('sources')} keywords={d.get('keywords')}")


@hc.check(area="services", name="crawler_api_metrics", critical=False)
def _crawler_api_metrics() -> str:
    """/v1/metrics parses and carries the live batch block - the signal the dashboard
    reads to know whether a crawl is running. Confirms the metrics surface, not health."""
    d = hc.http_json(f"{hc.CRAWLER_API_BASE}/v1/metrics", timeout=15)
    batch = d.get("batch")
    hc.need(isinstance(batch, dict), f"/v1/metrics has no batch block: {str(d)[:160]}")
    return (f"cpu={d.get('cpu_percent')}% batch.running={batch.get('running')} "
            f"done={batch.get('done')}/{batch.get('total')}")


# -- Postgres - the single `documents` store -----------------------------------
@hc.check(area="services", name="postgres_reachable", critical=True)
def _postgres_reachable() -> str:
    """Postgres answers a trivial read. hc.pg is READ-ONLY and bounded; it raises
    Skip when DATABASE_URL is unset (a legitimately ndjson-only deployment) so this
    only FAILs when a configured database is genuinely unreachable."""
    db = hc.pg_scalar("SELECT current_database()")
    hc.need(db is not None, "SELECT current_database() returned nothing")
    return f"reachable - db={db}"


@hc.check(area="services", name="documents_table", critical=False)
def _documents_table() -> str:
    """The `documents` table exists and holds rows. count(*) only - NEVER a body
    column: selecting main_text/html would detoast the corpus (a known outage, and
    hc.pg refuses it anyway). An empty-but-present table FAILs loudly here without
    taking the run down (non-critical), because a live corpus with zero rows is a
    real problem an ndjson-only Skip must not hide."""
    n = hc.pg_scalar("SELECT count(*) FROM documents")
    hc.need(n is not None, "count(*) over documents returned nothing")
    hc.need(int(n) > 0, "documents table is present but EMPTY (0 rows)")
    return f"{int(n):,} rows"


@hc.check(area="services", name="ingest_schema_present", critical=False)
def _ingest_schema_present() -> str:
    """The database the health check reads is one the ingest service initialised -
    proving hc and ingest point at the SAME Postgres, not two that merely both
    answer. The `documents_current` view and the `documents_text_len` trigger are
    created ONLY by pgstore.ensure_schema() at ingest startup, so their presence is
    a cheap, crawl-stable fingerprint of an ingest-managed corpus DB (a raw count
    would drift second-to-second during a crawl and prove nothing about identity)."""
    r = hc.pg(
        "SELECT (SELECT count(*) FROM pg_views WHERE viewname = 'documents_current') AS v, "
        "       (SELECT count(*) FROM pg_trigger WHERE tgname = 'documents_text_len') AS t")
    row = r[0] if r else {}
    has_view, has_trig = int(row.get("v") or 0), int(row.get("t") or 0)
    hc.need(has_view > 0,
            "documents_current view is absent - this DB was not initialised by the "
            "ingest service (pgstore.ensure_schema), so hc and ingest may differ")
    return f"documents_current view present, text_len trigger={'present' if has_trig else 'ABSENT'}"


# -- MinIO - the raw blob bucket -----------------------------------------------
@hc.check(area="services", name="minio_raw_bucket", critical=False)
def _minio_raw_bucket() -> str:
    """The MinIO bucket the crawler writes PDF/image blobs to exists. hc.minio_client()
    raises Skip when the MinIO env (endpoint/key/secret) is unset, so this only runs
    where object storage is actually configured. Bounded with a socket default so a
    dead endpoint cannot hang the check."""
    client = hc.minio_client()                       # raises Skip if MinIO env not set
    bucket = hc.cfg("MINIO_BUCKET", "mallory-raw")
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)
    try:
        exists = client.bucket_exists(bucket)
    finally:
        socket.setdefaulttimeout(prev)
    hc.need(exists, f"MinIO raw bucket {bucket!r} does not exist")
    return f"bucket {bucket!r} exists"


# -- CamoFox - the C3 stealth render server (optional tier) ---------------------
@hc.check(area="services", name="camofox_reachable", critical=False)
def _camofox_reachable() -> str:
    """The CamoFox render server answers /health (the same path camofox_client.health()
    probes). C3 is optional - it lives behind a compose profile and off a separate box -
    so an unreachable server is a Skip (deployment choice), while a server that answers
    with a non-200 is a real fault and FAILs. CAMOFOX_URL may be a comma-separated pool;
    the first entry is probed, matching the client's own _base()."""
    base = hc.CAMOFOX_URL.split(",")[0].strip().rstrip("/")
    if not base:
        raise hc.Skip("CAMOFOX_URL not set")
    try:
        status, body = hc.http_get(f"{base}/health", timeout=5)
    except Exception as exc:                          # transport error - unreachable by design
        raise hc.Skip(f"CamoFox not reachable at {base} (optional C3 tier): {str(exc)[:80]}")
    hc.need(status == 200, f"{base}/health -> HTTP {status}: {body[:120]}")
    return f"{base} /health ok"
