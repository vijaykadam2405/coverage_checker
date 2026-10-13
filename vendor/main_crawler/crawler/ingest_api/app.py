"""Stub Ingest API — FastAPI app exposing POST /ingest/v1/page.

Each POST body is a bundle: ``{"document": {...}}`` — one raw harvested page
per bundle, no separate typed "record" (deep classification is Layer 2's
job). Accepted bundles are kept in an in-memory store and written to
``data/output/ingested.ndjson`` for audit; rejected ones return
``422 {failing_rule}``.

Run: ``uvicorn ingest_api.app:app --port 9090``  (the test harness starts it
in-process).
"""
from __future__ import annotations

import json
import os
from collections import Counter, deque
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from crawler import config, storage

from . import dashboard, pgstore
from .validation import validate_page

# Dashboard lives at "/"; Swagger moved to /v1/docs so it doesn't clash.
app = FastAPI(title="Mallory Ingest API (stub)", version="0.1.0",
              docs_url="/v1/docs", redoc_url=None)

from . import live  # noqa: E402  — Live Data dashboard: ground truth from Postgres/MinIO + live batch
app.include_router(live.router)


@app.on_event("startup")
def _init_pg() -> None:
    # Ensure the documents table + html_len column exist before the dashboard reads (no-op without
    # a DB). Otherwise a dashboard hit on a pre-existing DB could reference a not-yet-added column.
    pgstore.ensure_schema()


# ── Auto-push to the data centre — OFF BY DEFAULT ─────────────────────────────
# Data leaves this machine only when an operator presses Push on the dashboard, after a confirm
# that names the destination and the row count. That is a deliberate property, not an oversight:
# the operator decides when a corpus crosses a machine boundary, and Verify exists so they can see
# what Push would do first.
#
# Set PUSH_AUTO=1 to opt a machine into shipping on a timer — useful for an unattended node, but it
# means that box now sends without anyone watching. Local-first either way: pages land in the local
# Postgres first and are copied up, so a machine whose tunnel is down keeps crawling and catches up
# instead of losing work, and `pushed_at` is stamped only after the remote commit returns — a
# tunnel that drops mid-slice costs a repeat, never a gap.
async def _auto_push_loop() -> None:
    import asyncio

    from . import live
    every = max(10, int(os.environ.get("PUSH_AUTO_EVERY_S", "60")))
    slice_n = max(1, int(os.environ.get("PUSH_AUTO_LIMIT", "200")))
    print(f"[push] auto-push every {every}s, {slice_n}/slice -> {live._dc_label()}", flush=True)
    while True:
        await asyncio.sleep(every)
        try:
            if not pgstore.has_db():
                continue
            pending = (pgstore.push_backlog() or {}).get("pending") or 0
            if not pending:
                continue
            # Blocking psycopg + a possibly-slow tunnel: off the event loop, or a slow push would
            # stall the ingest endpoint that the crawler is actively POSTing pages to.
            res = await asyncio.to_thread(live.push_to_centre, slice_n)
            if res.get("accepted"):
                print(f"[push] auto-pushed {res['accepted']}, {res.get('remaining')} left",
                      flush=True)
            elif res.get("errors"):
                print(f"[push] auto-push failed: {str(res['errors'])[:160]}", flush=True)
        except Exception as exc:                       # noqa: BLE001 — never kill the loop
            print(f"[push] auto-push error: {str(exc)[:160]}", flush=True)


@app.on_event("startup")
async def _start_auto_push() -> None:
    import asyncio

    from . import live
    if os.environ.get("PUSH_AUTO", "0") != "1":
        return                    # the default: nothing is sent without a press
    if not live.DC_DSN:
        return                    # nothing configured = nothing to push to; not an error
    print("[push] AUTO-PUSH IS ON — this machine ships to the centre unattended", flush=True)
    asyncio.create_task(_auto_push_loop())

# In-memory acceptance ledger — BOUNDED recent sample only; the durable store is
# data/output/ingested.ndjson. Unbounded lists here (each holding a page's full inline HTML)
# OOM-killed the container under multi-site crawls. ponytail: deque(maxlen) caps memory and we
# keep only slim records; lifetime totals come from the source counters, not len(ledger).
_LEDGER_MAX = int(os.environ.get("INGEST_LEDGER_MAX", "2000"))
ACCEPTED: deque = deque(maxlen=_LEDGER_MAX)   # slim {ingested_at,url,source_id,stream,document_id}
REJECTED: deque = deque(maxlen=_LEDGER_MAX)
ACCEPT_BY_SOURCE: Counter = Counter()         # lifetime, bounded cardinality (per source_id)
TOTAL_BY_SOURCE: Counter = Counter()
BY_STREAM: Counter = Counter()                # lifetime accepted-by-stream (bounded cardinality)


class IngestBundle(BaseModel):
    document: dict


def _append_ndjson(obj: dict) -> None:
    config.ensure_dirs()
    path = config.OUTPUT_DIR / "ingested.ndjson"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


@app.get("/", response_class=HTMLResponse)
def home(folder: str | None = None, depth: str | None = None) -> str:
    """Browsable dashboard of ingested page bundles (reads the audit ndjson).

    No params → the site index (one folder per crawled host). ``?folder=<host>`` opens that
    site's depth levels; ``?folder=<host>&depth=<n>`` lists every page at that depth;
    ``?folder=__all__`` is the flat grid of all pages."""
    return dashboard.render(folder, depth)


@app.get("/pdf-links")
def pdf_links(host: str) -> dict:
    """Pass-2 helper for the two-pass workflow: every PDF/attachment URL discovered in the crawled
    pages of `host`, parsed from the stored HTML — NOT downloaded here. Feed `urls` back as
    seed_urls to a `{"capture":["pdf"],"max_depth":0}` crawl to bulk-fetch them WITHOUT re-crawling
    pages (pass 1 stays fast with no pdf capture; this pass only downloads the files)."""
    from crawler import parse
    from crawler.canonicalize import registered_domain
    path = config.OUTPUT_DIR / "ingested.ndjson"
    seen: set[str] = set()
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"html"' not in line:
                    continue
                try:
                    doc = json.loads(line).get("document", {})
                except json.JSONDecodeError:
                    continue
                u = doc.get("url", "")
                if not u or registered_domain(u) != host:
                    continue
                for p in parse.extract_pdf_links(doc.get("html") or "", u):
                    seen.add(p)
    return {"host": host, "count": len(seen), "urls": sorted(seen)}


@app.get("/coverage", response_class=HTMLResponse)
def coverage(host: str) -> str:
    """On-demand cross-verify: crawled pages vs the site's sitemap. The sitemap is used ONLY to
    check coverage — it is never fed to the crawl frontier."""
    return dashboard.render_coverage(host)


@app.get("/artifact")
def artifact(path: str):
    """Serve a stored artifact (screenshot / image / PDF) by its s3://... URI — read from
    MinIO when configured, else local disk. Only our storage namespace is served (no
    traversal). PDFs/images open inline in the browser."""
    if not path.startswith(config.STORAGE_URI_PREFIX + "/"):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    data = storage.get(path)
    if data is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return Response(content=data, media_type=storage.content_type_for(path, data),
                    headers={"Content-Disposition": "inline"})


@app.get("/raw-html")
def raw_html(doc_id: str):
    """Serve the raw source HTML of an ingested page. The HTML rides inline on
    the document (not stored as a file), so we look it up in the audit ndjson by
    document_id. Rendered in a sandboxed iframe on the dashboard, or opened raw
    in a new tab. text/plain so the browser shows the source, not a live render
    of a third-party page inside our origin.

    Reads the html from Postgres `documents` when configured (works for every doc, not just those
    still in the ndjson tail); else scans the ndjson line-by-line for the doc_id (keeping the latest
    match's html) instead of loading every record into memory."""
    found = pgstore.fetch_html(doc_id) if pgstore.has_db() else None
    path = config.OUTPUT_DIR / "ingested.ndjson"
    if found is None and path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if doc_id not in line:            # cheap prefilter before the json parse
                    continue
                try:
                    doc = json.loads(line).get("document", {})
                except json.JSONDecodeError:
                    continue
                if doc.get("document_id") == doc_id and doc.get("html"):
                    found = doc["html"]           # keep scanning → latest wins
    if found is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return HTMLResponse(content=found, headers={"Content-Type": "text/plain; charset=utf-8"})


@app.get("/download")
def download(host: str):
    """Download one site's full data as a zip: every page's html + extracted text, the PDF/attachment
    blobs (the MinIO objects), and a self-contained Postgres dump (schema + COPY) — read the files
    directly, or `psql -f documents.sql` into your own DB. Requires DATABASE_URL (it IS a PG dump).

    Sync `def` → FastAPI runs it in a worker thread, so building the (possibly large) zip never
    blocks the event loop. The temp zip is unlinked after the response is sent."""
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse
    if not pgstore.has_db():
        return JSONResponse(status_code=503,
                            content={"error": "download needs Postgres (DATABASE_URL is not set)"})
    from . import export
    zip_path, n_pages, _ = export.build_site_zip(host)
    if n_pages == 0:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        return JSONResponse(status_code=404, content={"error": f"no captured pages for {host}"})
    return FileResponse(zip_path, media_type="application/zip", filename=f"mallory_{host}.zip",
                        background=BackgroundTask(os.unlink, zip_path))


@app.delete("/site")
def delete_site(host: str, confirm: str = "") -> JSONResponse:
    """Delete ALL captured data for one site — its Postgres `documents` rows AND their MinIO blobs
    (PDFs/images). This is what the dashboard's per-site 🗑 button calls, for data the operator no
    longer needs. Irreversible, so it requires ?confirm=DELETE; without it, a stray DELETE is a 400.
    Keyed on the registered domain the dashboard shows (same key as the zip download)."""
    if confirm != "DELETE":
        return JSONResponse(status_code=400,
                            content={"error": "refusing to delete without ?confirm=DELETE"})
    if not pgstore.has_db():
        return JSONResponse(status_code=503,
                            content={"error": "delete needs Postgres (DATABASE_URL is not set)"})
    # pgstore.delete_source does the whole job in a real transaction and removes only blobs NO other
    # site still references (attachment paths can be shared since the doc id is content-addressed).
    try:
        res = pgstore.delete_source(host)
    except Exception as exc:                           # noqa: BLE001 — surface, don't 500 silently
        return JSONResponse(status_code=500, content={"error": str(exc)[:160]})
    if res.get("error"):
        return JSONResponse(status_code=503, content=res)
    return JSONResponse(res)


@app.delete("/sites")
def delete_all_sites(confirm: str = "") -> JSONResponse:
    """Delete EVERY site's captured data — the whole `documents` table and all its MinIO blobs.

    A DIFFERENT confirm token to the per-site route (`DELETE-ALL`, not `DELETE`) on purpose: the two
    are one keystroke apart in a URL, and the blast radius is not. Without it, a stray DELETE is a
    400 rather than an emptied corpus."""
    if confirm != "DELETE-ALL":
        return JSONResponse(status_code=400,
                            content={"error": "refusing to delete everything without ?confirm=DELETE-ALL"})
    if not pgstore.has_db():
        return JSONResponse(status_code=503,
                            content={"error": "delete needs Postgres (DATABASE_URL is not set)"})
    try:
        res = pgstore.delete_all_documents()
    except Exception as exc:                           # noqa: BLE001 — surface, don't 500 silently
        return JSONResponse(status_code=500, content={"error": str(exc)[:160]})
    if res.get("error"):
        return JSONResponse(status_code=503, content=res)
    # The dashboard caches the MinIO object count for 30s. Straight after a delete-all that cache is
    # the difference between "the flush worked" and "the flush did nothing" on the one screen the
    # operator is checking, so drop it here rather than making them wait it out.
    try:
        from . import live
        live.invalidate_minio_cache()
    except Exception:                                  # noqa: BLE001 — a stale tile is not a failure
        pass
    print(f"[ingest] DELETED ALL -> {res}", flush=True)
    return JSONResponse(res)


@app.get("/health")
def health() -> dict:
    accepted = sum(ACCEPT_BY_SOURCE.values())          # lifetime, not the bounded ledger length
    return {"status": "ok", "accepted": accepted,
            "rejected": sum(TOTAL_BY_SOURCE.values()) - accepted}


@app.post(config.INGEST_API_PREFIX + "/page")
def ingest(bundle: IngestBundle):
    document = bundle.document
    src = document.get("source_id", "UNKNOWN")
    TOTAL_BY_SOURCE[src] += 1

    ok, failing_rule = validate_page(document)
    if not ok:
        REJECTED.append({"failing_rule": failing_rule, "url": document.get("url")})
        return JSONResponse(status_code=422, content={"failing_rule": failing_rule})

    ACCEPT_BY_SOURCE[src] += 1
    BY_STREAM[document.get("stream")] += 1
    ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _append_ndjson({"ingested_at": ingested_at, "document": document})   # full doc → durable ndjson
    pgstore.insert_document(document, ingested_at)   # + Postgres `documents` table (no-op if no DB)
    ACCEPTED.append({"ingested_at": ingested_at, "url": document.get("url"),  # slim, bounded sample
                     "source_id": src, "stream": document.get("stream"),
                     "document_id": document.get("document_id")})
    return {"status": "accepted", "document_id": document.get("document_id")}


@app.get("/stats")
def stats() -> dict:
    """Accept-rate per source SINCE THIS PROCESS STARTED (L2 uses this to tune source tiers).

    These are in-process counters and reset on restart — they are NOT the store's contents. For
    what is actually in Postgres/MinIO right now, use /api/live/summary (the Live Data dashboard).
    `session_only: true` is returned so no caller mistakes a fresh-restart 0 for an empty store.
    """
    by_source = {
        s: {"accepted": ACCEPT_BY_SOURCE[s], "total": TOTAL_BY_SOURCE[s],
            "accept_rate": round(ACCEPT_BY_SOURCE[s] / TOTAL_BY_SOURCE[s], 3)
            if TOTAL_BY_SOURCE[s] else 0.0}
        for s in TOTAL_BY_SOURCE
    }
    accepted = sum(ACCEPT_BY_SOURCE.values())
    return {"session_only": True, "note": "since-restart counters; see /api/live/summary for the store",
            "accepted": accepted, "rejected": sum(TOTAL_BY_SOURCE.values()) - accepted,
            "by_stream": dict(BY_STREAM), "by_source": by_source}


def reset() -> None:
    """Clear the ledger (used between test runs)."""
    ACCEPTED.clear(); REJECTED.clear()
    ACCEPT_BY_SOURCE.clear(); TOTAL_BY_SOURCE.clear(); BY_STREAM.clear()
