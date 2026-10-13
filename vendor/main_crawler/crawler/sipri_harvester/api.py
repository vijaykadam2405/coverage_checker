"""SIPRI dashboard + crawl control (:8095).

A separate service, not a page bolted onto crawler-api, for two reasons: SIPRI already has its own
Postgres schema and MinIO bucket, so the whole vertical stays self-contained; and adding routes to
crawler-api would mean rebuilding it, which discards a running crawl's in-memory frontier.

Read-only against sipri.* except for the two crawl-control endpoints.
"""
from __future__ import annotations

import logging
import pathlib

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, runner, store

log = logging.getLogger("sipri.api")
app = FastAPI(title="SIPRI data")

HERE = pathlib.Path(__file__).parent

# SIPRI's own top-level nav, in the order the site shows it. Pages are bucketed by first path
# segment; anything unrecognised lands in "other" rather than being silently dropped, so the
# category totals always reconcile with the table count.
CATEGORIES = [
    ("research", "Research"),
    ("databases", "Databases"),
    ("publications", "Publications"),
    ("yearbook", "SIPRI Yearbook"),
    ("news", "News"),
    ("events", "Events"),
    ("media", "Media"),
    ("commentary", "Commentary"),
    ("about", "About"),
    ("visualizations", "Visualizations"),
]


def _q(sql: str, params: tuple = ()) -> list[tuple]:
    """Read helper. Returns [] when the DB is unreachable — the dashboard degrades to empty tiles
    instead of a 500, which is the difference between 'no data yet' and 'the page is broken'."""
    try:
        return store.query(sql, params)
    except Exception as exc:                      # noqa: BLE001
        log.warning("query failed: %s", exc)
        return []


@app.get("/health")
def health() -> dict:
    return {"ok": True, "schema": config.PG_SCHEMA, "bucket": config.MINIO_BUCKET}


@app.get("/api/summary")
def summary() -> dict:
    sch = config.PG_SCHEMA
    transfers = _q(f"select count(*) from {sch}.arms_transfers")
    pages = _q(f"select count(*) from {sch}.documents")
    ds = _q(f"select count(*), coalesce(sum(row_count),0), coalesce(sum(bytes),0) from {sch}.datasets")
    yrs = _q(f"select min(order_yr), max(order_yr) from {sch}.arms_transfers where order_yr is not null")
    # The PDF library. `attachments` postdates this endpoint and postdates store.stats(), so until
    # now the 1,164 harvested documents were invisible to every surface that reads SIPRI — the
    # backfill ran and nothing could show it. Counted separately from `pages` on purpose: they are
    # documents, not crawled HTML, and adding them to the page count would inflate it.
    att = _q(f"select count(*) filter (where status='stored'), "
             f"coalesce(sum(bytes) filter (where status='stored'),0) from {sch}.attachments")
    return {
        "arms_transfers": transfers[0][0] if transfers else 0,
        "pages": pages[0][0] if pages else 0,
        "datasets": ds[0][0] if ds else 0,
        "dataset_rows": int(ds[0][1]) if ds else 0,
        "dataset_bytes": int(ds[0][2]) if ds else 0,
        "attachments": att[0][0] if att else None,
        "attachment_bytes": int(att[0][1]) if att and att[0][1] is not None else None,
        "year_from": yrs[0][0] if yrs and yrs[0][0] else None,
        "year_to": yrs[0][1] if yrs and yrs[0][1] else None,
    }


@app.get("/api/categories")
def categories() -> list[dict]:
    """Crawled pages per SIPRI nav section."""
    sch = config.PG_SCHEMA
    rows = _q(
        f"""select split_part(replace(replace(url,'https://www.sipri.org/',''),
                                      'https://www.att-assistance.org/',''),'/',1) seg,
                   count(*), max(harvested_at)
            from {sch}.documents group by 1"""
    )
    by_seg = {(r[0] or "").lower(): (r[1], r[2]) for r in rows}
    out, claimed = [], set()
    for slug, label in CATEGORIES:
        n, last = by_seg.get(slug, (0, None))
        claimed.add(slug)
        out.append({"slug": slug, "label": label, "pages": n, "last": str(last) if last else None})
    other = sum(v[0] for k, v in by_seg.items() if k not in claimed)
    if other:
        out.append({"slug": "other", "label": "Other / root", "pages": other, "last": None})
    return out


@app.get("/api/datasets")
def datasets_list() -> list[dict]:
    sch = config.PG_SCHEMA
    rows = _q(f"""select key, title, kind, bytes, row_count, minio_key, harvested_at, source_url
                  from {sch}.datasets order by key""")
    return [{"key": r[0], "title": r[1], "kind": r[2], "bytes": r[3], "rows": r[4],
             "minio_key": r[5], "harvested_at": str(r[6]) if r[6] else None, "source_url": r[7]}
            for r in rows]


@app.get("/api/transfers")
def transfers(limit: int = 50, buyer: str = "", seller: str = "", category: str = "") -> dict:
    """Sample rows + the aggregates that make the table meaningful."""
    sch = config.PG_SCHEMA
    where, params = [], []
    for col, val in (("buyer", buyer), ("seller", seller), ("category", category)):
        if val:
            where.append(f"{col} ilike %s")
            params.append(f"%{val}%")
    clause = (" where " + " and ".join(where)) if where else ""
    rows = _q(f"""select buyer, seller, order_yr, delivery_yr, units, units_est, category, desg, descr, status
                  from {sch}.arms_transfers{clause}
                  order by delivery_yr desc nulls last limit %s""", tuple(params) + (min(limit, 500),))
    total = _q(f"select count(*) from {sch}.arms_transfers{clause}", tuple(params))
    cats = _q(f"select category, count(*) from {sch}.arms_transfers group by 1 order by 2 desc")
    sellers = _q(f"select seller, count(*) from {sch}.arms_transfers group by 1 order by 2 desc limit 10")
    return {
        "total": total[0][0] if total else 0,
        "rows": [{"buyer": r[0], "seller": r[1], "order_yr": r[2], "delivery_yr": r[3],
                  "units": r[4], "units_est": r[5], "category": r[6], "desg": r[7],
                  "descr": r[8], "status": r[9]} for r in rows],
        "by_category": [{"name": r[0], "n": r[1]} for r in cats],
        "top_sellers": [{"name": r[0], "n": r[1]} for r in sellers],
    }


@app.get("/api/pages")
def pages(section: str = "", limit: int = 100) -> list[dict]:
    sch = config.PG_SCHEMA
    clause, params = "", []
    if section and section != "other":
        clause = " where url ilike %s"
        params.append(f"%/{section}/%")
    rows = _q(f"""select url, title, html_len, minio_key, harvested_at
                  from {sch}.documents{clause} order by harvested_at desc limit %s""",
              tuple(params) + (min(limit, 500),))
    return [{"url": r[0], "title": r[1], "html_len": r[2], "minio_key": r[3],
             "harvested_at": str(r[4]) if r[4] else None} for r in rows]


# ── crawl control ────────────────────────────────────────────────────────────
@app.get("/api/run/status")
def run_status() -> dict:
    return runner.state()


@app.post("/api/run/start")
def run_start(task: str = "all", target: str = "") -> JSONResponse:
    ok, msg = runner.start(task=task, target=target or None)
    return JSONResponse({"started": ok, "message": msg}, status_code=200 if ok else 409)


@app.post("/api/run/stop")
def run_stop() -> dict:
    return {"stopping": runner.request_stop()}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "dashboard.html").read_text(encoding="utf-8")
