"""Delta / reconciliation health checks - is the push/verify machinery trustworthy?

WHAT "delta" PROVES
    Two independent promises the dashboards make to the operator:

    1. The numbers on the page RECONCILE with each other. The store's own invariant is
       sum(pages-per-source) == total documents: every page counted in the per-site panel is
       one of the pages in the headline total, and vice versa. Both are derived from the
       SAME `documents_current` view (one row per URL, the best capture) - the summary total
       is `count(*)` over it, the per-source pages are that same `count(*)` grouped by
       source_id. If those two disagree, one of the endpoints has silently changed its base
       (e.g. counting the raw `documents` table, which holds every revision) and every
       reconciliation the operator does on screen is wrong. This is THE key invariant.

    2. "Verify data / Push to data centre" can be BELIEVED. `pushed_at` is a purely LOCAL
       claim - this machine stamps it after its own commit returns, and nothing ever goes
       back to confirm the row is still on the far side. A centre restored from an old
       backup, a dropped table, a wiped volume all leave rows stamped "delivered" here that
       do not exist there, while the backlog reads 0 forever and the data is quietly gone.
       /api/live/verify checks the stamped ids against the centre directly and reports
       `divergence.missing` - documents marked delivered that are NOT at the centre. That
       count is the exact fault Verify exists to catch, so a non-zero value is a FAIL here,
       not a note: it means Push believes it is done and it is not.

WHY THESE CHECKS AND NOT OTHERS
    Every assertion is read straight off the response shapes in crawler/ingest_api/live.py
    and the counts in crawler/ingest_api/pgstore.py - no invented fields. All traffic is GET
    and READ-ONLY: this suite NEVER POSTs /api/live/push, so it can prove the delta is
    coherent without moving a single document.

    Cross-endpoint totals (summary vs sources, orchestrator vs ingest) are each served from
    live.py's own independent 30-minute-TTL caches ("summary", "sources_all", and the
    orchestrator's _rollup over them). During and just after a crawl the two caches hold
    snapshots taken at different times, so an exact cross-endpoint equality would cry wolf on
    a perfectly healthy store - the one thing every comment in live.py is written to avoid.
    Those checks therefore assert reconciliation within a cache-skew tolerance and FAIL only
    on gross divergence (the signature of a base-definition bug or a reachability fault),
    while reporting the exact numbers so the operator can see the drift. The within-request
    invariants that come from a single query snapshot (push/status, and most of the verify
    local block) are asserted exactly.
"""
from __future__ import annotations

import json

from .. import hc

# Cross-endpoint totals come from SEPARATE 30-min-TTL caches in live.py, so during/after a
# crawl they are snapshots from different moments. This is the allowance for that skew; a real
# base-definition bug (summary counting raw `documents` instead of `documents_current`) shifts a
# number by the whole `revisions` count, far above this floor.
def _skew_tol(total: int) -> int:
    return max(500, int((total or 0) * 0.02))

# /api/live/verify runs its "documents" query and its "documents_current" query back to back
# with NO caching (pgstore.query opens a fresh connection each time), a few ms apart. Under an
# active crawl a page or two can land in that gap, so current_documents vs unique_urls may differ
# by a handful. A broken one-row-per-URL view would instead diverge by the reported `revisions`
# (orders of magnitude larger), so this small window still catches the real fault.
_XQ_RACE = 100


def _ingest(path: str, timeout: float | None = None):
    """GET a JSON endpoint on the ingest service (raises on non-200 / transport error)."""
    return hc.http_json(f"{hc.INGEST_BASE}{path}", timeout=timeout)


# /api/live/verify is the most expensive read here (distinct-url/hash scans locally + a bounded
# id check across the tunnel). Two checks consume it, so fetch it once per process run.
_VERIFY: dict = {}


def _verify() -> tuple[int, dict | None, str]:
    if "r" not in _VERIFY:
        status, body = hc.http_get(f"{hc.INGEST_BASE}/api/live/verify", timeout=180)
        try:
            obj = json.loads(body) if body else None
        except json.JSONDecodeError:
            obj = None
        _VERIFY["r"] = (status, obj, body)
    return _VERIFY["r"]


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _nn(d: dict, key: str) -> int:
    """Fetch d[key], assert it is a non-negative integer, and return it."""
    v = d.get(key)
    hc.need(_is_int(v), f"{key} is not an integer: {v!r}")
    hc.need(v >= 0, f"{key} is negative: {v}")
    return v


# -- 1. THE reconciliation invariant: sum pages-per-source == total documents --------------------
@hc.check(area="delta", name="reconciliation: sum(pages) == documents", critical=False)
def reconciliation() -> str:
    """summary.postgres.documents (count over documents_current) must equal the sum of every
    source's pages from /api/live/sources (that same count grouped by source_id)."""
    s = _ingest("/api/live/summary", timeout=90)
    pg = s.get("postgres") or {}
    if pg.get("error") or "documents" not in pg:
        raise hc.Skip(f"store not readable: {pg.get('error') or 'no postgres block'}")
    total = pg["documents"]
    hc.need(_is_int(total) and total >= 0, f"summary documents is not a count: {total!r}")

    # limit huge so the per-source list is COMPLETE - a truncated list would under-sum and look
    # like a reconciliation failure. live.py caps the cached list at 100k sources (there are ~500).
    rows = _ingest("/api/live/sources?limit=1000000", timeout=120)
    hc.need(isinstance(rows, list), f"/api/live/sources did not return a list: {type(rows).__name__}")
    sigma = sum((r.get("pages") or 0) for r in rows)
    diff = sigma - total
    running = bool((s.get("batch") or {}).get("running"))
    tol = _skew_tol(total)
    detail = (f"sum(pages)={sigma:,} across {len(rows)} sources vs documents={total:,} "
              f"(diff={diff:+,}, tol=+/-{tol}, crawl={'running' if running else 'idle'})")
    hc.need(abs(diff) <= tol, "RECONCILIATION BROKEN - " + detail)
    return detail


# -- 2. /api/live/verify: the LOCAL delta object is internally coherent ------------------------
@hc.check(area="delta", name="verify: local delta coherent", critical=False)
def verify_local() -> str:
    """The `local` block is present in every branch of verify (at-centre, standalone, or a real
    comparison). Its counts must be non-negative and mutually consistent with how pgstore derives
    them: documents_current is one row per URL, so current_documents == unique_urls; every raw
    row is a revision of some URL, so documents >= unique_urls and revisions == documents-urls."""
    status, obj, body = _verify()
    if status == 503:
        raise hc.Skip(f"no local store to verify: {(obj or {}).get('detail') or body[:120]}")
    hc.need(status == 200, f"/api/live/verify -> HTTP {status}: {body[:160]}")
    hc.need(isinstance(obj, dict), "verify did not return a JSON object")
    hc.need("error" not in obj, f"verify returned an error field: {obj.get('error')!r}")
    hc.need(bool(obj.get("checked_at")), "verify has no checked_at timestamp")

    loc = obj.get("local")
    hc.need(isinstance(loc, dict), "verify has no local block")
    documents = _nn(loc, "documents")
    urls = _nn(loc, "unique_urls")
    hashes = _nn(loc, "unique_content_hashes")
    _nn(loc, "html_bytes")
    pushed = _nn(loc, "pushed")
    pending = _nn(loc, "pending")
    current = _nn(loc, "current_documents")
    revisions = _nn(loc, "revisions")
    _nn(loc, "superseded_unsent")
    empty = _nn(loc, "empty_main_text")

    # documents_current is DISTINCT ON (url): exactly one row per URL (small race allowance -
    # verify's two queries are uncached and a few ms apart, see _XQ_RACE).
    hc.need(abs(current - urls) <= _XQ_RACE,
            f"current_documents({current}) != unique_urls({urls}) - documents_current is "
            f"supposed to hold exactly one row per URL")
    # same snapshot (loc query), so these are exact:
    hc.need(documents >= urls, f"documents({documents}) < unique_urls({urls}) - impossible")
    hc.need(revisions == documents - urls,
            f"revisions({revisions}) != documents-unique_urls({documents - urls})")
    hc.need(hashes <= documents, f"unique_content_hashes({hashes}) > documents({documents})")
    hc.need(pending <= current, f"pending({pending}) > current_documents({current})")
    hc.need(pushed <= documents, f"pushed({pushed}) > documents({documents})")
    hc.need(empty <= documents, f"empty_main_text({empty}) > documents({documents})")
    return (f"documents={documents:,} urls={urls:,} current={current:,} pushed={pushed:,} "
            f"pending={pending:,} revisions={revisions:,} empty_text={empty} ok={obj.get('ok')}")


# -- 3. /api/live/push/status: reachable, parseable, no impossible backlog state ---------------
@hc.check(area="delta", name="push/status: sane backlog", critical=False)
def push_status() -> str:
    """push_backlog counts pending/pushed/documents in ONE query over documents_current, so
    pending + pushed == documents exactly (pushed_at is either NULL or not). At the centre live.py
    forces pending=0 by construction, so the sum invariant does not apply and is skipped there."""
    d = _ingest("/api/live/push/status", timeout=60)
    if d.get("error") == "no-db":
        raise hc.Skip("no local store configured")
    pending = _nn(d, "pending")
    pushed = _nn(d, "pushed")
    documents = _nn(d, "documents")
    centre = d.get("centre_documents")
    if centre is not None:
        hc.need(_is_int(centre) and centre >= 0, f"centre_documents not a count: {centre!r}")

    if d.get("at_centre"):
        # writes land at the centre directly; nothing is ever queued, so pending is forced to 0.
        hc.need(pending == 0, f"at-centre but pending={pending} - nothing should queue at the centre")
        return (f"at centre: documents={documents:,} pushed={pushed:,} pending=0 "
                f"reachable={d.get('reachable')}")

    hc.need(pending + pushed == documents,
            f"pending({pending}) + pushed({pushed}) != documents({documents}) - backlog does not "
            f"account for every row")
    return (f"pending={pending:,} pushed={pushed:,} documents={documents:,} "
            f"centre_reachable={d.get('reachable')} centre_documents={centre}")


# -- 4. /api/live/centre/check: the per-leg diagnostic itself works ----------------------------
@hc.check(area="delta", name="centre/check: reports cleanly", critical=False)
def centre_check() -> str:
    """Prove the connection diagnostic returns a well-formed per-leg report. A DOWN centre is the
    NORMAL state (documents wait in the local store, nothing is lost - live.py says so), so red
    legs are REPORTED, never failed; the machinery fault would be a malformed response, which the
    structural assertions catch. Skips where there is no link to probe: standalone or at-centre."""
    d = _ingest("/api/live/centre/check", timeout=90)
    if d.get("configured") is False:
        raise hc.Skip(d.get("note") or "not configured - this machine crawls and stores locally")
    if d.get("at_centre"):
        raise hc.Skip(d.get("note") or "crawler runs AT the data centre - no link to check")

    legs = d.get("legs")
    hc.need(isinstance(legs, list) and legs, "centre/check returned no legs")
    for leg in legs:
        hc.need(isinstance(leg, dict) and "name" in leg and "ok" in leg, f"malformed leg: {leg!r}")
        hc.need(leg["ok"] in (True, False, None), f"leg ok not tri-state: {leg!r}")
    hc.need(bool(d.get("checked_at")), "centre/check has no checked_at")
    hc.need(d.get("ok") in (True, False, None), f"top-level ok not tri-state: {d.get('ok')!r}")

    red = [leg["name"] for leg in legs if leg.get("ok") is False]
    tail = f"; red: {', '.join(red)}" if red else ""
    return (f"ok={d.get('ok')} legs={len(legs)} centre_documents={d.get('centre_documents')}{tail}")


# -- 5. Delta math + the delivered-but-missing catch -------------------------------------------
@hc.check(area="delta", name="verify: no delivered-but-missing", critical=False)
def delta_math() -> str:
    """No negative deltas, local total >= delivered, and - the reason Verify exists - zero
    documents stamped delivered that are absent at the centre. divergence.missing > 0 is the exact
    silent-data-loss bug (pushed_at is a local claim never confirmed against the far side), so it
    FAILs. Skips when there is no far side to compare (at-centre, standalone, or tunnel down)."""
    status, obj, body = _verify()
    if status == 503:
        raise hc.Skip(f"no local store to verify: {(obj or {}).get('detail') or body[:120]}")
    hc.need(status == 200 and isinstance(obj, dict),
            f"/api/live/verify -> HTTP {status}: {body[:160]}")

    loc = obj.get("local") or {}
    documents, pushed = loc.get("documents"), loc.get("pushed")
    hc.need(_is_int(documents) and _is_int(pushed), "verify local counts missing")
    hc.need(documents >= pushed, f"documents({documents}) < pushed({pushed}) - impossible delta")
    for k in ("pending", "superseded_unsent", "revisions"):
        v = loc.get(k)
        hc.need(_is_int(v) and v >= 0, f"negative/invalid {k}: {v!r}")

    div = obj.get("divergence")
    if not isinstance(div, dict):
        # at-centre, standalone, or the centre is unreachable (all normal) - nothing to diff.
        raise hc.Skip(obj.get("verdict") or "no divergence block - nothing to compare against")
    checked, present, missing = div.get("checked"), div.get("present"), div.get("missing")
    hc.need(all(_is_int(x) for x in (checked, present, missing)),
            f"divergence counts not integers: {div!r}")
    hc.need(missing >= 0 and present >= 0 and checked >= 0, f"negative divergence: {div!r}")
    hc.need(present <= checked, f"present({present}) > checked({checked})")
    hc.need(present + missing == checked, f"present+missing != checked: {div!r}")
    # THE fault Verify was built for: rows this machine marked delivered that are NOT at the centre.
    hc.need(missing == 0,
            f"{missing} document(s) marked delivered are MISSING at the centre - re-push needed "
            f"(checked {checked} of {div.get('of_pushed')} pushed, complete={div.get('complete')})")
    return (f"checked={checked:,} present={present:,} missing=0 "
            f"complete={div.get('complete')} of_pushed={div.get('of_pushed')}")


# -- 6. Cross-check: orchestrator's data total agrees with ingest's ----------------------------
@hc.check(area="delta", name="cross-check: orchestrator vs ingest", critical=False)
def cross_check() -> str:
    """/data/summary on the orchestrator PROXIES /api/live/summary, so the document total on the
    orchestrator's data tile must match ingest's - the whole reason live.py exists (the old panels
    read the orchestrator's own SQLite job log and diverged from Postgres). Each side caches
    independently, so agreement is asserted within the cache-skew tolerance, not to the row."""
    s = _ingest("/api/live/summary", timeout=90)
    pg = s.get("postgres") or {}
    if pg.get("error") or "documents" not in pg:
        raise hc.Skip(f"ingest store not readable: {pg.get('error') or 'no postgres block'}")
    ingest_total = pg["documents"]
    hc.need(_is_int(ingest_total) and ingest_total >= 0, f"ingest total not a count: {ingest_total!r}")

    try:
        o = hc.http_json(f"{hc.ORCH_BASE}/api/data/summary", timeout=90)
    except Exception as e:  # noqa: BLE001 - orchestrator absent/unreachable is not a delta fault
        raise hc.Skip(f"orchestrator unreachable: {str(e)[:120]}")
    if o.get("reachable") is False:
        raise hc.Skip(f"orchestrator reports store unreachable: {o.get('error')}")
    opg = o.get("postgres") or {}
    hc.need("documents" in opg, f"orchestrator /data/summary has no postgres.documents: {opg}")  # /api/data/summary
    orch_total = opg["documents"]
    hc.need(_is_int(orch_total) and orch_total >= 0, f"orchestrator total not a count: {orch_total!r}")

    diff = orch_total - ingest_total
    running = bool((s.get("batch") or {}).get("running"))
    tol = _skew_tol(ingest_total)
    detail = (f"orchestrator={orch_total:,} vs ingest={ingest_total:,} "
              f"(diff={diff:+,}, tol=+/-{tol}, crawl={'running' if running else 'idle'})")
    hc.need(abs(diff) <= tol, "orchestrator and ingest disagree - " + detail)
    return detail
