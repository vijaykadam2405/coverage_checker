"""Dashboards area - prove every dashboard-backing endpoint serves real, self-consistent data.

Three operator dashboards render nothing but JavaScript over a handful of JSON endpoints, so a
dashboard is only as healthy as the endpoints behind it. When one of those rollups is slow enough
to trip the client timeout, the panel does not degrade gracefully - it reads "corpus unreachable" /
"timed out" over a perfectly healthy store. This suite hits each endpoint the UIs actually call and
asserts three things:

  * REACHABLE + REAL   - 200, JSON, no {"error"}, the expected keys present, counts non-negative.
                         An unreachable store is reported as an error by the services (never zeros),
                         so a {"error"} here is exactly the "dashboard shows unreachable" symptom.
  * SELF-CONSISTENT    - numbers inside one response are internally plausible (subsets <= totals,
                         distinct <= rows), and the cross-endpoint totals that read the SAME view
                         (documents_current) reconcile within a tolerance that survives cache skew.
  * BOUNDED IN TIME    - every call is timed and the seconds are reported in the detail, so a
                         slow-lane regression is visible day to day. The known-expensive rollups
                         (orch /api/data/sources at limit=5000, /api/logs/sites, the corpus dedup
                         audit) get generous-but-bounded timeouts; we FAIL only on timeout/error,
                         never merely for being slow.

Dashboards covered, and the endpoints their panels poll (read from the UI source, not guessed):

  ingest "Live Data" (crawler/ingest_api/live.py + live.html + app.py "/"):
      GET /api/live/summary   /api/live/sources   /api/live/types   /api/live/quality
      GET /api/live/recent    /api/live/search    /live (html)      / (html)

  orchestrator operator panel (orchestrator/.../static/index.html + api.py, /api prefix):
      GET /api/data/summary   /api/data/sources?limit=5000   /api/data/types
      GET /api/data/quality   /api/logs/sites                / (html)
      (each /api/data/* PROXIES the matching ingest /api/live/* behind a single-flight rollup cache)

  crawler-api crawl dashboard (crawler/crawler_api/app.py + dashboard.py):
      GET /v1/metrics   /v1/batch/status   /health   / (html)

Auth: the orchestrator gates everything behind a login when DASH_AUTH=1. An /api/* call then 401s
and the operator "/" redirects to the login page - both are reported as SKIP("auth on"), not FAIL,
because a protected console is a choice, not a broken dashboard. Ingest and crawler-api carry no
auth of their own.

READ-ONLY: every request here is a GET. Nothing crawls, pushes, deletes or dispatches.
"""
from __future__ import annotations

import json
import time

from .. import hc


# -- tunable, bounded timeouts (seconds) ---------------------------------------
# Fast endpoints (HTML pages, /health, /v1/metrics, /v1/batch/status, ingest reads that are cached
# and cheap) use DASH_TIMEOUT. The orchestrator's /api/data/* rollups proxy a 60s ingest call behind
# their own cache, so a COLD one legitimately takes up to ~60s - they get DASH_SLOW_TIMEOUT. The
# corpus dedup audit is by far the most expensive read in the stack, so it gets its own knob.
def _cfg_f(key: str, default: str) -> float:
    try:
        return float(hc.cfg(key, default))
    except ValueError:
        return float(default)


DASH_TIMEOUT = _cfg_f("HC_DASH_TIMEOUT_S", str(max(hc.HTTP_TIMEOUT, 60.0)))
DASH_SLOW_TIMEOUT = _cfg_f("HC_DASH_SLOW_TIMEOUT_S", "120")      # sources@5000, logs/sites, home html
DASH_QUALITY_TIMEOUT = _cfg_f("HC_DASH_QUALITY_TIMEOUT_S", "180")  # corpus dedup / cleanliness audit
# How far the SAME-view totals may drift between two independently-cached rollups before we call it a
# real divergence rather than cache skew. Both /api/live/{summary,types} scan documents_current but
# under separate long-TTL caches, and a growing corpus means their snapshots are minutes apart - a
# strict equality would cry wolf. A gross split (one has data, the other zeros) still exceeds this.
RECONCILE_TOL = _cfg_f("HC_DASH_RECONCILE_TOL", "0.25")
# A benign, bounded search term for the corpus-search endpoint (limit kept tiny at the call site).
SEARCH_Q = hc.cfg("HC_DASH_SEARCH_Q", "report")


# -- small HTTP helpers: time the call, turn auth into a SKIP, keep errors readable ----------------
def _timed_get(url: str, timeout: float) -> tuple[int, str, float]:
    t0 = time.monotonic()
    status, body = hc.http_get(url, timeout=timeout)
    return status, body, time.monotonic() - t0


def _get_json(url: str, timeout: float) -> tuple[object, float]:
    """GET a JSON endpoint -> (parsed, seconds). 401 -> SKIP('auth on'); non-200/non-JSON -> FAIL."""
    status, body, secs = _timed_get(url, timeout)
    if status == 401:
        raise hc.Skip("auth on")
    if status != 200:
        raise AssertionError(f"HTTP {status} in {secs:.1f}s: {body[:200]}")
    try:
        return json.loads(body), secs
    except json.JSONDecodeError as e:
        raise AssertionError(f"non-JSON in {secs:.1f}s ({e}): {body[:200]}")


def _get_html(url: str, timeout: float) -> tuple[str, float]:
    """GET an HTML page -> (body, seconds). 401 -> SKIP('auth on'); non-200 -> FAIL."""
    status, body, secs = _timed_get(url, timeout)
    if status == 401:
        raise hc.Skip("auth on")
    if status != 200:
        raise AssertionError(f"HTTP {status} in {secs:.1f}s: {body[:200]}")
    return body, secs


_NODB_MARKERS = ("no-db", "no database", "no datab", "database_url", "no db")


def _is_nodb(msg: object) -> bool:
    """A store deliberately run without Postgres (ndjson-only) is a deployment choice, not a fault -
    the services signal it with a distinctive sentinel rather than a real error. Skip, don't fail."""
    s = str(msg or "").lower()
    return any(m in s for m in _NODB_MARKERS)


def _num(v) -> float:
    """Coerce a JSON number for arithmetic; anything non-numeric reads as 0 for the >=0 assertions."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _rel_delta(a: float, b: float) -> float:
    hi = max(abs(a), abs(b), 1.0)
    return abs(a - b) / hi


# ==============================================================================
# INGEST - the "Live Data" dashboard (crawler/ingest_api)
# ==============================================================================
@hc.check(area="dashboards", name="ingest /api/live/summary", critical=False)
def ingest_summary() -> str:
    """Headline tile: totals from Postgres + the live batch's cpu_percent."""
    d, secs = _get_json(f"{hc.INGEST_BASE}/api/live/summary", DASH_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    pg = d.get("postgres") or {}
    err = pg.get("error")
    if err:
        if _is_nodb(err):
            raise hc.Skip(f"ingest store not configured ({err})")
        raise AssertionError(f"store not readable - the tile would read 'unreachable': {err}")
    for k in ("documents", "sources", "html_bytes", "text_chars"):
        hc.need(k in pg, f"summary.postgres missing '{k}'")
        hc.need(_num(pg[k]) >= 0, f"summary.postgres.{k} is negative: {pg[k]!r}")
    hc.need(_num(pg.get("last_5min")) <= _num(pg["documents"]),
            f"last_5min ({pg.get('last_5min')}) > documents ({pg['documents']})")
    # cpu_percent lives on the live-batch leg; it is present whenever crawler-api is reachable
    # (psutil reads it every call), and absent only when that leg is down - a degraded strip, not a
    # broken summary, so report the state rather than failing the tile on it.
    batch = d.get("batch") or {}
    cpu = batch.get("cpu_percent")
    if cpu is None and batch.get("reachable") is False:
        cpu_note = "cpu_percent n/a (crawler-api leg unreachable)"
    else:
        hc.need(isinstance(cpu, (int, float)) and not isinstance(cpu, bool),
                f"cpu_percent is not numeric: {cpu!r}")
        hc.need(0 <= cpu <= 100 * 1024, f"cpu_percent implausible: {cpu}")
        cpu_note = f"cpu={cpu}%"
    return (f"{int(_num(pg['documents'])):,} docs / {int(_num(pg['sources']))} sources, "
            f"{cpu_note} [{secs:.1f}s]")


@hc.check(area="dashboards", name="ingest /api/live/sources", critical=False)
def ingest_sources() -> str:
    """DATA-PER-SITE panel: per-source rows. live.html polls limit=300."""
    d, secs = _get_json(f"{hc.INGEST_BASE}/api/live/sources?limit=300", DASH_TIMEOUT)
    hc.need(isinstance(d, list), f"expected a list of rows, got {type(d).__name__}")
    if not d:
        return f"0 sources (empty store?) [{secs:.1f}s]"
    keys = ("source_id", "pages", "html_bytes", "with_html", "pdfs", "text_chars")
    r0 = d[0]
    for k in keys:
        hc.need(k in r0, f"source row missing '{k}': {sorted(r0)}")
    hc.need(all(_num(r.get("pages")) >= 0 for r in d), "a source row has negative pages")
    total = sum(_num(r.get("pages")) for r in d)
    top = d[0].get("source_id")
    return f"{len(d)} sources, sum_pages={int(total):,}, top={top} [{secs:.1f}s]"


@hc.check(area="dashboards", name="ingest /api/live/types", critical=False)
def ingest_types() -> str:
    """Corpus split by data type (html/text/pdf) and by fetch tier (served_by)."""
    d, secs = _get_json(f"{hc.INGEST_BASE}/api/live/types", DASH_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    t = d.get("types") or {}
    if t.get("error"):
        if _is_nodb(t["error"]):
            raise hc.Skip(f"ingest store not configured ({t['error']})")
        raise AssertionError(f"by_type errored: {t['error']}")
    docs = _num(t.get("documents"))
    for k in ("documents", "with_html", "with_text", "with_pdf", "pdf_attachments"):
        hc.need(k in t, f"types.types missing '{k}'")
    # single-query invariants -> strict (they can never legitimately be violated).
    for sub in ("with_html", "with_text", "with_pdf"):
        hc.need(_num(t[sub]) <= docs, f"types.{sub} ({t[sub]}) > documents ({docs})")
    hc.need(_num(t["pdf_attachments"]) >= _num(t["with_pdf"]),
            f"pdf_attachments ({t['pdf_attachments']}) < with_pdf ({t['with_pdf']})")
    served = d.get("served_by")
    hc.need(isinstance(served, list), f"served_by is not a list: {type(served).__name__}")
    if served:
        hc.need("served_by" in served[0] and "pages" in served[0],
                f"served_by row missing keys: {sorted(served[0])}")
    return (f"{int(docs):,} docs (html={int(_num(t['with_html'])):,} "
            f"text={int(_num(t['with_text'])):,} pdf={int(_num(t['with_pdf'])):,}), "
            f"{len(served)} tiers [{secs:.1f}s]")


@hc.check(area="dashboards", name="ingest /api/live/quality", critical=False)
def ingest_quality() -> str:
    """Corpus dedup + cleanliness audit - the most expensive read; bounded, generous timeout."""
    d, secs = _get_json(f"{hc.INGEST_BASE}/api/live/quality", DASH_QUALITY_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    if d.get("error"):
        if _is_nodb(d["error"]):
            raise hc.Skip(f"ingest store not configured ({d['error']})")
        raise AssertionError(f"quality errored: {d['error']}")
    for k in ("rows_total", "urls_distinct", "revisions", "hashes_distinct", "empty"):
        hc.need(k in d, f"quality missing '{k}'")
    rows_total = _num(d["rows_total"])
    hc.need(_num(d["urls_distinct"]) <= rows_total,
            f"urls_distinct ({d['urls_distinct']}) > rows_total ({rows_total})")
    hc.need(_num(d["hashes_distinct"]) <= rows_total,
            f"hashes_distinct ({d['hashes_distinct']}) > rows_total ({rows_total})")
    hc.need(_num(d["revisions"]) >= 0, f"revisions negative: {d['revisions']}")
    empty = d.get("empty") or {}
    hc.need(isinstance(empty, dict), "quality.empty is not an object")
    hc.need(all(_num(v) <= rows_total for v in empty.values()),
            f"an empty-bucket count exceeds rows_total: {empty}")
    return (f"{int(rows_total):,} rows / {int(_num(d['urls_distinct'])):,} urls, "
            f"revisions={int(_num(d['revisions'])):,}, "
            f"dup_content={d.get('duplicate_content')} [{secs:.1f}s]")


@hc.check(area="dashboards", name="ingest /api/live/recent", critical=False)
def ingest_recent() -> str:
    """Recent-documents feed. live.html polls limit=80."""
    d, secs = _get_json(f"{hc.INGEST_BASE}/api/live/recent?limit=80", DASH_TIMEOUT)
    hc.need(isinstance(d, list), f"expected a list, got {type(d).__name__}")
    if d:
        for k in ("url", "source_id", "ingested_at"):
            hc.need(k in d[0], f"recent row missing '{k}': {sorted(d[0])}")
    return f"{len(d)} recent rows [{secs:.1f}s]"


@hc.check(area="dashboards", name="ingest /api/live/search", critical=False)
def ingest_search() -> str:
    """Corpus search with a benign query - proves the endpoint answers without erroring."""
    url = f"{hc.INGEST_BASE}/api/live/search?q={SEARCH_Q}&limit=5"
    d, secs = _get_json(url, DASH_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    hc.need(d.get("reachable") is not False,
            f"search reports store unreachable: {d.get('error', d)}")
    hc.need(isinstance(d.get("rows"), list), f"search.rows is not a list: {type(d.get('rows')).__name__}")
    hc.need("body_ready" in d, "search missing 'body_ready'")
    return f"q={SEARCH_Q!r}: {len(d['rows'])} rows, body_ready={d['body_ready']} [{secs:.1f}s]"


@hc.check(area="dashboards", name="ingest /live dashboard html", critical=False)
def ingest_live_html() -> str:
    """The Live Data dashboard page itself renders (HTML 200, not an error page)."""
    body, secs = _get_html(f"{hc.INGEST_BASE}/live", DASH_TIMEOUT)
    hc.need("<" in body and len(body) > 200, f"not an HTML page ({len(body)} bytes)")
    hc.need("Live crawl data" in body or "<title>" in body,
            "does not look like the Live Data page")
    return f"200, {len(body):,} bytes [{secs:.1f}s]"


@hc.check(area="dashboards", name="ingest / dashboard html", critical=False)
def ingest_home_html() -> str:
    """The ingest home dashboard (per-site harvested-pages browser) renders - slow timeout: it reads
    the audit ndjson."""
    body, secs = _get_html(f"{hc.INGEST_BASE}/", DASH_SLOW_TIMEOUT)
    hc.need("<" in body and len(body) > 100, f"not an HTML page ({len(body)} bytes)")
    return f"200, {len(body):,} bytes [{secs:.1f}s]"


# ==============================================================================
# ORCHESTRATOR - the operator panel (/api prefix; proxies ingest behind a rollup cache)
# ==============================================================================
def _orch_reachable_payload(path: str, timeout: float, need_keys: tuple[str, ...] = ()) -> tuple[dict, float]:
    """GET an /api/data/* rollup, enforce reachable+real, return (payload, seconds).

    reachable:false is how these handlers report a dead ingest/store WITHOUT sending zeros - i.e. it
    is precisely the 'panel shows unreachable' symptom this suite exists to catch, so it FAILs (or
    SKIPs when the store is deliberately DB-less)."""
    d, secs = _get_json(f"{hc.ORCH_BASE}{path}", timeout)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    if d.get("reachable") is False or d.get("error"):
        err = d.get("error") or "reachable:false"
        if _is_nodb(err):
            raise hc.Skip(f"store not configured ({err})")
        raise AssertionError(f"panel would read unreachable: {err}")
    for k in need_keys:
        hc.need(k in d, f"{path} missing '{k}': {sorted(d)}")
    return d, secs


@hc.check(area="dashboards", name="orch /api/data/summary", critical=False)
def orch_data_summary() -> str:
    """Watchfloor totals tile - proxies ingest /api/live/summary."""
    d, secs = _orch_reachable_payload("/api/data/summary", DASH_SLOW_TIMEOUT)
    pg = d.get("postgres") or {}
    hc.need("documents" in pg, f"summary.postgres has no documents: {sorted(pg)}")
    hc.need(_num(pg["documents"]) >= 0, f"documents negative: {pg['documents']}")
    return (f"{int(_num(pg['documents'])):,} docs, as_of={d.get('as_of_s')}s [{secs:.1f}s]")


@hc.check(area="dashboards", name="orch /api/data/sources (limit=5000)", critical=False)
def orch_data_sources() -> str:
    """The per-category rollup's source list - index.html requests limit=5000. THIS is the call that
    previously timed out; assert it RESPONDS within the bounded slow timeout and carries rows."""
    d, secs = _orch_reachable_payload("/api/data/sources?limit=5000", DASH_SLOW_TIMEOUT,
                                      need_keys=("rows",))
    rows = d["rows"]
    hc.need(isinstance(rows, list), f"rows is not a list: {type(rows).__name__}")
    if rows:
        hc.need("source_id" in rows[0] and "pages" in rows[0],
                f"source row missing keys: {sorted(rows[0])}")
    return f"{len(rows):,} rows, as_of={d.get('as_of_s')}s [{secs:.1f}s]"


@hc.check(area="dashboards", name="orch /api/data/types", critical=False)
def orch_data_types() -> str:
    """Data-type + fetch-tier breakdown - proxies ingest /api/live/types."""
    d, secs = _orch_reachable_payload("/api/data/types", DASH_SLOW_TIMEOUT)
    t = d.get("types") or {}
    hc.need("documents" in t, f"types.types has no documents: {sorted(t)}")
    hc.need(isinstance(d.get("served_by"), list), "served_by is not a list")
    return (f"{int(_num(t['documents'])):,} docs, {len(d['served_by'])} tiers, "
            f"as_of={d.get('as_of_s')}s [{secs:.1f}s]")


@hc.check(area="dashboards", name="orch /api/data/quality", critical=False)
def orch_data_quality() -> str:
    """Corpus quality audit tile - proxies the expensive ingest /api/live/quality."""
    d, secs = _orch_reachable_payload("/api/data/quality", DASH_QUALITY_TIMEOUT)
    hc.need("rows_total" in d, f"quality has no rows_total: {sorted(d)}")
    hc.need(_num(d.get("urls_distinct")) <= _num(d["rows_total"]),
            f"urls_distinct > rows_total: {d.get('urls_distinct')} > {d['rows_total']}")
    return (f"{int(_num(d['rows_total'])):,} rows, dup_content={d.get('duplicate_content')}, "
            f"as_of={d.get('as_of_s')}s [{secs:.1f}s]")


@hc.check(area="dashboards", name="orch /api/logs/sites", critical=False)
def orch_logs_sites() -> str:
    """Per-site crawl log table - index.html polls limit=500. A slow fan-out across the fleet; assert
    it responds within the bounded slow timeout and reports whether its two backends were reachable."""
    d, secs = _get_json(f"{hc.ORCH_BASE}/api/logs/sites?limit=500", DASH_SLOW_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    for k in ("count", "rows", "counts"):
        hc.need(k in d, f"logs/sites missing '{k}': {sorted(d)}")
    hc.need(isinstance(d["rows"], list), f"rows is not a list: {type(d['rows']).__name__}")
    # store_reachable=false means the stored-pages column would be blank across the whole table - the
    # store leg backing this panel is down. crawler_reachable=false is degraded but the store leg is
    # what makes the rows real, so fail on that one.
    if d.get("store_reachable") is False:
        raise AssertionError(f"store leg unreachable: {d.get('store_error')}")
    crawler = "" if d.get("crawler_reachable", True) else f" (crawler leg down: {d.get('crawler_error')})"
    return f"{d['count']} sites, {len(d['rows'])} rows{crawler} [{secs:.1f}s]"


@hc.check(area="dashboards", name="orch / operator panel html", critical=False)
def orch_panel_html() -> str:
    """The operator panel page renders. With DASH_AUTH=1 this redirects to the login page - detected
    and reported as SKIP('auth on') rather than a false PASS on the wrong page."""
    body, secs = _get_html(f"{hc.ORCH_BASE}/", DASH_TIMEOUT)
    hc.need("<" in body and len(body) > 200, f"not an HTML page ({len(body)} bytes)")
    if "Crawl Control" in body:
        return f"200, operator panel, {len(body):,} bytes [{secs:.1f}s]"
    # urllib follows the 302 to /login, so a login page here means auth is on and we are not signed in.
    if "sign in" in body.lower() or "login" in body.lower():
        raise hc.Skip("auth on")
    return f"200, {len(body):,} bytes (unrecognised page) [{secs:.1f}s]"


# ==============================================================================
# CRAWLER-API - the crawl dashboard (crawler/crawler_api)
# ==============================================================================
@hc.check(area="dashboards", name="crawler-api /v1/metrics", critical=False)
def crawler_metrics() -> str:
    """The dashboard's live metrics strip: cpu_percent + the current batch."""
    d, secs = _get_json(f"{hc.CRAWLER_API_BASE}/v1/metrics", DASH_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    cpu = d.get("cpu_percent")
    hc.need(isinstance(cpu, (int, float)) and not isinstance(cpu, bool),
            f"cpu_percent not numeric: {cpu!r}")
    batch = d.get("batch") or {}
    hc.need("running" in batch, f"batch missing 'running': {sorted(batch)}")
    running = bool(batch.get("running"))
    return (f"cpu={cpu}%, batch running={running} "
            f"({batch.get('done')}/{batch.get('total')}) [{secs:.1f}s]")


@hc.check(area="dashboards", name="crawler-api /v1/batch/status", critical=False)
def crawler_batch_status() -> str:
    """The dashboard's batch panel. A stale 'owner' Thread left in the payload 500s this endpoint and
    takes the dashboard with it, so proving it is 200/JSON is the check that matters."""
    d, secs = _get_json(f"{hc.CRAWLER_API_BASE}/v1/batch/status", DASH_TIMEOUT)
    hc.need(isinstance(d, dict), f"expected an object, got {type(d).__name__}")
    hc.need("running" in d, f"batch/status missing 'running': {sorted(d)}")
    hc.need("owner" not in d, "batch/status leaked the non-JSON 'owner' field")
    return (f"running={bool(d.get('running'))}, stop_pending={d.get('stop_pending')}, "
            f"hosts={len(d.get('hosts') or [])} [{secs:.1f}s]")


@hc.check(area="dashboards", name="crawler-api /health", critical=False)
def crawler_health() -> str:
    """The crawl dashboard's liveness ping."""
    d, secs = _get_json(f"{hc.CRAWLER_API_BASE}/health", DASH_TIMEOUT)
    hc.need(isinstance(d, dict) and d.get("status") == "ok",
            f"health not ok: {d}")
    return f"ok (sources={d.get('sources')}, keywords={d.get('keywords')}) [{secs:.1f}s]"


@hc.check(area="dashboards", name="crawler-api / crawl dashboard html", critical=False)
def crawler_dashboard_html() -> str:
    """The crawl dashboard page renders (HTML 200)."""
    body, secs = _get_html(f"{hc.CRAWLER_API_BASE}/", DASH_TIMEOUT)
    hc.need("<" in body and len(body) > 200, f"not an HTML page ({len(body)} bytes)")
    hc.need("Job Dashboard" in body or "<title>" in body,
            "does not look like the crawl dashboard page")
    return f"200, {len(body):,} bytes [{secs:.1f}s]"


# ==============================================================================
# CONSISTENCY - the numbers the panels show must tell ONE story, not several
# ==============================================================================
@hc.check(area="dashboards", name="ingest totals reconcile (summary vs types vs served_by)",
          critical=False)
def ingest_totals_reconcile() -> str:
    """/api/live/summary, the by-type total, and the sum of served_by pages all count the SAME view
    (documents_current). They must agree - within a tolerance that survives independent cache TTLs on
    a growing corpus, so cache skew is not a false alarm but a zeros-vs-data split still is caught."""
    summ, _ = _get_json(f"{hc.INGEST_BASE}/api/live/summary", DASH_TIMEOUT)
    pg = summ.get("postgres") or {}
    if pg.get("error"):
        raise (hc.Skip(f"store not configured ({pg['error']})") if _is_nodb(pg["error"])
               else AssertionError(f"store not readable: {pg['error']}"))
    types, _ = _get_json(f"{hc.INGEST_BASE}/api/live/types", DASH_TIMEOUT)
    t = types.get("types") or {}
    if t.get("error"):
        raise (hc.Skip(f"store not configured ({t['error']})") if _is_nodb(t["error"])
               else AssertionError(f"by_type errored: {t['error']}"))

    s_docs = _num(pg.get("documents"))
    t_docs = _num(t.get("documents"))
    sb_pages = sum(_num(r.get("pages")) for r in (types.get("served_by") or []))

    d_types = _rel_delta(s_docs, t_docs)
    d_served = _rel_delta(s_docs, sb_pages)
    detail = (f"summary={int(s_docs):,} vs by_type={int(t_docs):,} (delta {d_types:.1%}) vs "
              f"sum_served_by={int(sb_pages):,} (delta {d_served:.1%}); tol={RECONCILE_TOL:.0%}")
    hc.need(d_types <= RECONCILE_TOL, f"summary vs by_type diverge beyond tolerance - {detail}")
    hc.need(d_served <= RECONCILE_TOL, f"summary vs sum_served_by diverge beyond tolerance - {detail}")
    return detail


@hc.check(area="dashboards", name="orch tile agrees with ingest store", critical=False)
def orch_ingest_agree() -> str:
    """The orchestrator's Watchfloor totals tile PROXIES the ingest store. Prove the operator panel
    and the Live Data dashboard show the same corpus size (within cache-skew tolerance) - a mismatch
    means one of the two panels is serving a number the other's source does not back."""
    orch, _ = _get_json(f"{hc.ORCH_BASE}/api/data/summary", DASH_SLOW_TIMEOUT)
    if orch.get("reachable") is False or orch.get("error"):
        err = orch.get("error") or "reachable:false"
        raise (hc.Skip(f"store not configured ({err})") if _is_nodb(err)
               else AssertionError(f"orch panel would read unreachable: {err}"))
    ingest, _ = _get_json(f"{hc.INGEST_BASE}/api/live/summary", DASH_TIMEOUT)
    ipg = ingest.get("postgres") or {}
    if ipg.get("error"):
        raise (hc.Skip(f"store not configured ({ipg['error']})") if _is_nodb(ipg["error"])
               else AssertionError(f"ingest store not readable: {ipg['error']}"))

    o_docs = _num((orch.get("postgres") or {}).get("documents"))
    i_docs = _num(ipg.get("documents"))
    delta = _rel_delta(o_docs, i_docs)
    detail = (f"orch={int(o_docs):,} vs ingest={int(i_docs):,} (delta {delta:.1%}, "
              f"as_of={orch.get('as_of_s')}s); tol={RECONCILE_TOL:.0%}")
    hc.need(delta <= RECONCILE_TOL, f"orch tile and ingest store diverge beyond tolerance - {detail}")
    return detail
