"""Live Data dashboard — a faithful mirror of what is actually in the local store.

Built because the existing dashboards diverged from reality: /stats used in-memory counters that reset
to 0 on restart (while Postgres held 72k docs), and the orchestrator's job/log panels read its own
SQLite run-history, which never sees documents landed by direct crawler-api batches. Every number
here resolves to a single Postgres query, a MinIO listing, or the crawler-api's own live batch — no
counters, no job log, nothing cached that can drift. "Nothing extra, nothing low."

Three sources, each labelled on the page so it is always clear where a number comes from:
  Postgres documents  — the durable page store (total, per source, recent)
  MinIO bucket        — PDF/blob attachments (best-effort count; the bucket is the crawler's dump)
  crawler-api /metrics — the CURRENT running batch, polled live
"""
from __future__ import annotations

import os
import pathlib
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from . import pgstore

router = APIRouter()
HERE = pathlib.Path(__file__).parent

CRAWLER_API = os.environ.get("CRAWLER_API_URL", "http://crawler-api:8099").rstrip("/")

# MinIO object count is O(bucket): it walks EVERY object to count them. MEASURED at ~100 GB of
# blobs: 26.7s. Behind the old 30s TTL that is the same arithmetic error as the Postgres rollup —
# the cache never holds, so the listing runs continuously and the dashboard's own poll is what
# keeps it busy. Same rule: the TTL must comfortably exceed the operation it caches.
# invalidate_minio_cache() still exists for the one moment staleness matters (straight after a
# delete), so a long TTL costs nothing that matters.
_MINIO_CACHE: dict = {"at": 0.0, "val": None}
# 30min, not 5min: the walk is O(bucket) (~100GB, minutes), so a 5min TTL had a refresher running
# essentially back-to-back. It never blocks the request anymore (see _minio_stats), but a blob count
# does not change meaningfully in half an hour, so walk it half-hourly.
_MINIO_TTL = float(os.environ.get("INGEST_MINIO_TTL_S", "1800") or 1800)
_MINIO_REFRESH_LOCK = threading.Lock()

# The Postgres rollups need the SAME treatment, and did not have it.
#
# MEASURED on the live stack: throughput had halved (1.19 -> 0.48 pages/s) and cgroup deltas put
# POSTGRES at 7.16 of 40 cores — more than any crawler container, where it normally sits below 1.
# pg_stat_activity showed THREE concurrent copies of this module's corpus aggregate, ~39s each, plus
# a corpus-wide dedup window query at 8m07s. Every one is a full pass over `documents_current`,
# which is a DISTINCT-ON view over 927k rows. The dashboard polls faster than the query returns, so
# each poll started another one and they stacked.
#
# A TTL alone does not fix that: with nothing cached yet, N simultaneous polls still launch N
# queries. The lock is the part that matters — the first caller runs it, the rest wait and take its
# result. That is why this is single-flight and not just @lru_cache with a timer.
#
# Same pattern the orchestrator already uses for its own rollups; the ingest API simply never got
# it, and only MinIO was cached here.
# 30s was chosen when the rollup took ~1s. MEASURED at 1.07M documents: live_summary takes 33.3s,
# because it is a DISTINCT ON scan over the whole corpus. A 33s query behind a 30s TTL runs
# essentially CONTINUOUSLY — it never gets to be a cache, it just re-arms — and the dashboard's
# corpus tile times out waiting for it and reads "corpus unreachable" over a perfectly healthy
# database. The TTL has to exceed the query, with room as the corpus keeps growing.
# MEASURED under active crawl: the documents_current rollup is ~250s+ (not the 33s of a quiet box),
# so a 300s TTL barely exceeds it and the cache re-arms almost continuously. 30min keeps it a real
# cache (~14% duty cycle) — a corpus total does not change meaningfully in half an hour, and exact
# figures are a DBeaver query away.
_PG_TTL = float(os.environ.get("INGEST_ROLLUP_TTL_S", "1800") or 1800)
# /api/live/quality is the corpus-wide dedup audit — 8m07s (487s) measured. The rule that matters
# is TTL >> query: at the previous 900s it would still be running 54% of every hour, which is not
# a cache, it is a background job with extra steps. An hour keeps it near 13% and falling as the
# corpus grows; it is an AUDIT, so hours-old is fine.
_QUALITY_TTL = float(os.environ.get("INGEST_QUALITY_TTL_S", "3600") or 3600)
_PG_CACHE: dict[str, dict] = {}
_PG_LOCKS: dict[str, threading.Lock] = {}
_PG_LOCKS_GUARD = threading.Lock()


def _cached_pg(key: str, fn, ttl: float | None = None):
    """Serve the cached rollup and refresh it in the BACKGROUND when it goes stale — stale-while-
    revalidate. A rollup that takes minutes must never sit on a request every TTL: the orchestrator's
    per-panel timeout is 20-60s, well under the ~130s+ rollup, so an INLINE recompute made the tile
    read 'store unreachable' every refresh over a perfectly healthy store. Behaviour:
      - fresh (within ttl)        → return it.
      - stale (present but aged)  → return the STALE value now, kick ONE background refresh.
      - cold (nothing cached yet, i.e. just after a restart) → compute inline once (single-flight),
        then it is warm and every later refresh is background-only.
    """
    ttl = _PG_TTL if ttl is None else ttl
    now = time.monotonic()
    hit = _PG_CACHE.get(key)
    if hit and now - hit["at"] < ttl:
        return hit["val"]
    with _PG_LOCKS_GUARD:
        lock = _PG_LOCKS.setdefault(key, threading.Lock())
    if hit is not None:
        # STALE: hand back the old value immediately, refresh off the request (one refresher per key).
        if lock.acquire(blocking=False):
            def _run():
                try:
                    _PG_CACHE[key] = {"at": time.monotonic(), "val": fn()}
                finally:
                    lock.release()
            threading.Thread(target=_run, name=f"pg-refresh:{key}", daemon=True).start()
        return hit["val"]
    # COLD: nothing cached yet — compute inline ONCE under the lock (single-flight), then warm forever.
    with lock:
        hit = _PG_CACHE.get(key)                    # someone may have filled it while we waited
        if hit is not None:
            return hit["val"]
        val = fn()
        _PG_CACHE[key] = {"at": time.monotonic(), "val": val}
        return val


def invalidate_pg_cache() -> None:
    """Forget the cached rollups — same reasoning as invalidate_minio_cache: right after a flush or
    a delete the operator is looking at the page to confirm the rows are gone."""
    _PG_CACHE.clear()


def _minio_refresh() -> None:
    """Walk the bucket and update the cache. Runs in a background thread only — the walk is O(bucket)
    (~100GB, minutes) and must never sit on a request."""
    out = {"objects": None, "bytes": None, "bucket": None, "error": None}
    try:
        from crawler import config, storage
        client = storage._client()                    # reuse the crawler's configured MinIO client
        bucket = config.MINIO_BUCKET
        out["bucket"] = bucket
        n = total = 0
        for obj in client.list_objects(bucket, recursive=True):
            n += 1
            total += obj.size or 0
        out["objects"], out["bytes"] = n, total
    except Exception as exc:                           # noqa: BLE001 — MinIO down must not break the page
        out["error"] = str(exc)[:120]
    _MINIO_CACHE.update(at=time.monotonic(), val=out)


def _minio_stats() -> dict:
    """Best-effort blob count that is NEVER on the request's critical path. Walking the bucket inline
    made /api/live/summary hang for minutes and read 'corpus unreachable' over a healthy store — the
    Postgres part is already cached and fast, and one slow MinIO leg dragged the whole tile down. So:
    serve whatever is cached immediately (even slightly stale — a blob count does not change
    meaningfully in minutes), and kick a single background refresh when it ages past the TTL. A cold
    cache returns a 'measuring' placeholder rather than blocking the first caller."""
    now = time.monotonic()
    hit = _MINIO_CACHE["val"]
    fresh = hit is not None and now - _MINIO_CACHE["at"] < _MINIO_TTL
    if not fresh and _MINIO_REFRESH_LOCK.acquire(blocking=False):
        def _run():
            try:
                _minio_refresh()
            finally:
                _MINIO_REFRESH_LOCK.release()
        threading.Thread(target=_run, name="minio-refresh", daemon=True).start()
    return hit if hit is not None else {"objects": None, "bytes": None, "bucket": None,
                                        "error": None, "measuring": True}


def invalidate_minio_cache() -> None:
    """Forget the cached object count. Called after anything that DELETES blobs.

    The 30s cache is right for dashboard polling and wrong for the one moment it matters: straight
    after a flush, the operator is looking at the page specifically to confirm the corpus is gone,
    and a stale count tells them it is not. Measured — the bucket was empty and the tile still read
    "353 objects / 547 MB", which reads as a failed delete rather than a cached number."""
    _MINIO_CACHE.update(at=0.0, val=None)


def _live_batch() -> dict:
    """The CURRENT crawl, straight from crawler-api. This is the realtime half of the dashboard."""
    try:
        r = httpx.get(f"{CRAWLER_API}/v1/metrics", timeout=6)
        m = r.json()
    except Exception as exc:                           # noqa: BLE001
        return {"reachable": False, "error": str(exc)[:120]}
    b = m.get("batch") or {}
    p = m.get("progress") or {}
    t = (p.get("running") and p.get("totals")) or m.get("totals") or {}
    return {
        "reachable": True,
        "running": bool(b.get("running")),
        "total_jobs": b.get("total"),
        "pages": p.get("pages"),
        "pages_per_sec": round(p.get("pages_per_sec") or 0, 2),
        "elapsed_s": round(p.get("elapsed_s") or 0),
        "totals": {k: t.get(k) for k in ("fetched", "kept", "sent", "accepted",
                                         "dropped_by_gate", "skipped_unchanged")},
        "cpu_percent": m.get("cpu_percent"),
    }


@router.get("/api/live/summary")
def summary() -> dict:
    # No counts at all when the store cannot be read — not zeros. has_db() caches True after the
    # first success, so a LATER outage takes this path, and zeros there read as "the corpus is
    # empty" on both this page and the orchestrator's tile.
    pg = _cached_pg("summary", pgstore.live_summary) if pgstore.has_db() else {"error": "no-db"}
    return {"postgres": pg, "minio": _minio_stats(), "batch": _live_batch()}


@router.get("/api/live/sources")
def sources(limit: int = 200, q: str = "") -> list[dict]:
    """`q` filters source_id case-insensitively; "" (the default) is the unfiltered list."""
    if not pgstore.has_db():
        return []
    # ponytail: the unfiltered list is what the DATA PER SITE panel polls on a timer, and by_source is
    # a heavy full-view GROUP BY (~130s on this corpus). Cache it — this endpoint alone ran raw on every
    # poll (the 18-deep pile-up). Key it WITHOUT the limit: the GROUP BY scans the whole corpus whatever
    # the row cap, so per-limit keys (sources:5000 vs :500 vs :200) just recomputed the same query, and
    # the dashboard's limit=5000 poll never hit a limit=200 warm cache. Cache the full list once, slice
    # to the caller's limit. A typed filter (q) is user-initiated and rare, so it runs uncached.
    if q:
        return pgstore.by_source(limit, q)
    rows = _cached_pg("sources_all", lambda: pgstore.by_source(100000, ""))
    return rows[:limit] if (limit and limit > 0) else rows


@router.get("/api/live/types")
def types() -> dict:
    """The corpus split by derived data type and by fetch tier. Both read documents_current, so
    these totals reconcile with /api/live/summary rather than telling a second story."""
    if not pgstore.has_db():
        return {"types": {"error": "no-db"}, "served_by": []}
    return {"types": _cached_pg("types", pgstore.by_type),
            "served_by": _cached_pg("served_by", pgstore.by_served_by)}


@router.get("/api/live/quality")
def quality() -> dict:
    """Dedupe + cleanliness audit of this corpus. Read-only; see pgstore.quality().

    Cached HARDER than the other rollups, and separately, because it is by far the most expensive
    read in the API: a `row_number() OVER (PARTITION BY source_id, content_hash)` across the whole
    corpus. MEASURED at 8m07s on 927k rows while the dashboard polled it — so the poll interval was
    two orders of magnitude shorter than the query, and copies stacked until Postgres was the
    biggest CPU consumer on the box. This is an AUDIT, not a live gauge; minutes-old is fine, and a
    number that costs the crawl throughput to compute is not."""
    if not pgstore.has_db():
        return {"error": "no-db"}
    return _cached_pg("quality", pgstore.quality, ttl=_QUALITY_TTL)


@router.post("/api/live/dedupe")
def dedupe(apply: bool = False) -> dict:
    """Collapse same-bytes-different-url pages to one URL each. Dry unless `apply=true`.

    POST even for the dry run: it is the write endpoint with a safety flag, and a GET that can
    delete when you add a query parameter is a trap for whatever proxy or crawler finds it.
    """
    if not pgstore.has_db():
        return {"error": "no-db"}
    return pgstore.dedupe(apply=apply)


@router.get("/api/live/recent")
def recent(limit: int = 100, source: str = "", sources: str = "",
           served_by: str = "", dtype: str = "", since: str = "", until: str = "") -> list[dict]:
    """`sources` is a comma-separated list — the orchestrator expands a catalog CATEGORY into its
    domains here, because `documents` has no category column to filter on directly."""
    ids = [s.strip() for s in sources.split(",") if s.strip()]
    return pgstore.recent(limit, source, sources=ids, served_by=served_by,
                          dtype=dtype, since=since, until=until) if pgstore.has_db() else []


@router.get("/api/live/search")
def search(q: str = "", limit: int = 100, source: str = "", sources: str = "",
           dtype: str = "", since: str = "", until: str = "", body: bool = True) -> dict:
    """Full-corpus search: title/url always, page body once the tsvector index is built. `sources` is
    the comma-separated category→domains list (same reason as recent()). See pgstore.search()."""
    if not pgstore.has_db():
        return {"reachable": False, "body_ready": False, "rows": []}
    ids = [s.strip() for s in sources.split(",") if s.strip()]
    out = pgstore.search(q, limit=limit, source=source, sources=ids, dtype=dtype,
                         since=since, until=until, body=body)
    out["reachable"] = True
    return out


# ── Pushing the local store up to the data centre ────────────────────────────
# The centre is DATA_CENTRE_HOST, reached over the SSH tunnel (docker-compose.tunnel.yml) which
# forwards its Postgres to db-tunnel:5432 and its MinIO to db-tunnel:9000. There is no HTTP ingest
# API on the far side — the crawler normally writes into that Postgres DIRECTLY, and when the tunnel
# is down a run lands in the local Postgres instead of being lost. So the push is a store-to-store
# copy of the same schema, not a re-POST through an API.

# This ingest's own store IS the data centre — set on a crawler deployed ON the centre, where
# DATABASE_URL already points at the corpus. There is then no second database to push to, and
# DATA_CENTRE_DSN is correctly unset.
#
# Without this the panel says "no data centre configured / this machine crawls and stores locally",
# which is wrong twice over on such a machine: nothing is stored locally, and nothing is missing.
# A warning that is not a fault is one operators learn to ignore, which costs the times it is real.
AT_CENTRE = os.environ.get("CRAWLER_AT_CENTRE", "0").strip().lower() in ("1", "true", "yes")
DC_DSN = os.environ.get("DATA_CENTRE_DSN", "")            # postgresql://…@db-tunnel:5432/mallory
DC_MINIO = os.environ.get("DATA_CENTRE_MINIO", "")        # db-tunnel:9000 (blank = skip blobs)
DC_MINIO_KEY = os.environ.get("DATA_CENTRE_MINIO_KEY", "")
DC_MINIO_SECRET = os.environ.get("DATA_CENTRE_MINIO_SECRET", "")
DC_MINIO_BUCKET = os.environ.get("DATA_CENTRE_MINIO_BUCKET", "mallory-raw")

# The columns copied across. Named explicitly rather than SELECT * so a new local column cannot
# silently break every insert against a server that has not been migrated yet.
_PUSH_COLS = ("document_id", "url", "title", "source_id", "served_by", "main_text", "html",
              "attachments", "ingested_at", "html_len", "crawl_run_id", "node_id")


def _now_iso() -> str:
    """UTC stamp. Local rather than imported from the crawler package: ingest_api is meant to run
    standalone, and a verify button should not be the thing that drags the engine in."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dc_label() -> str:
    """Host:port of the centre, no credentials — this string reaches the dashboard."""
    if not DC_DSN:
        return ""
    from urllib.parse import urlsplit
    p = urlsplit(DC_DSN)
    return f"{p.hostname}:{p.port or 5432}{p.path}"


def _store_label() -> str:
    """Host:port/db of the store THIS ingest writes to, credentials stripped — it reaches the
    dashboard. On a crawler deployed at the centre that store IS the centre, so this is what the
    'centre' field should show rather than a blank."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return ""
    from urllib.parse import urlsplit
    q = urlsplit(dsn)
    return f"{q.hostname}:{q.port or 5432}{q.path}"


# How long the connection check may spend on one leg. Module constants so a test can shrink them:
# probing an unroutable address at production timeouts turned a 1s test into a 45s one, and a test
# nobody will wait for is a test that gets skipped.
CHECK_TCP_TIMEOUT_S = 5.0
CHECK_PG_TIMEOUT_S = 12.0


def _tcp_open(host: str, port: int, timeout: float = CHECK_TCP_TIMEOUT_S) -> tuple[bool, str]:
    """Can we even open a socket? Separated from the Postgres/MinIO probes on purpose: a refused
    TCP connect means the TUNNEL is down, while a completed connect that then fails to log in means
    the tunnel is fine and the CREDENTIALS are wrong. Those have opposite fixes, and collapsing
    them into one 'unreachable' is what makes setting up a new machine a guessing game."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} accepting connections"
    except Exception as exc:                           # noqa: BLE001
        return False, f"{host}:{port} — {type(exc).__name__}: {str(exc)[:80]}"


def _leg(name: str, ok: bool | None, detail: str, fix: str = "") -> dict:
    """ok True/False/None — None is 'not configured', which is a choice, not a fault."""
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


@router.get("/api/live/centre/check")
def centre_check() -> dict:
    """Probe the data-centre link one leg at a time and say which leg is broken.

    This is the button an operator presses on a machine they have just set up. `push/status`
    already answers "can Postgres be reached", but on a fresh box the interesting failures are
    upstream of that: the tunnel container is not running, the SSH key is not authorised, the
    MinIO credentials are wrong, or the heartbeat is going somewhere else. One boolean cannot
    point at any of those.

    Everything here is a read. Nothing is created, migrated or sent.
    """
    legs: list[dict] = []
    label = _dc_label()

    if AT_CENTRE and not DC_DSN:
        # The crawler runs ON the centre. The link this check exists to probe is not missing, it is
        # absent by construction — every page is written into the corpus at the moment it is
        # captured. Report the store itself, which is the only thing left that CAN be down.
        pg = mn = None
        try:
            pg = pgstore.totals()[0]
        except Exception as e:  # noqa: BLE001
            legs.append(_leg("corpus", False, f"the store could not be read: {e}"[:200],
                             "check DATABASE_URL and that the centre's Postgres is up"))
        if pg is not None:
            legs.append(_leg("corpus", True, f"writing straight into the centre — {pg:,} page(s)"))
        return {"ok": None if pg is None else True, "configured": True, "at_centre": True,
                "centre_url": _store_label(), "legs": legs, "checked_at": _now_iso(),
                "note": "This crawler runs AT the data centre: pages land in the corpus as they are "
                        "captured, so there is no link to check and nothing to push."}

    if not DC_DSN:
        legs.append(_leg("configuration", None, "DATA_CENTRE_DSN is not set",
                         "Set DATA_CENTRE_DSN in .env, then: docker compose up -d ingest"))
        # ok is None, not False: a machine deliberately run standalone is not faulty, and a check
        # that reports it as broken is one people learn to ignore.
        return {"ok": None, "configured": False, "centre_url": None, "legs": legs,
                "checked_at": _now_iso(),
                "note": "Not configured is not broken — this machine crawls and stores locally."}

    from urllib.parse import urlsplit
    p = urlsplit(DC_DSN)
    pg_host, pg_port = p.hostname or "", p.port or 5432

    # 1 — the tunnel carrying Postgres.
    ok, detail = _tcp_open(pg_host, pg_port)
    legs.append(_leg("tunnel → postgres", ok, detail, "" if ok else
                     "The db-tunnel container is down or its SSH session is dead. "
                     "docker compose -f docker-compose.crawler.yml -f docker-compose.tunnel.yml "
                     "up -d db-tunnel  (it also self-restarts; give it ~60s)"))

    # 2 — Postgres itself. Only meaningful if the socket opened, but run it regardless so a
    #     misleading "port open, database refuses" is still visible.
    #
    #     ONE connection, reused below for the heartbeat query. Opening a second one produced a
    #     report where the same database was simultaneously down (count timed out) and up
    #     (heartbeat answered) — two handshakes over an SSH tunnel do not have to agree, and a
    #     self-contradicting report is worse than no report.
    #
    #     Generous timeout and one retry, deliberately: an SSH-tunnelled handshake is occasionally
    #     slow, and this button is a considered diagnostic pressed by hand, not a poll. A check
    #     that cries wolf on a healthy link is a check people learn to ignore.
    conn = None
    try:
        import psycopg
        try:
            conn = psycopg.connect(DC_DSN, connect_timeout=CHECK_PG_TIMEOUT_S)
        except Exception:                              # noqa: BLE001 — transient slow handshake
            conn = psycopg.connect(DC_DSN, connect_timeout=CHECK_PG_TIMEOUT_S)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            n = cur.fetchone()[0]
        legs.append(_leg("centre postgres", True, f"{n:,} document(s) at the centre"))
        centre_docs = n
    except Exception as exc:                           # noqa: BLE001
        msg = str(exc).strip().splitlines()[0][:160]
        auth = "password" in msg.lower() or "authentication" in msg.lower()
        legs.append(_leg("centre postgres", False, msg,
                         "The credentials in DATA_CENTRE_DSN are wrong." if auth else
                         "Reachable but not answering — check the centre's Postgres is up."))
        centre_docs = None

    # 3 — MinIO. push/status never checked this, so a machine with a good DSN and bad MinIO keys
    #     looked green and then failed halfway through its first push, on the PDFs.
    if not DC_MINIO:
        legs.append(_leg("centre minio", None, "DATA_CENTRE_MINIO not set — PDFs stay local",
                         "Set DATA_CENTRE_MINIO/_KEY/_SECRET to ship attachments too"))
    else:
        mh, _, mp = DC_MINIO.partition(":")
        ok, detail = _tcp_open(mh, int(mp or 9000))
        legs.append(_leg("tunnel → minio", ok, detail, "" if ok else
                         "Same tunnel as Postgres — if that leg is green too, the centre's MinIO is down"))
        try:
            from minio import Minio
            cli = Minio(DC_MINIO, access_key=DC_MINIO_KEY, secret_key=DC_MINIO_SECRET, secure=False)
            exists = cli.bucket_exists(DC_MINIO_BUCKET)
            legs.append(_leg("centre minio", True,
                             f"bucket '{DC_MINIO_BUCKET}' {'exists' if exists else 'does not exist yet'}"
                             + ("" if exists else " — the first push creates it")))
        except Exception as exc:                       # noqa: BLE001
            legs.append(_leg("centre minio", False, str(exc).strip().splitlines()[0][:160],
                             "Check DATA_CENTRE_MINIO_KEY / _SECRET"))

    # 4 — do the two services agree on who this machine IS? ingest writes documents.node_id and
    #     crawler-api writes the heartbeat, so if only one of them has MALLORY_NODE_ID the corpus
    #     is stamped with a container hostname while the dashboard shows a different name, and
    #     "which machine captured this" quietly stops being answerable. Nothing else surfaces it:
    #     both services look healthy, and both are individually doing what they were told.
    node = os.environ.get("MALLORY_NODE_ID", "") or ""
    try:
        r = httpx.get(f"{CRAWLER_API}/v1/fleet/self", timeout=8)
        crawler_node = (r.json() or {}).get("node_id") or ""
    except Exception:                                  # noqa: BLE001
        crawler_node = ""
    if crawler_node and node and crawler_node != node:
        legs.append(_leg("machine identity", False,
                         f"crawler-api calls this machine '{crawler_node}', ingest calls it '{node}'",
                         "Set ONE MALLORY_NODE_ID in .env (not per-service) and recreate both"))
    elif crawler_node and not node:
        legs.append(_leg("machine identity", False,
                         f"crawler-api reports '{crawler_node}' but ingest has no MALLORY_NODE_ID — "
                         f"documents are being stamped with the ingest container's hostname instead",
                         "Put MALLORY_NODE_ID in .env so BOTH services read it, then: "
                         "docker compose up -d ingest crawler-api"))
    elif not node:
        legs.append(_leg("machine identity", None,
                         "MALLORY_NODE_ID is not set — falling back to the hostname",
                         "Set it in .env. Two machines cloned from one image share a hostname and "
                         "would collapse onto a single row at the centre"))
    else:
        legs.append(_leg("machine identity", True, f"both services agree: '{node}'"))

    try:
        if conn is None:                               # Postgres leg already failed and said why
            raise RuntimeError("no connection to the centre")
        with conn.cursor() as cur:
            if node:
                cur.execute("SELECT EXTRACT(EPOCH FROM (now()-last_seen))::int FROM fleet_nodes"
                            " WHERE node_id=%s", (node,))
            else:
                cur.execute("SELECT EXTRACT(EPOCH FROM (now()-last_seen))::int FROM fleet_nodes"
                            " ORDER BY last_seen DESC LIMIT 1")
            row = cur.fetchone()
        who = node or "this machine (unnamed — using the hostname)"
        if row is None:
            legs.append(_leg("fleet heartbeat", False, f"no row at the centre for {who}",
                             "crawler-api writes it. Check FLEET_HEARTBEAT=1 and that crawler-api "
                             "has the same DATA_CENTRE_DSN, then: curl localhost:8099/v1/fleet/self"))
        else:
            ago = int(row[0])
            legs.append(_leg("fleet heartbeat", ago < 120, f"{who} last reported {ago}s ago",
                             "" if ago < 120 else "Stale — crawler-api may not be running"))
    except Exception as exc:                           # noqa: BLE001
        msg = str(exc)
        if "fleet_nodes" in msg and "does not exist" in msg:
            legs.append(_leg("fleet heartbeat", False, "no machine has ever reported to this centre",
                             "The table is created by the first heartbeat — check FLEET_HEARTBEAT=1"))
        else:
            legs.append(_leg("fleet heartbeat", False, msg.strip().splitlines()[0][:160]))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                          # noqa: BLE001
                pass

    hard = [x for x in legs if x["ok"] is False]
    return {
        "ok": not hard,
        "configured": True,
        "centre_url": label or None,
        "node_id": node or None,
        "centre_documents": centre_docs,
        "legs": legs,
        "checked_at": _now_iso(),
        # Said plainly because the natural fear on seeing red here is that a crawl is being lost.
        "note": ("Everything is reachable." if not hard else
                 "Crawling continues either way — pages land in this machine's own Postgres and "
                 "wait. Nothing is lost while the centre is unreachable."),
    }


@router.get("/api/live/push/status")
def push_status() -> dict:
    """Backlog, plus whether the centre's Postgres actually answers over the tunnel."""
    if not pgstore.has_db():
        return {"error": "no-db"}
    out = pgstore.push_backlog()
    out["centre_url"] = _dc_label() or None
    out["blobs"] = bool(DC_MINIO)
    if AT_CENTRE and not DC_DSN:
        # Nothing is pending because nothing is queued: the write already went to the centre.
        out.update(reachable=True, at_centre=True, pending=0,
                   error=None, note="already at the centre — pages are written there directly")
        return out
    if not DC_DSN:
        out["reachable"] = False
        out["error"] = "DATA_CENTRE_DSN is not set — nothing to push to"
        return out
    try:
        import psycopg
        # connect_timeout, not just the query: a dead tunnel blackholes the TCP handshake, and
        # without it this call hangs until the dashboard's own timeout instead of saying "down".
        with psycopg.connect(DC_DSN, connect_timeout=8) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            out["centre_documents"] = cur.fetchone()[0]
        out["reachable"] = True
        out["detail"] = "postgres ok"
    except Exception as exc:                           # noqa: BLE001
        # Unreachable is the NORMAL state here (it is why the documents are local at all), so it is
        # reported as data with a reason rather than raised as a server error.
        out["reachable"] = False
        out["detail"] = str(exc).strip().splitlines()[0][:160]
    return out


def _copy_blobs(docs: list[dict]) -> tuple[int, list[str]]:
    """Copy each document's attachments to the centre's MinIO. Returns (copied, errors).

    Without this the centre gets documents whose storage_path points at a PDF only this machine
    has — a reference that resolves to nothing, which is worse than an absent attachment because
    nothing downstream can tell the difference.
    """
    if not DC_MINIO:
        return 0, []
    import io

    from minio import Minio

    from crawler import config, storage
    dst = Minio(DC_MINIO, access_key=DC_MINIO_KEY, secret_key=DC_MINIO_SECRET, secure=False)
    if not dst.bucket_exists(DC_MINIO_BUCKET):
        dst.make_bucket(DC_MINIO_BUCKET)
    src, prefix = storage._client(), f"s3://{config.MINIO_BUCKET}/"
    copied, errors = 0, []
    for doc in docs:
        for att in (doc.get("attachments") or []):
            path = (att or {}).get("storage_path") or ""
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            try:
                # Skip what the centre already has: a re-push after a partial run must not re-upload
                # gigabytes over an SSH tunnel to end up with identical bytes.
                try:
                    dst.stat_object(DC_MINIO_BUCKET, rel)
                    continue
                except Exception:                      # noqa: BLE001 — not there, so send it
                    pass
                data = src.get_object(config.MINIO_BUCKET, rel).read()
                dst.put_object(DC_MINIO_BUCKET, rel, io.BytesIO(data), length=len(data))
                copied += 1
            except Exception as exc:                   # noqa: BLE001
                errors.append(f"{rel[:60]} -> {str(exc)[:60]}")
    return copied, errors


@router.get("/api/live/verify")
def verify_against_centre(check_ids: int = 20000) -> dict:
    """Compare THIS machine's store against the data centre and report what is actually true.

    Read-only. Nothing here writes, deletes or pushes — it answers "is my data really up there and
    is either side dirty", so the operator knows what Push will do before pressing it.

    The check that matters is DIVERGENCE. `pushed_at` is a LOCAL claim — this machine stamped it
    after its own commit returned — and nothing has ever gone back to confirm the row is still on
    the far side. A centre restored from an older backup, a dropped table, a wiped volume: all of
    them leave rows stamped "delivered" here that do not exist there, and the backlog reads 0
    forever while the data is gone. So the ids are checked against the centre directly.
    """
    if not pgstore.has_db():
        raise HTTPException(503, "no local database configured")
    out: dict = {"centre_url": _dc_label() or None, "checked_at": _now_iso()}

    # ── local side ────────────────────────────────────────────────────────────────────────────
    loc = pgstore.query(
        """SELECT count(*), count(DISTINCT url), count(DISTINCT content_hash),
                  coalesce(sum(html_len),0), count(*) FILTER (WHERE pushed_at IS NOT NULL),
                  count(*) FILTER (WHERE pushed_at IS NULL),
                  count(*) FILTER (WHERE coalesce(text_len, length(main_text), 0) = 0)
           FROM documents""")
    if not loc:
        raise HTTPException(503, "local database unreachable")
    n, urls, hashes, bytes_, pushed, pending_rows, empty = loc[0]
    # PENDING IS COUNTED THE WAY PUSH COUNTS IT — over documents_current, one row per URL, the best
    # capture. Push deliberately sends only that (shipping every revision would triple the wire
    # cost and hand the centre older copies to reconcile), so counting raw rows here made Verify
    # say "1 never sent" while the Push button beside it said "centre has everything" and sat
    # disabled. Both were right about different questions, which is the worst possible outcome for
    # an operator: two numbers on one screen that contradict each other.
    cur_rows = pgstore.query(
        "SELECT count(*) FILTER (WHERE pushed_at IS NULL), count(*) FROM documents_current")
    pending, current_n = (cur_rows[0] if cur_rows else (pending_rows, urls))
    out["local"] = {"documents": n, "unique_urls": urls, "unique_content_hashes": hashes,
                    "html_bytes": int(bytes_), "pushed": pushed, "pending": pending,
                    "current_documents": current_n,
                    # A superseded revision is never sent, by design. Reported so the row count
                    # difference is explained instead of looking like a delivery failure.
                    "revisions": n - urls, "superseded_unsent": max(0, pending_rows - pending),
                    "empty_main_text": empty}

    if AT_CENTRE and not DC_DSN:
        # There is no second copy to diverge from, so the local figures ARE the centre's figures.
        out["ok"] = True
        out["verdict"] = ("this crawler writes into the centre directly — the numbers above ARE "
                          "the centre's, there is no second copy to compare against")
        return out
    if not DC_DSN:
        out["ok"] = False
        out["verdict"] = "no data centre configured — nothing to compare against"
        return out

    # ── centre side + the divergence check ────────────────────────────────────────────────────
    try:
        import psycopg
        with psycopg.connect(DC_DSN, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT url), coalesce(sum(html_len),0) "
                        "FROM documents")
            c_docs, c_urls, c_bytes = cur.fetchone()
            cur.execute("SELECT count(*) FROM (SELECT url FROM documents GROUP BY url "
                        "HAVING count(*) > 1) d")
            c_dup_urls = cur.fetchone()[0]
            cur.execute("SELECT coalesce(node_id,'(before node tracking)'), count(*) "
                        "FROM documents GROUP BY 1 ORDER BY 2 DESC LIMIT 20")
            by_node = [{"node_id": r[0], "documents": r[1]} for r in cur.fetchall()]

            # Bounded on purpose: an unbounded ANY() on a million-row store is a query nobody
            # wants a dashboard button to launch. `checked` vs `pushed` is reported so a partial
            # check is never mistaken for a complete one.
            ids = [r[0] for r in (pgstore.query(
                "SELECT document_id FROM documents WHERE pushed_at IS NOT NULL "
                "ORDER BY ingested_at DESC LIMIT %s", (max(1, check_ids),)) or [])]
            present = 0
            if ids:
                cur.execute("SELECT count(*) FROM documents WHERE document_id = ANY(%s)", (ids,))
                present = cur.fetchone()[0]
        missing = len(ids) - present
        out["centre"] = {"documents": c_docs, "unique_urls": c_urls,
                         "html_bytes": int(c_bytes), "duplicate_urls": c_dup_urls,
                         "by_node": by_node}
        out["divergence"] = {"checked": len(ids), "of_pushed": pushed, "present": present,
                             "missing": missing,
                             "complete": len(ids) >= pushed}
        out["reachable"] = True
    except Exception as exc:                           # noqa: BLE001
        out["reachable"] = False
        out["ok"] = False
        out["verdict"] = f"centre unreachable: {str(exc).strip().splitlines()[0][:160]}"
        return out

    # ── blobs ─────────────────────────────────────────────────────────────────────────────────
    # Counted, not listed: the question is "does the far side hold the attachments its documents
    # point at", and a document whose PDF exists only here is a reference that resolves to nothing.
    try:
        out["blobs"] = _verify_blobs()
    except Exception as exc:                           # noqa: BLE001
        out["blobs"] = {"error": str(exc)[:160]}

    problems = []
    if out["divergence"]["missing"]:
        problems.append(f"{out['divergence']['missing']} document(s) marked delivered are NOT at "
                        f"the centre — re-push needed")
    if pending:
        problems.append(f"{pending} document(s) never sent")
    # c_dup_urls is deliberately NOT a problem. `documents` keeps every VERSION of a page on
    # purpose — document_id is deterministic on (url, content), so an unchanged re-crawl collides
    # and is skipped while a genuinely changed page is kept as history, and `documents_current`
    # picks the best copy per URL. A true byte-identical duplicate cannot exist: it would need the
    # same primary key. Reporting it as dirt would make this button cry wolf on the normal state of
    # a healthy incremental corpus, and a check people learn to ignore is worse than no check.
    if empty:
        problems.append(f"{empty} local document(s) have no main text")
    b = out.get("blobs") or {}
    if b.get("missing_at_centre"):
        problems.append(f"{b['missing_at_centre']} attachment blob(s) referenced but absent at "
                        f"the centre")
    out["problems"] = problems
    out["ok"] = not problems
    out["verdict"] = ("local and centre agree — nothing to do" if not problems
                      else "; ".join(problems))
    return out


def _verify_blobs(limit: int = 500) -> dict:
    """Do the centre's MinIO objects exist for the attachments its documents reference?"""
    if not DC_MINIO:
        return {"configured": False}
    from minio import Minio

    from crawler import config, storage
    rows = pgstore.query(
        "SELECT attachments FROM documents WHERE attachments IS NOT NULL "
        "AND jsonb_array_length(attachments) > 0 ORDER BY ingested_at DESC LIMIT %s", (limit,))
    prefix = f"s3://{config.MINIO_BUCKET}/"
    rels = []
    for (atts,) in (rows or []):
        for att in (atts or []):
            p = (att or {}).get("storage_path") or ""
            if p.startswith(prefix):
                rels.append(p[len(prefix):])
    if not rels:
        return {"configured": True, "checked": 0, "missing_at_centre": 0,
                "note": "no attachments stored yet"}
    dst = Minio(DC_MINIO, access_key=DC_MINIO_KEY, secret_key=DC_MINIO_SECRET, secure=False)
    src = storage._client()
    missing_centre = missing_local = 0
    for rel in rels:
        try:
            dst.stat_object(DC_MINIO_BUCKET, rel)
        except Exception:                              # noqa: BLE001
            missing_centre += 1
        try:
            src.stat_object(config.MINIO_BUCKET, rel)
        except Exception:                              # noqa: BLE001
            missing_local += 1
    return {"configured": True, "checked": len(rels), "missing_at_centre": missing_centre,
            "missing_locally": missing_local, "bucket": DC_MINIO_BUCKET}


@router.post("/api/live/push")
def push_to_centre(limit: int = 200) -> dict:
    """Copy the next slice of local-only documents into the centre's Postgres (and its MinIO).

    One slice per call, deliberately: an operator pressing a button wants an answer in seconds and a
    number they can watch go down, not a request that hangs for an hour over an SSH tunnel. The
    response carries `remaining`, so the UI just calls again.

    The whole slice is ONE remote transaction. It commits or it does not, and `pushed_at` is stamped
    only after that commit returns — so a tunnel that drops mid-slice leaves those documents pending
    and they are re-sent on the next press. The failure mode is a repeat, never a silent gap.
    """
    if not pgstore.has_db():
        raise HTTPException(503, "no database configured")
    if not DC_DSN:
        raise HTTPException(400, "DATA_CENTRE_DSN is not set — refusing to push into the void")

    batch = pgstore.unpushed(limit)
    if not batch:
        return {"sent": 0, "accepted": 0, "failed": 0, "remaining": 0,
                "message": "nothing pending — the centre already has everything"}

    # Blobs FIRST. If a document lands but its PDF upload then fails, the centre holds a dangling
    # reference; this way a blob failure costs a retry of the slice, not a broken row.
    blobs, blob_errors = 0, []
    try:
        blobs, blob_errors = _copy_blobs(batch)
    except Exception as exc:                           # noqa: BLE001
        blob_errors = [str(exc)[:160]]

    import json as _json

    import psycopg
    from psycopg.types.json import Jsonb
    # .get, not [c]: every column here except document_id is optional at the centre, and a row
    # that predates one of them (node_id was added late) must ship with a NULL rather than take
    # the whole push down with a KeyError. Losing one attribute beats losing the batch.
    rows = [tuple(Jsonb(d.get(c)) if c == "attachments" and d.get(c) is not None else d.get(c)
                  for c in _PUSH_COLS) for d in batch]
    ph = ",".join(["%s"] * len(_PUSH_COLS))
    sql = (f"INSERT INTO documents ({','.join(_PUSH_COLS)}) VALUES ({ph}) "
           "ON CONFLICT (document_id) DO NOTHING")
    try:
        with psycopg.connect(DC_DSN, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                # Self-migrate the centre. It is a REMOTE database that predates any column added
                # here, and an operator running N crawler machines should not have to hand-migrate
                # it before the first push from a new build — the failure mode without this is an
                # opaque "column does not exist" from a host they may not have a shell on.
                # Idempotent, so N machines pushing concurrently is a no-op for all but the first.
                cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS node_id TEXT")
                cur.execute("CREATE INDEX IF NOT EXISTS documents_node_idx ON documents (node_id)")
                cur.executemany(sql, rows)
            conn.commit()
    except Exception as exc:                           # noqa: BLE001
        detail = str(exc).strip().splitlines()[0][:200]
        print(f"[live] push FAILED, nothing stamped: {detail}", flush=True)
        return {"sent": len(batch), "accepted": 0, "failed": len(batch),
                "remaining": pgstore.push_backlog().get("pending"),
                "errors": [detail], "blobs_copied": blobs, "centre_url": _dc_label()}

    stamped = pgstore.mark_pushed([d["document_id"] for d in batch])
    back = pgstore.push_backlog()
    print(f"[live] pushed {stamped} docs + {blobs} blob(s) to {_dc_label()}", flush=True)
    return {"sent": len(batch), "accepted": stamped, "failed": len(blob_errors),
            "remaining": back.get("pending"), "blobs_copied": blobs,
            "errors": blob_errors[:5], "centre_url": _dc_label()}


# ── Deleting a source's data ─────────────────────────────────────────────────
# The only destructive route on this service. Two steps on purpose: the operator sees a real count
# from /preview before /delete removes anything, so "delete this site" is never a blind click.

@router.get("/api/live/doc/{document_id}/text")
def doc_text(document_id: str) -> dict:
    """The extracted text of one page. /raw-html serves the SOURCE and /artifact serves PDFs; the
    cleaned text is what this corpus exists to hold and was the one artifact with no route."""
    if not pgstore.has_db():
        return {"error": "no-db"}
    d = pgstore.document_text(document_id)
    return d or {"error": "not_found"}


@router.get("/api/live/doc/{document_id}/files")
def doc_files(document_id: str) -> dict:
    """PDFs / images stored for one page, with the s3:// path /artifact serves them from."""
    if not pgstore.has_db():
        return {"error": "no-db", "files": []}
    return {"document_id": document_id, "files": pgstore.document_attachments(document_id)}


@router.get("/api/live/source/{source_id}/depth")
def source_depth(source_id: str, sample: int = 12) -> dict:
    """Pages per crawl depth for one site, with example URLs at each level. Read-only.

    The site -> depth -> pages view already existed as SERVER-RENDERED HTML on this dashboard; this
    is the same navigation as JSON so the orchestrator console can show it without a second tunnel
    to this port. It is a GROUP BY rather than that view's load-everything-into-memory index, which
    is what makes it usable on a 42k-page site.
    """
    if not pgstore.has_db():
        return {"error": "no-db", "depths": []}
    return pgstore.depth_breakdown(source_id, sample=max(1, min(50, sample)))


@router.get("/api/live/source/{source_id}/preview")
def source_preview(source_id: str) -> dict:
    """Exactly what deleting this source would destroy. Read-only."""
    if not pgstore.has_db():
        return {"error": "no-db"}
    out = pgstore.preview_source(source_id)
    # Crawl history lives in the CRAWLER's sqlite, not here — ask it, so the dialog can warn that
    # deleting documents alone leaves the site un-recrawlable (every page reads back "unchanged").
    try:
        r = httpx.post(f"{CRAWLER_API}/v1/history/forget",
                       params={"host": source_id, "apply": "false"}, timeout=30)
        out["history_rows"] = r.json().get("matched")
    except Exception as exc:                           # noqa: BLE001
        out["history_rows"] = None
        out["history_error"] = str(exc)[:120]
    return out


@router.delete("/api/live/source/{source_id}")
def source_delete(source_id: str, delete_files: bool = True,
                  forget_history: bool = True) -> dict:
    """Delete every document for one source. IRREVERSIBLE.

    `forget_history` defaults to True because the two halves belong together: documents live in
    Postgres, but the crawler's content hashes live in its own sqlite, and clearing only the first
    leaves a site that cannot be re-crawled — every page would classify "unchanged" and be skipped.
    """
    if not pgstore.has_db():
        return {"error": "no-db"}
    try:
        out = pgstore.delete_source(source_id, delete_files=delete_files)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if forget_history:
        try:
            r = httpx.post(f"{CRAWLER_API}/v1/history/forget",
                           params={"host": source_id, "apply": "true"}, timeout=60)
            out["history_forgotten"] = r.json().get("forgotten")
        except Exception as exc:                       # noqa: BLE001 — documents are already gone;
            # report the failure rather than 500, or the operator cannot tell what did happen.
            out["history_forgotten"] = None
            out["history_error"] = str(exc)[:120]
    print(f"[live] DELETED source={source_id!r} -> {out}", flush=True)
    return out


@router.get("/live", response_class=HTMLResponse)
def page() -> str:
    return (HERE / "live.html").read_text(encoding="utf-8")
