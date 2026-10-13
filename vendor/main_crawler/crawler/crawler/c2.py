"""C2 — the ARCHIVE tier. Standalone: no browser, no BFS, no live fetch, no fallback ladder.

C1 (normal sites) and C3 (bot-protected) work together on the live web. C2 is EXCLUSIVE and
DATE-DRIVEN: a Job carrying ``archive_date`` routes here instead of AsyncEngine, and THE ARCHIVE'S
OWN INDEX — not link-following — is the frontier. Output is the same Document as C1/C3, served_by
"c2_wayback", with the real snapshot date in ``archived_at``.

  archive_date alone        -> "state as of that day": the nearest capture at or before its END.
  archive_date + archive_to -> the state when the window OPENED, plus every CHANGED version through
                               the end of archive_to. Unchanged repeats are never emitted.

ponytail: ONE archive source, and NO source registry/Protocol — ``Snapshot.locator`` IS the seam.
Both other sources named in the original ask were measured DEAD for this question (2026-07-16):
  * Memento/TimeTravel: timetravel.mementoweb.org is NXDOMAIN (LANL decommissioned it 2025-09-05).
    The public MemGator answers on nginx while its Go backend 502/503/504s — a landing-page
    healthcheck reads green while every query fails. No working public aggregator exists. Code
    written against it fails at getaddrinfo. models.py contracts "c2_memento"; it stays
    unimplemented, and that is the correct call.
  * Common Crawl: ~1.01 captures/url/crawl (you cannot diff INSIDE a crawl), ~1% cross-crawl url
    overlap (you cannot diff ACROSS two), and 57% of the calendar falls outside every crawl window.
    Measured for example.com 2026-07-01..23: Wayback 794 captures / 247 digests, CC **zero**.
Wayback CDX alone gives server-side date filtering AND content digests in one response.
UPGRADE PATH: if IA ever shows a real coverage gap, add an archive DIRECTLY (archive.ph/timemap/
works and is complementary) — never via an aggregator. It has no id_ raw mode, so budget its own
HTML cleanup; ``Snapshot.locator`` is where it would attach.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

import httpx

from . import errors, extract, gate, parse
from .async_engine import JobCtx, _env_float, _env_int, _now_iso, stop_requested
from .fetcher import FetchResult
from .harvest import HarvestedPage
from .models import Job

log = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
# id_ = RAW replay: the original bytes, no archive chrome and no link rewriting.
REPLAY = "https://web.archive.org/web/{ts}id_/{url}"
# Belt-and-suspenders: id_ should never inject the toolbar, but a non-id_ replay would, and its
# markup inside main_text would poison content_hash so every snapshot looked "changed".
_WB_TOOLBAR = re.compile(r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?"
                         r"<!--\s*END WAYBACK TOOLBAR INSERT\s*-->", re.DOTALL | re.IGNORECASE)
# A domain scan is paginated; this bounds a pathological domain. Hitting it means the scan is
# INCOMPLETE, which _run_job reports rather than passing off as success.
_CDX_MAX_PAGES = _env_int("CRAWLER_C2_CDX_MAX_PAGES", 200)


@dataclass(frozen=True)
class Snapshot:
    """One archived capture. ``locator`` is the archive-source seam — the only thing that would
    differ for another archive."""
    urlkey: str          # CDX's canonical GROUP key (see _split)
    ts: str              # 14-digit UTC, YYYYMMDDhhmmss
    original: str        # the original LIVE url — what Document.url must be
    digest: str          # content fingerprint; equality here == identical bytes

    @property
    def locator(self) -> str:
        # NEVER canonicalize this: canonicalize_url() strips the trailing slash, turning
        # ".../id_/http://www.sipri.org/" into ".../id_/http://www.sipri.org" — a wrong locator on
        # every homepage/directory url. That is why C2 does not use Fetcher.fetch/fetch_asset,
        # both of which canonicalize their argument.
        return REPLAY.format(ts=self.ts, url=self.original)

    @property
    def archived_at(self) -> str:
        t = self.ts
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"


# ── rate limiting ────────────────────────────────────────────────────────────
# archive.org has no API key and no documented allowance beyond ~60 req/min. A 429 ignored for >60s
# earns a 1-HOUR IP ban that DOUBLES per violation — on a shared egress IP that is an outage for
# everyone, so this is a correctness constraint, not a nicety. The lock matters: FastAPI runs sync
# endpoints on a threadpool, so a bare module global would let N threads all pass the gate at once.
_GATE_LOCK = threading.Lock()
_next_ok = 0.0


def _gate(delay_s: float) -> None:
    global _next_ok
    with _GATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _next_ok - now)
        _next_ok = max(now, _next_ok) + delay_s
    if wait > 0:
        time.sleep(wait)


def _client(timeout_s: float) -> httpx.Client:
    """One pooled client for the whole run.

    Honours CRAWLER_PROXY_URL — the same residential gateway C1 uses (async_engine._proxy_config),
    which rotates the exit IP per request provider-side and so lifts the real ceiling here. httpx
    takes the raw url form directly, so no dict conversion. Empty = direct.
    ponytail: routing C2 through C3/CamoFox was audited and REJECTED — camofox shares this
    container's egress IP (measured identical), archive.org has no WAF for stealth to defeat, and
    its fetch_bytes hits the target twice, doubling requests against the host that bans us.
    """
    proxy = os.environ.get("CRAWLER_PROXY_URL", "").strip() or None
    return httpx.Client(timeout=timeout_s, follow_redirects=True, proxy=proxy,
                        headers={"User-Agent": os.environ.get(
                            "CRAWLER_C2_UA", "Mallory-Crawler/1.0 (+archive research)")})


def _get(client: httpx.Client, url: str, params: dict | None, delay_s: float) -> httpx.Response | None:
    """One rate-limited GET with 429/503 backoff. None = give up (caller must treat as a GAP)."""
    for attempt in range(3):
        if stop_requested():
            return None
        _gate(delay_s)
        try:
            r = client.get(url, params=params)
        except Exception as e:                       # transport/DNS/timeout
            log.warning("c2 GET failed (%s/3) %s: %s", attempt + 1, url, e)
            time.sleep(delay_s * (2 ** attempt))
            continue
        if r.status_code in (429, 503):
            # Honour Retry-After or back off exponentially. Ignoring this is what earns the ban.
            ra = errors.parse_retry_after(r.headers.get("Retry-After")) or delay_s * (2 ** (attempt + 1))
            log.warning("c2 throttled %s on %s — sleeping %.0fs", r.status_code, url, ra)
            time.sleep(min(ra, 300.0))
            continue
        return r
    return None


# ── the window ───────────────────────────────────────────────────────────────
def _window(job: Job) -> tuple[str, str | None]:
    """(to_ts, from_ts). A calendar day is widened to its full UTC span, so `archive_date` alone
    means "as of the END of that day" and a range is inclusive of both endpoints."""
    to_ts = job.archive_to or job.archive_date
    to = to_ts.replace("-", "") + "235959"
    frm = job.archive_date.replace("-", "") + "000000" if job.archive_to else None
    return to, frm


# ── the frontier: ONE paginated domain scan ─────────────────────────────────
def _cdx_params(domain: str, to_ts: str) -> dict:
    return {
        "url": domain, "matchType": "domain", "output": "json", "to": to_ts,
        # urlkey is the GROUP key: one page is archived as BOTH http://x/ and http://x:80/ with
        # differing `original` but ONE urlkey. Grouping on `original` splits a page's history in two
        # and breaks its baseline.
        "fl": "urlkey,timestamp,original,digest,mimetype,statuscode",
        # NOT statuscode:200 — a content REVERT is a warc/revisit row carrying statuscode '-', so a
        # 200 filter silently deletes real changes. This full-string (Java matches()) negation drops
        # archived 4xx/5xx while keeping '-'.
        "filter": "!statuscode:[45]..",
        # NO collapse=digest: it is ADJACENT-only, so at a urlkey boundary (url A's last digest ==
        # url B's first) it deletes B's FIRST row — B's BASELINE. Survivable only with cross-url
        # dedup; ours is deliberately per-url. We dedup in _select instead.
        # NO from=: "the state as of 1 July" includes a page last captured in 2023 and never touched
        # since; from= returns ZERO rows for it, so it would vanish from the baseline while the run
        # reported success. Bound by `to` only; _select splits the window client-side.
        "pageSize": os.environ.get("CRAWLER_C2_CDX_PAGESIZE", "5"),   # BLOCKS, not records
    }


def _num_pages(client: httpx.Client, domain: str, to_ts: str, delay_s: float) -> int | None:
    """How many index pages this domain scan spans. None = ask failed (caller reports a GAP).

    showNumPages must be sent WITHOUT output=json/fl: with them CDX answers HTTP 200 and a JSON
    array of NULLS ([["urlkey",...],[null,null,...]]) instead of the count, which parses as nothing
    and would make every scan look like a total failure. Bare, it returns a plain integer body.
    """
    p = {k: v for k, v in _cdx_params(domain, to_ts).items() if k not in ("output", "fl")}
    r = _get(client, CDX_URL, {**p, "showNumPages": "true"}, delay_s)
    if r is None or r.status_code != 200:
        return None
    try:
        return int(r.text.strip())
    except ValueError:
        log.warning("c2 showNumPages returned non-integer for %s: %r", domain, r.text[:80])
        return None


def _rows(client: httpx.Client, domain: str, to_ts: str, want_urls: int,
          delay_s: float) -> tuple[list, bool, bool]:
    """THE FRONTIER. Returns (rows, partial, truncated).

      partial   = a page FAILED -> the index is INCOMPLETE and the run must say so. A scan that dies
                  on page 7 of 12 enumerates 60% of the site; unflagged, that reads to the operator
                  as "this domain only had 3,000 urls".
      truncated = we stopped EARLY (budget/ceiling) -> the last url's history may be cut mid-scan.
    """
    n = _num_pages(client, domain, to_ts, delay_s)
    if n is None:
        return [], True, False
    capped = min(n, _CDX_MAX_PAGES)
    log.warning("c2 frontier host=%s to=%s pages=%d(of %d) — the CDX index IS the frontier and "
                "archive.org caps us near %.0f req/min, so this scan alone is ~%.0fs",
                domain, to_ts, capped, n, 60.0 / delay_s, capped * delay_s)
    rows: list = []
    partial = False
    seen_urlkeys: set[str] = set()
    for i in range(capped):
        if stop_requested():
            return rows, partial, True
        r = _get(client, CDX_URL, {**_cdx_params(domain, to_ts), "page": str(i)}, delay_s)
        if r is None or r.status_code != 200:
            partial = True                      # a GAP, not silence
            continue
        try:
            page = r.json()
        except Exception:
            partial = True
            continue
        if not isinstance(page, list) or len(page) < 2:
            continue                            # [0] is the header row; a bare header = empty page
        body = page[1:]
        rows.extend(body)
        seen_urlkeys.update(row[0] for row in body if row)
        # Budget stop: every url yields >=1 document, so once we hold more distinct urlkeys than
        # max_pages can emit, scanning on is pure waste. A max_pages=40 job costs ~1 request here,
        # not 505. `>` not `>=`: we need one SPARE urlkey, because the one we stop on may be cut
        # (see _run_job) and gets dropped.
        if want_urls > 0 and len(seen_urlkeys) > want_urls:
            return rows, partial, True
    return rows, partial, capped < n


def _split(rows: list) -> tuple[dict[str, list[Snapshot]], dict[str, list[str]]]:
    """CDX rows -> (pages: urlkey -> [Snapshot] sorted by ts, pdfs: original url -> [ts] sorted).

    mimetype comes from the INDEX, which is why archived PDFs are exact and free: the same scan that
    found the pages already says which PDFs the archive holds and WHEN.
    A warc/revisit row's mimetype HIDES the real one, so it stays with the pages: id_ replays the
    original Content-Type and the true kind is resolved from the RESPONSE, not from CDX.
    """
    pages: dict[str, list[Snapshot]] = {}
    pdfs: dict[str, list[str]] = {}
    for row in rows:
        if len(row) < 6:
            continue
        urlkey, ts, original, digest, mimetype, status = row[:6]
        if not ts or not original:
            continue
        if status and status.isdigit() and int(status) >= 400:
            continue                                   # belt: CDX ignores filters it can't parse
        if (mimetype or "").lower() == "application/pdf":
            pdfs.setdefault(original, []).append(ts)
            continue
        pages.setdefault(urlkey, []).append(Snapshot(urlkey, ts, original, digest or ""))
    for snaps in pages.values():
        snaps.sort(key=lambda s: s.ts)                 # never trust server order
    for tss in pdfs.values():
        tss.sort()
    return pages, pdfs


def _select(snaps: list[Snapshot], from_ts: str | None, to_ts: str) -> list[Snapshot]:
    """The whole feature, in one walk. Every snap already satisfies ts <= to_ts (CDX `to=`).

    as-of  (from_ts None) -> the NEAREST capture at or before to_ts. One document.
    range                 -> the state when the window OPENED (nearest capture at or before
                             from_ts), plus every CHANGED version inside the window.

    "Do not bring repeated" is the digest comparison: an in-window capture identical to what we last
    kept is skipped. Comparing to the PREVIOUS KEPT digest (not a global set) is deliberate — an
    A->B->A revert is three real states and must emit three times; a global set would eat the third.
    A page first captured INSIDE the window has no prior state, so its first in-window capture is
    its baseline — which falls out of the walk for free.
    """
    if not snaps:
        return []
    if from_ts is None:
        return [snaps[-1]]                             # sorted by ts; last <= to_ts is the nearest
    picks: list[Snapshot] = []
    before = [s for s in snaps if s.ts < from_ts]
    last_digest = None
    if before:
        picks.append(before[-1])                       # the baseline: the state the window opened on
        last_digest = before[-1].digest
    for s in snaps:
        if s.ts < from_ts:
            continue
        if s.digest and s.digest == last_digest:
            continue                                   # unchanged -> not a version
        picks.append(s)
        last_digest = s.digest
    return picks


def _pdf_at(pdfs: dict[str, list[str]], url: str, ts: str) -> str | None:
    """The archived PDF nearest at-or-before this page's capture, as a raw replay locator.

    Resolved from the INDEX, not by trusting /web/{ts}id_/ to 302 to the nearest capture: the
    index is `to=`-bounded so it physically cannot hand back a post-window PDF, while the redirect
    picks nearest in EITHER direction and would happily import a 2026 PDF into a July-2024 document.
    None = the archive never captured it in-window -> skip and count it (never fall back to live).
    """
    tss = pdfs.get(url) or pdfs.get(url.rstrip("/")) or []
    older = [t for t in tss if t <= ts]
    pick = older[-1] if older else None
    return REPLAY.format(ts=pick, url=url) if pick else None


def _fetch(client: httpx.Client, snap: Snapshot, delay_s: float) -> FetchResult | None:
    r = _get(client, snap.locator, None, delay_s)
    if r is None or r.status_code >= 400 or not r.content.strip():
        return None
    html = _WB_TOOLBAR.sub("", r.text)
    return FetchResult(url=snap.original, final_url=snap.locator, status=200,
                       content_type=r.headers.get("content-type", "text/html"), kind="html",
                       text_html=html, tier=1, fetched_at=_now_iso(), from_fixture=True)


# ── emit ─────────────────────────────────────────────────────────────────────
def _emit(ctx: JobCtx, client: httpx.Client, snap: Snapshot, pdfs: dict, delay_s: float) -> bool:
    """One snapshot -> one Document through the SAME build -> gate -> dedup -> send path as C1/C3."""
    # Cross-run dedup with ZERO schema change. crawl_pages is keyed on the url string VERBATIM
    # (CrawlHistory never canonicalizes; verified), and nothing ever SCANS the table — every read is
    # `WHERE canonical_url = ?`. So this key is provably disjoint from C1's row for the same url and
    # cannot corrupt C1's history. Keyed on TS, not digest: a revert to earlier content is a real new
    # state and must survive, while a re-run of the same window re-derives the same ts and is skipped.
    hkey = f"{snap.original}#c2@{snap.ts}"
    if ctx.history is not None and ctx.history.get(hkey) is not None:
        ctx.skipped_unchanged += 1
        return False

    fr = _fetch(client, snap, delay_s)
    if fr is None:
        ctx.bump_error(errors.OTHER)
        return False
    ctx.fetched += 1

    pdf_links, missing = [], 0
    if "pdf" in ctx.job.capture:
        budget = extract.pdf_budget(ctx.job)
        found = parse.extract_pdf_links(fr.text_html or "", snap.original)
        # `<= 0 means UNLIMITED` — the extract.py convention. NOT links[:budget or None], which
        # silently drops the LAST link for any negative budget.
        for live_url in (found if budget <= 0 else found[:budget]):
            loc = _pdf_at(pdfs, live_url, snap.ts)
            if loc is None:
                missing += 1
            else:
                pdf_links.append(loc)

    hp = HarvestedPage(url=snap.original, depth=0, fetch=fr, pdf_links=pdf_links,
                       served_by="c2_wayback", served_from=snap.locator,
                       archived_at=snap.archived_at)
    doc = extract.build_document(ctx.job, hp, ctx.seed, ctx.fetcher, False,
                                 crawl_run_id=getattr(ctx, "crawl_run_id", None))
    if doc is None:
        ctx.bump_reason("no_main_text")
        return False
    g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
    ctx.bump_reason(g.reason)
    if not g.keep:
        ctx.dropped_by_gate += 1
        return False
    if pdf_links:
        extract.enrich_assets(ctx.job, doc, hp, ctx.fetcher)
    ctx.kept += 1
    ctx.c2_pdfs_missing = getattr(ctx, "c2_pdfs_missing", 0) + missing

    outcome = ctx.ingest.send(doc)
    ctx.sent += 1
    # Write history ONLY on accept. send() NEVER raises — HttpIngestClient and CollectingIngestClient
    # both catch and return IngestOutcome(False, "transport_error:..."). Writing before this check
    # would tombstone thousands of never-delivered docs as done forever on an ingest outage, and the
    # re-run would skip them all.
    if outcome.accepted:
        ctx.accepted += 1
        if ctx.history is not None:
            ctx.history.upsert(hkey, content_hash=doc.content_hash, etag=None, last_modified=None,
                               status=200, fetched_at=fr.fetched_at, js_heavy=False)
    else:
        ctx.rejected += 1
    return True


def _run_job(ctx: JobCtx, client: httpx.Client, delay_s: float) -> None:
    job = ctx.job
    to_ts, from_ts = _window(job)
    domains = sorted({(u.split("://", 1)[-1].split("/", 1)[0]) for u in job.seed_urls})
    incomplete = False
    for domain in domains:
        if stop_requested() or ctx.done:
            break
        rows, partial, truncated = _rows(client, domain, to_ts, job.max_pages, delay_s)
        incomplete = incomplete or partial or truncated
        pages, pdfs = _split(rows)
        if truncated and len(pages) > 1:
            # CDX sorts by (urlkey, timestamp), so a url's captures are contiguous but CAN straddle
            # the page we stopped on — leaving that LAST url with only part of its history. Half a
            # history silently yields the wrong "nearest capture" and a bogus baseline, and nothing
            # downstream could tell. It sorts last, so drop it; the budget stop kept a spare url
            # precisely so this costs nothing.
            cut = max(pages)
            del pages[cut]
        log.info("c2 %s: %d urls, %d archived pdfs in index", domain, len(pages), len(pdfs))
        for urlkey in sorted(pages):
            if stop_requested() or ctx.done:
                break
            # Reserve per-URL BEFORE expanding its snapshots, and never globally sort picks by ts:
            # baselines are by construction the oldest rows, so a global ts-sort under a budget cap
            # emits every baseline and ZERO changes — deleting the whole range feature. max_pages
            # counts SNAPSHOTS (documents), the same noun the frontier's want_urls bounds.
            for snap in _select(pages[urlkey], from_ts, to_ts):
                if not ctx.reserve():
                    ctx.done = True
                    break
                try:
                    _emit(ctx, client, snap, pdfs, delay_s)
                except Exception:
                    log.exception("c2 emit failed url=%s ts=%s", snap.original, snap.ts)
                    ctx.bump_error(errors.OTHER)
    # A truncated/partial scan must NEVER read as a complete success.
    ctx.c2_incomplete = incomplete
    if incomplete:
        log.warning("c2 job=%s INCOMPLETE — the archive index scan was truncated or had gaps; "
                    "this is a PARTIAL view of the domain, not the whole of it", job.job_id)


def run_c2_batch(jobs: list[Job], *, forward: bool, seed, kp) -> list[dict]:
    """Run archive jobs. No browser, no event loop, no multiprocess sharding — C2 is httpx-bound and
    hard-limited by archive.org's rate cap, so concurrency would only buy a ban."""
    from .dedup import CrawlHistory

    delay_s = _env_float("CRAWLER_C2_CDX_DELAY_S", 2.5)      # ~24/min, under IA's stated 60
    timeout_s = float(seed.capture_defaults.get("timeout_seconds", 30))
    run_id = "run_" + uuid.uuid4().hex[:12]     # batch lineage stamped on every doc (see build_document)
    out: list[dict] = []
    with _client(timeout_s) as client:
        for job in jobs:
            ctx = JobCtx(job, seed, kp, forward=forward)
            ctx.crawl_run_id = run_id
            ctx.history = CrawlHistory()
            try:
                _run_job(ctx, client, delay_s)
            finally:
                summary = ctx.summary()
                summary["c2_incomplete"] = getattr(ctx, "c2_incomplete", False)
                summary["c2_pdfs_not_archived"] = getattr(ctx, "c2_pdfs_missing", 0)
                ctx.history.close()
            # Same shape as run_batch_async: {job_id, summary, documents}. `documents` is the PULL
            # flow — /v1/crawl returns it inline — so an archive job must carry it too or a C2 crawl
            # would answer with an empty list while reporting sent=N.
            out.append({"job_id": job.job_id, "summary": summary,
                        "documents": [d["document"] for d in ctx.ingest.collected]})
    return out
