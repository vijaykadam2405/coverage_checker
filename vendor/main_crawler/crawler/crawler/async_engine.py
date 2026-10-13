"""Async multi-tab browser-pool crawl engine — saturates the machine.

A single-tab sequential crawl uses ~1 core, ~250MB. This engine runs ONE shared pool of W
Chromium browsers, each with T persistent tabs (``playwright.async_api``), fed by a shared
host-aware frontier, so W*T pages render concurrently across many hosts. Per-host politeness
(HostLimiter) means 140 tabs still never hammer a single site.

Governing rule: **one event loop, one thread.** Every Playwright object lives on that loop;
only plain data (FetchResult/Document) ever crosses to ``asyncio.to_thread``. Shared mutable
state (the frontier, per-job seen/counters/budget, the shared CrawlHistory) is touched ONLY
inline in coroutines on the loop thread — cooperative scheduling makes any no-``await`` section
atomic, so almost no locks are needed.

The per-page work is gate → self-dedup → same-run hash dedup → enrich → send.

This is the sole production engine — ``crawler_api.app`` routes both /v1/crawl and
/v1/crawl/batch here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import (blocklist, camofox_client, captcha, edgecase, errors, extract, gate, hostsolver,
               interaction_async, issues, paid_proxy, parse, stealth)
from .canonicalize import canonicalize_url, is_parking_host, same_site
from .dedup import CrawlHistory, classify
from .fetcher import (
    _BROWSER_UA_FALLBACK, FetchResult, Fetcher, _is_tls_or_dns_error, _now_iso, _with_www,
)
from .harvest import HarvestedPage, _allowed_offsite, _in_path_scope, _link_is_relevant
from .ingest_client import CollectingIngestClient, HttpIngestClient, is_retryable_failure
from .keywords import get_corpus
from .models import Job
from .robots import RobotsCache
from .seed import Seed, load_seed
from . import config

try:
    import psutil                          # CPU sampler for the adaptive concurrency controller
except Exception:                          # optional — adaptive gate degrades to a no-op without it
    psutil = None

log = logging.getLogger("async_engine")

# Cross-thread STOP signal. The batch runs in its own daemon thread (run_batch_async);
# the API's stop endpoint runs on a different request thread and sets this Event. The
# engine's drain/worker loops poll it and tear the crawl down gracefully (like the
# wall-clock timeout). A threading.Event is safe to set from any thread and read on the
# loop thread without a lock. ponytail: one global flag — there's one shared pool /
# one batch at a time (the /v1/crawl/batch handler is single-flight), so no per-batch id needed.
_STOP = threading.Event()


# Live adaptive-concurrency state, published for /v1/metrics (the engine runs in a batch thread, so
# the request handler can't reach the engine instance — this module global bridges it).
_ADAPTIVE_STATE: dict = {"enabled": False, "cpu": 0.0, "ram": 0.0, "target": 0,
                         "render_limit": 0, "active": 0}


def adaptive_state() -> dict:
    return dict(_ADAPTIVE_STATE)


# Live crawl-progress counter for the dashboard speed metric. Mutated only on the engine loop thread
# (plain-int += is atomic under the GIL); read by the /v1/metrics request thread. Counts pages the
# LIVE engine works through (C1 + impersonate + C3 + incremental-304). C2 archive runs separately
# and isn't counted here. ponytail: single-process counter — a multi-process shard (CRAWLER_WORKERS>1)
# only sees the parent's pages; expose per-worker + sum if that ever matters (default WORKERS=1).
# Live doc counters too, not just pages: the batch `results` (and therefore /v1/metrics "totals")
# are only written when the WHOLE batch finishes, so on a 147-job crawl the dashboard would sit at
# "0 fetched / 0 kept" for hours. These tick as each page lands so the panel is honest mid-run.
# deflected_render / deflected_host: items requeued instead of parking a tab (render pool saturated
# or the host's permit queue was full). Both were invisible before, which made a starved pool look
# identical to a slow site — the throughput was flat either way and nothing said which.
_STAT_KEYS = ("fetched", "kept", "sent", "accepted", "dropped_by_gate", "skipped_unchanged",
              # not_modified_304: the conditional-GET fast path actually firing. It was counted on
              # the job context and never surfaced here, which made the single most valuable
              # re-crawl optimisation IMPOSSIBLE TO MEASURE from outside — /v1/metrics could show
              # `skipped_unchanged` (the expensive verdict, reached only after a full fetch and
              # often a browser render) but not `not_modified_304` (the cheap one, taken before
              # either). So "is incremental helping?" had no answer but a guess, and the honest
              # move was to refuse to roll it out fleet-wide until this line existed.
              "not_modified_304",
              "deflected_render", "deflected_host",
              # deferred_render: an httpx fetch showed the page needs a browser, so the item was
              # moved to the render lane instead of being rendered inline. That is the mechanism
              # that keeps a worker doing cheap work; counting it is how we can tell the lane split
              # is actually being used rather than silently bypassed.
              "deferred_render", "render_gaveup",
              # skipped_duplicate: fetched fine, but the body hashes to something we already store.
              # It was counted per-job all along and simply never published live, which hid the
              # single largest outcome of a crawl: measured on the 2026-07-28 batch, 16,912 of
              # 18,620 fetches (91%) were duplicates and the panel showed only "fetched 18,620 /
              # kept 1,174" — a 94% gap with no name on it. One host was serving the same page
              # under an unbounded set of URLs; nothing on screen could say so.
              "skipped_duplicate",
              # host_busy_gaveup: the deflect-budget gave up on an item because its HOST never
              # drained. It shares the terminal branch of _retry_or_log_render with a real render
              # failure, so it used to tick render_gaveup — which read as "the browser pool could
              # not cope" while 61 of 64 render permits sat idle. Different cause, different fix.
              "host_busy_gaveup",
              "paid_recovered")
# Lane occupancy, published for /v1/metrics. httpx_reserve is the number of workers that are
# structurally unavailable to the render lane — if that ever reads 0, the split is not doing its job.
_LANE_STATE: dict = {"render_lane_cap": 0, "render_lane_used": 0, "httpx_reserve": 0,
                     "render_q": 0, "frontier": 0}


def lane_state() -> dict:
    return dict(_LANE_STATE)


# The run id and seed hosts of the crawl happening RIGHT NOW.
#
# _batch_status only learns its run_ids in the `finally` after the whole batch returns, so every
# per-site view was blank for the entire life of a run and populated the moment it no longer
# mattered. That is what made the live-sites table read "nothing crawling right now" on top of a
# working crawl — the batch report had no run id to query crawl_pages with, so it returned no
# sites, so the dashboard had no hosts. Published here the same way lanes and progress are.
_RUN_STATE: dict = {"run_id": "", "hosts": []}


def run_state() -> dict:
    return {"run_id": _RUN_STATE["run_id"], "hosts": list(_RUN_STATE["hosts"])}
_PROGRESS: dict = {"pages": 0, "t0": None, "running": False,
                   "stats": {k: 0 for k in _STAT_KEYS}}


def progress_state() -> dict:
    """pages/elapsed + LIVE doc counters for the running crawl (see _STAT_KEYS), for /v1/metrics."""
    import time
    p = _PROGRESS
    elapsed = (time.monotonic() - p["t0"]) if p["t0"] else 0.0
    pages = int(p["pages"])
    return {"pages": pages, "elapsed_s": round(elapsed, 1),
            "pages_per_sec": round(pages / elapsed, 3) if elapsed > 0.5 else 0.0,
            "running": bool(p["running"]),
            "totals": dict(p.get("stats") or {})}


def _tick_stat(name: str, n: int = 1) -> None:
    """Bump a live doc counter. Loop-thread only; plain int += is atomic under the GIL."""
    s = _PROGRESS.get("stats")
    if s is not None and name in s:
        s[name] += n


def _progress_reset() -> None:
    """New run/recrawl: zero the counter and start the clock (dashboard resets its live rate)."""
    import time
    _PROGRESS["pages"] = 0
    _PROGRESS["stats"] = {k: 0 for k in _STAT_KEYS}
    _PROGRESS["t0"] = time.monotonic()
    _PROGRESS["running"] = True


def _progress_done() -> None:
    _PROGRESS["running"] = False


def _tick_page(n: int = 1) -> None:
    _PROGRESS["pages"] += n


def _stop_file():
    return config.DATA_DIR / ".crawl_stop"


def request_stop() -> None:
    """Signal the running crawl to halt (C1 live + C3 CamoFox both stop — the engine drives all
    tiers). Also writes a stop file so CHILD worker processes (multi-process sharding) see it via
    the shared filesystem, not just this process's in-memory event."""
    _STOP.set()
    try:
        _stop_file().write_text("stop")
    except Exception:
        pass


def clear_stop() -> None:
    _STOP.clear()
    try:
        _stop_file().unlink(missing_ok=True)
    except Exception:
        pass


def stop_requested() -> bool:
    if _STOP.is_set():
        return True
    try:
        return _stop_file().exists()      # a sibling worker process / the parent requested stop
    except Exception:
        return False


# ── per-job stop (what the category switch needs) ─────────────────────────────
# STOP IS ALL-OR-NOTHING and that is why the category switch never stopped anything. `request_stop`
# sets one process-wide event; there was no way to say "halt these jobs, leave the rest crawling",
# so switching a category off could only ever gate the NEXT dispatch while its live jobs ran on to
# exhaustion. An operator watching the news crawl continue for hours after switching News off
# reasonably concludes the switch is broken — it was.
#
# Same shape as the stop flag (a file under DATA_DIR) for the same reason: it has to survive the
# process and be visible to child workers. Cached on mtime because it is consulted per URL.

def _drop_file():
    return config.DATA_DIR / ".crawl_drop"


_DROPS: tuple[float, frozenset] = (-1.0, frozenset())


def _drop_mtime() -> float:
    try:
        return _drop_file().stat().st_mtime
    except OSError:
        return -1.0


def set_dropped_jobs(job_ids) -> frozenset:
    """Replace the drop set. Empty clears it. Returns the set now in force."""
    global _DROPS
    s = frozenset(str(j) for j in job_ids if j)
    try:
        if s:
            _drop_file().write_text(json.dumps(sorted(s)))
        else:
            _drop_file().unlink(missing_ok=True)
    except Exception:                     # noqa: BLE001 — an unwritable file must not kill the API
        log.warning("could not persist the drop set", exc_info=True)
    # Set the cache directly rather than waiting for the mtime to change: the API thread and the
    # engine share this process, and a write followed by a read inside one mtime tick would
    # otherwise serve the stale set — the window in which a just-stopped category keeps crawling.
    _DROPS = (_drop_mtime(), s)
    return s


def dropped_jobs() -> frozenset:
    global _DROPS
    mt = _drop_mtime()
    if mt != _DROPS[0]:
        try:
            raw = json.loads(_drop_file().read_text()) if mt >= 0 else []
            _DROPS = (mt, frozenset(str(j) for j in raw))
        except Exception:                 # noqa: BLE001 — a corrupt file must not stop the crawl
            _DROPS = (mt, frozenset())
    return _DROPS[1]


def job_dropped(job_id: str) -> bool:
    d = dropped_jobs()
    return bool(d) and job_id in d


def _item_dropped(item) -> bool:
    """Drain check at the top of a worker loop, so a switched-off category releases its in-memory
    queue immediately instead of walking every URL through a tab and a host slot first.

    The item is dropped UNPROCESSED, which is what keeps its durable frontier row `pending` — a
    category switched off and back on resumes where it was, exactly like a STOP."""
    ctx = getattr(item, "ctx", None)
    return ctx is not None and job_dropped(ctx.job.job_id)


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


def _log_captcha_limitation(url: str, info, run_id: str | None) -> None:
    """Append a captcha wall we could NOT clear to a durable, greppable log the operator can read.

    The crawl already (a) counts it via ctx.bump_reason, (b) records it in crawl_history with a
    detail string, and (c) log.info's it. But the operator asked specifically to 'maintain the URL
    in logs', so this adds a persistent JSONL artifact on the data volume — one line per wall, with
    the captcha type and why the solve failed — that survives restarts and is trivial to grep or
    hand to a solver run. Best-effort: a logging failure must never affect the crawl."""
    try:
        import json as _json
        path = config.DATA_DIR / "captcha_limitations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "url": url,
            "captcha_type": getattr(info, "captcha_type", None),
            "solvability": getattr(info, "solvability", None),
            "solver_tried": getattr(info, "solver", None),
            "error": getattr(info, "error", None),
            "crawl_run_id": run_id,
            "logged_at": _now_iso(),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("captcha-limitation log write failed for %s", url, exc_info=True)


def _blank_between_items() -> bool:
    """Park each tab on about:blank between work items (default ON). See _worker for why.
    CRAWLER_BLANK_IDLE_TABS=0 leaves the tab on the rendered page after _process returns."""
    return os.environ.get("CRAWLER_BLANK_IDLE_TABS", "1") != "0"


def _resume_tracking() -> bool:
    """Whether to maintain the durable frontier. On by default: it is what makes an interrupted
    crawl resumable, and the cost is a batched sqlite write per few hundred URLs. Set
    CRAWLER_RESUME_TRACKING=0 to disable (a resumed job then simply starts fresh)."""
    return os.environ.get("CRAWLER_RESUME_TRACKING", "1") != "0"


def _frontier_dfs() -> bool:
    """DFS (LIFO) frontier instead of the default BFS (FIFO). On sites with a wide nav
    mega-menu the seed page fans out to dozens of category links; BFS drains every
    depth-1 category before reaching a single depth-2/3 article, so a capped crawl
    never gets to real content. DFS dives article-first. Set CRAWLER_FRONTIER=dfs.
    Order only — full-budget coverage is identical; DFS just surfaces articles sooner."""
    return os.environ.get("CRAWLER_FRONTIER", "bfs").strip().lower() == "dfs"


def _render_mode(job) -> str:
    """How to FETCH each page:
      always — Playwright renders every page (the classic render-everything engine).
      auto   — fast httpx first; render ONLY pages that look JS-dependent, and escalate a
               whole host to always-render once httpx keeps coming up empty (a site that
               "won't open" without JS). Default — lets a static site (most WordPress news)
               be crawled at httpx speed while a real SPA still gets a browser.
      never  — httpx only, never render (fastest, misses JS-only content).
    An explicit per-job render_js=true forces 'always'; otherwise CRAWLER_RENDER_MODE wins.
    NOTE: httpx-served pages get no screenshot (that needs a browser) — use render_js=true
    if you need a screenshot of every page."""
    if getattr(job, "render_js", False):
        return "always"
    m = os.environ.get("CRAWLER_RENDER_MODE", "auto").strip().lower()
    return m if m in ("auto", "always", "never") else "auto"


def _depth_ok(depth: int, max_depth: int) -> bool:
    """Whether a page at `depth` may enqueue children. max_depth < 0 = UNLIMITED (crawl as deep as
    the site goes); else follow while depth < max_depth (0 = seed pages only)."""
    return max_depth < 0 or depth < max_depth


# ── link-follow heuristics: pagination priority + junk-link skip ─────────────────────────────
# errors.PAGE_PARAM is the SHARED param list, so the deep-page TRAP guard and this follow-heuristic
# always agree on what a page index looks like. It covers ?page=/?paged=/?pagi= (pixabay)/?pagina=/
# ?pageNum=, and deliberately not ?pagesize=/?per_page= (a page SIZE is not a page index).
_PAGINATION_RE = re.compile(rf"(?:[?&](?:{errors.PAGE_PARAM})=\d+|/page/\d+)(?=$|[&/])", re.I)
_ACTION_LINK_RE = re.compile(
    r"/(?:log-?in|log-?out|log-?off|sign-?in|sign-?out|vote|upvote|downvote"
    # The same control in the language the site is written in. /de/anmeldung and /es/iniciar-sesion
    # are logins, and on zentauron.de they were the doorway into the ?back= recursion below — the
    # English-only list walked straight past them.
    r"|anmeldung|iniciar-sesion|connexion|se-connecter|accedi|inloggen|zaloguj|entrar|giris"
    r")(?=$|[/?])|[?&]goto=", re.I)


# URLs a site's SOFTWARE emits, rather than pages a site has. Measured on the live fleet
# 2026-07-31, when it had stored 2 pages in an hour at load 79: 4,704 of 19,014 pending URLs were
# calendar exports, and the corpus held 22,823 same-bytes-under-different-url pages.
#
# What makes these worth their own rule rather than a bigger cap is that no amount of retrying can
# turn one into content, and each still costs a full fetch — these went down the RENDER lane and
# burned the 240s item watchdog four times over apiece, which is what wedged the fleet.
#
#   * CALENDAR EXPORTS. The Events Calendar (WordPress) hangs ?ical=1 / ?outlook-ical=1 off every
#     event, category and day, so the URL space is one per date with no end — gdbiw.com alone held
#     5,445 pending, 4,690 of them ical. The response is an .ics FILE, never a page.
#   * CALENDAR NAV. /day/2026-12-08/, and the ?tribe-bar-date= / ?eventDisplay= controls that
#     generate it. A calendar will happily emit 2031 as readily as last week.
#   * SELF-REFERENTIAL REDIRECTS. A login form carrying ?back=<the current url> re-encodes its own
#     URL each hop, so the percent-escaping grows without bound and every hop stores the SAME login
#     page: 78 distinct URLs under one content hash, 3,304 redundant pages on zentauron.de.
#     The value must LOOK like a URL — `?back=1` is a flag and stays crawlable.
#
# Deliberately NOT here: ?PAGEN_1= on an article (bounded already by the 20-variant facet cap) and
# deep nav permutations (the per-host deep-path cap owns those). This rule is only for shapes that
# are unbounded AND can never be content.
_GENERATOR_TRAP_RE = re.compile(
    r"[?&](?:ical|outlook-ical|ics)=|[?&]format=(?:ical|ics)(?=$|&)|"
    r"[?&](?:tribe-bar-date|eventdisplay|eventdate)=|"
    r"/(?:day|month|week)/\d{4}-\d{2}-\d{2}(?=$|[/?])|"
    r"[?&](?:back|return|returnurl|return_to|redirect|redirect_uri|redirect_to|next)="
    r"(?:https?(?::|%3a)|%2f%2f|//)", re.I)


# Cursor / keyset pagers carry an OPAQUE token, so _PAGINATION_RE's `=\d+` never matches:
# old.reddit ?after=t3_…, MediaWiki ?pagefrom=Aasmae…, Gutenberg ?start_index=51. All three ARE
# enqueued today, but as ordinary FACET params — not front-loaded, counted as depth+1, and capped
# at CRAWLER_MAX_QUERY_VARIANTS=20, which is what kills a long chain 20 pages in. Kept out of
# errors.PAGE_PARAM deliberately: that list also feeds the deep-page trap guard, and these count
# ITEMS or carry a cursor, so ?start_index=501 is page 21 of a search rather than a trap.
_CURSOR_RE = re.compile(
    r"[?&](?:start[-_]?index|pagefrom|page[-_]from|uefrom|after|cursor|"
    r"next_?(?:token|page|cursor)|continue|paging_?token)=[^&]", re.I)


# Two conventions the two regexes above structurally cannot match, both measured in the edge-case
# corpus. JSON:API spells its pager page[number]= / page[cursor]= / page[offset]= (percent-encoded
# page%5Bcursor%5D= on the wire), so the param NAME is not `page` and _PAGINATION_RE misses it;
# Solr/Lucene deep paging spells its cursor cursorMark=, where `cursor` is not immediately
# followed by `=` and _CURSOR_RE misses it. Consequence for both: the chain was treated as a facet
# explosion — depth+1 per page and dead at CRAWLER_MAX_QUERY_VARIANTS=20 pages.
_JSONAPI_RE = re.compile(r"[?&]page(?:\[|%5B)(?:number|offset|cursor|start)(?:\]|%5D)=[^&]", re.I)
_CURSORMARK_RE = re.compile(r"[?&]cursor[-_]?mark=[^&]", re.I)


# Junk a site's MARKUP mints, rather than its software. Measured on the live news fleet
# 2026-08-05, when 244,660 URLs were pending across only 75 hosts:
#
#   * SCHEMELESS RELATIVE LINKS. An href written `www.serco.com/aspac` — no scheme, no leading
#     `//` — is a relative PATH by RFC 3986, so urljoin correctly produces
#     https://asiapacificdefencereporter.com/boeing-beefs-up-ghost-bat/www.serco.com/aspac. The
#     link sits in a site-wide footer ad, so EVERY page mints its own variant, each of which
#     soft-404s into a page that mints more. No path segment legitimately begins with `www.`.
#   * CLOUDFLARE INTERNALS. /cdn-cgi/l/email-protection is the obfuscated-mailto decoder, injected
#     into every page Cloudflare proxies. It is machinery, never content.
#     The (?<!/) is load-bearing: without it the `//www.` of every ordinary https://www.host/ URL
#     matches and the rule swallows the whole crawl.
_BROKEN_RELATIVE_RE = re.compile(
    r"(?<!/)/www\.[a-z0-9-]+\.[a-z]{2,}(?=$|[/?])|/cdn-cgi/", re.I)


def _looks_paginated(url: str) -> bool:
    """A 'next page' link — ?p=/page=/pg=/paged=/start=/offset=N, /page/N, page[number]=N, or a
    cursor/keyset token (?cursor=, ?cursorMark=, page[cursor]=).
    These normally sit at the BOTTOM of a listing, so on a rate-limited or max_pages-capped crawl
    they lose their fetch slot to the nav/story links above them and the chain is never followed.
    Following the listing chain is usually the point of a paginated crawl, so _enqueue_links
    front-loads these. Shape-only (no anchor text) so it also works on the C1 path, which has only
    URLs."""
    return bool(_PAGINATION_RE.search(url) or _CURSOR_RE.search(url)
                or _JSONAPI_RE.search(url) or _CURSORMARK_RE.search(url))


async def _bounded_close(aw, timeout_s: float, what: str) -> bool:
    """Await a cleanup coroutine, giving up after *timeout_s*. Never raises.

    Returns True if the close actually completed. THE RETURN VALUE MATTERS: an abandoned close
    leaves a live process behind, and the caller is the only thing that can go kill it — see
    _kill_driver_tree and AsyncEngine.shutdown.

    Used for every close() in shutdown: a hung Chromium close is not worth blocking a batch on, and
    it silently did — see AsyncEngine.shutdown."""
    try:
        await asyncio.wait_for(aw, timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        log.warning("%s close timed out after %.0fs — abandoning it", what, timeout_s)
    except Exception:
        pass
    return False


def _driver_children() -> set:
    """PIDs of our direct child processes. Playwright's node driver is one of them."""
    if psutil is None:
        return set()
    try:
        return {p.pid for p in psutil.Process().children(recursive=False)}
    except Exception:                                  # noqa: BLE001
        return set()


# THE BROWSER POOL BELONGS TO THE PROCESS, NOT TO A BATCH.
#
# Three fixes were made to the old per-batch create/destroy cycle - _recycle leaving dead browsers
# behind, a timed-out teardown abandoning live trees, a failed start() skipping teardown entirely.
# Each was a real bug and each was verified fixed; load fell 235 -> 73 across them. The leak
# survived all three. MEASURED 2026-08-18, 90 minutes after the third rolled out fleet-wide:
#
#     fleet      : 612 chrome, 23 node drivers   (healthy is 12 - one per instance)
#     instance 7 : 6 batches started, 0 close timeouts, 0 kills fired, 4 drivers ALIVE
#
# Zero close timeouts is the finding. Every close and every stop() REPORTED SUCCESS - so the kill
# added by fix 2 correctly never fired - and four whole trees were alive anyway. A Playwright close
# that returns cleanly does not mean the OS process exited, and each fix had assumed it did, in a
# different place.
#
# So the cycle itself was the defect: a driver and browser set built and destroyed every ~3 minutes
# per instance, each destruction another chance to strand something no later pass can reach. Built
# ONCE per container, the failure mode has nowhere to live.
#
# CONTEXTS ARE DELIBERATELY NOT SHARED. They carry cookies and session state, so reusing them
# across batches would bleed one site's session into the next crawl. They are cheap; browser
# processes are not. Each batch opens its own and closes them in shutdown().
_SHARED_POOL: dict = {"pw": None, "pids": set(), "browsers": [], "W": 0}
# ONE BUILDER AT A TIME. Building the pool spans two awaits (driver start, then W launches), and a
# check-then-await is not exclusion: measured on the canary, two batches starting a second apart
# both read an empty pool, both built one, and the loser's driver tree was orphaned for the life of
# the process - the very leak the pool exists to remove. Created lazily rather than at import: a
# module-level asyncio.Lock() binds to whatever loop happens to be current at import time, which is
# not the engine loop.
_POOL_LOCK: "asyncio.Lock | None" = None


def _pool_lock() -> "asyncio.Lock":
    global _POOL_LOCK
    if _POOL_LOCK is None:
        _POOL_LOCK = asyncio.Lock()
    return _POOL_LOCK


def reset_shared_pool() -> None:
    """FORGET the shared pool without closing it. Tests only - nothing in production drops it."""
    global _POOL_LOCK
    _SHARED_POOL.update(pw=None, pids=set(), browsers=[], W=0)
    _POOL_LOCK = None


async def _shared_browsers(W: int) -> list:
    """The process-wide driver and its W browsers, launched on first use and reused forever.

    Never closed between batches: that close is precisely the operation measured reporting success
    while leaving the process alive. They die with the container, which is the one teardown the
    kernel actually guarantees.
    """
    if _SHARED_POOL["browsers"] and _pool_is_alive():
        if _SHARED_POOL["W"] != W:
            # Only reachable if CRAWLER_BROWSERS changed under a running process, which it cannot -
            # env is fixed at container start. Say so rather than silently serving a pool of a
            # different size than the caller sized its worker count for.
            log.warning("shared pool holds %d browser(s) but this batch asked for %d - reusing the "
                        "existing pool; restart the container to change CRAWLER_BROWSERS",
                        _SHARED_POOL["W"], W)
        return _SHARED_POOL["browsers"]

    async with _pool_lock():
        # RE-CHECK UNDER THE LOCK. The fast path above is only an optimisation; by the time this
        # coroutine holds the lock another batch may have finished building, and taking the branch
        # below anyway is what orphaned a driver tree on the canary.
        if _SHARED_POOL["browsers"] and _pool_is_alive():
            return _SHARED_POOL["browsers"]
        if _SHARED_POOL["browsers"]:
            # DEAD POOL. Measured on the live fleet 2026-08-31: four instances up five days each
            # reported their batch "done" with 0 pages fetched, every one of them dying at
            # `Browser.new_context: Target page, context or browser has been closed`. The pool is
            # built once for the life of the PROCESS and was never revalidated, so once its browsers
            # died - a crash, an OOM, a camofox restart - every later batch failed instantly and
            # reported success. Silent, and it survives until someone restarts the container.
            #
            # Forget the handles rather than closing them: close() on an already-dead browser is the
            # call that hangs, and this runs on the batch's critical path. The OS has already reaped
            # the processes; what leaks here is nothing.
            log.warning("shared browser pool is dead (browsers disconnected) - rebuilding")
            _SHARED_POOL.update(pw=None, pids=set(), browsers=[], W=0)
        return await _build_shared_pool(W)


def _pool_is_alive() -> bool:
    """Is every cached browser still usable?

    A pool that outlives its browsers is worse than no pool: new_context() raises TargetClosedError,
    the batch dies in start(), and the API still reports it complete with zero pages. Checked with
    is_connected(), which is a local flag on the Playwright object and costs nothing - deliberately
    not a probe, because this is on the path of every batch.
    """
    for b in _SHARED_POOL.get("browsers") or []:
        try:
            if not b.is_connected():
                return False
        except Exception:
            return False          # an object that cannot answer is not usable either
    return True


async def _build_shared_pool(W: int) -> list:
    """Launch the pool. Callers must hold _pool_lock()."""
    from playwright.async_api import async_playwright
    # Driver pid still captured by difference across the start call - it remains the only handle on
    # the node process, and _kill_driver_tree is still the last resort.
    _before = _driver_children()
    pw = await async_playwright().start()
    _SHARED_POOL["pw"] = pw
    _SHARED_POOL["pids"] = _driver_children() - _before

    headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
    browsers = []
    try:
        for _ in range(W):
            # Harden the primary tier: drop the automation fingerprint (webdriver flag + infobar) so
            # C1 stops drawing blocks that force premature C3 escalation. DoH args (secure mode)
            # only present when CRAWLER_DOH_TEMPLATE is set. Both no-op when CRAWLER_STEALTH=0.
            browsers.append(await pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", *stealth.stealth_launch_args(), *stealth.doh_launch_args()],
                ignore_default_args=stealth.stealth_ignore_default_args()))
    except BaseException:
        # A half-built pool must not be published, or every later batch borrows a short pool and the
        # missing browsers never appear. Drop what was built and let the caller fail; the next batch
        # rebuilds from scratch.
        _SHARED_POOL.update(pw=None, pids=set(), browsers=[], W=0)
        for b in browsers:
            await _bounded_close(b.close(), _env_float("CRAWLER_CLOSE_TIMEOUT_S", 5.0), "browser")
        await _bounded_close(pw.stop(), _env_float("CRAWLER_CLOSE_TIMEOUT_S", 5.0), "playwright")
        raise
    _SHARED_POOL["browsers"], _SHARED_POOL["W"] = browsers, W
    log.info("shared browser pool: %d browser(s) launched for the life of this process", W)
    return browsers


def _kill_driver_tree(pids, what: str = "playwright driver") -> int:
    """SIGKILL these processes and everything under them. Returns how many were killed.

    WHY THIS EXISTS — the leak that survived the _recycle fix.
    ------------------------------------------------------------------
    shutdown()'s docstring justified abandoning a hung close with "the process is ending and tini
    reaps the browser children anyway". THE PROCESS IS NOT ENDING. shutdown() runs at the end of
    every BATCH, inside a crawler-api container that then stays up for the next one. So an
    abandoned close does not get reaped by anything: tini reaps ZOMBIES, and a Chromium that
    ignored close() is not a zombie, it is alive and holding its RSS.

    Each batch calls async_playwright().start(), which forks a `node` driver that owns that batch's
    browsers. When teardown times out, that whole driver tree is orphaned from the engine's point
    of view but still running — and the next batch starts a fresh one beside it.

    MEASURED 2026-08-16 on the live fleet, and the shape is unmistakable once you count TOP-LEVEL
    chrome processes rather than all of them:

        instance 10 :   25 chrome,  2 browsers under ONE node driver      <- healthy
        instance 11 :  265 chrome, 24 browsers under TWELVE node drivers  <- twelve leaked batches
        instance  7 :  320 chrome
        fleet       : 2,266 chrome processes against a ~300 baseline

    It tracks batches, not uptime — every instance had the same 25h uptime — and `recycle:` appears
    0-2 times in those logs while "close timed out" appears 89-107 times. The fleet was down to
    0.14 pages/s on 0.2 cores with a load average near 300.

    Killing is safe here precisely BECAUSE the close was already abandoned: playwright has stopped
    tracking these, nothing will ever come back for them, and the pages they were rendering are
    already lost to the timeout.
    """
    if psutil is None:
        return 0
    killed = 0
    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except Exception:                              # noqa: BLE001 — already gone, which is fine
            continue
        try:
            victims = proc.children(recursive=True) + [proc]
        except Exception:                              # noqa: BLE001
            victims = [proc]
        for v in victims:
            try:
                v.kill()
                killed += 1
            except Exception:                          # noqa: BLE001
                pass
    if killed:
        log.warning("%s did not close — killed %d abandoned process(es)", what, killed)
    return killed


def _seed_with_dns(url: str) -> str:
    """If a seed's apex has no A record, seed `www.` instead.

    Measured on the 92 catalogued sites holding zero pages: THREE OF FIVE sampled had no apex A
    record at all while www resolved — crye.com, adsys.com and aisino.com all answer on www and
    nowhere else. Every one of those crawls started at a hostname that does not exist, fetched
    nothing, and was written off as a dead site.

    This is not a new idea in this codebase; it is an existing one in the wrong module.
    `sitemap._host_variants()` already tries apex + www, with the comment "many sites only publish
    the sitemap on www". Seeds never learned it.

    Deliberately narrow. It runs ONCE per seed (a job has one or two), only when the apex genuinely
    fails to resolve, and only to add `www.` — never to strip it, never on a host that already has
    it, and never on a resolution error that is not "no such name". A resolver hiccup under fleet
    fan-out must not silently rewrite a working seed, so anything other than a clean NXDOMAIN-shaped
    failure leaves the URL exactly as it was.
    """
    import socket

    try:
        p = urlsplit(url)
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    # A bare IP or a single-label name has no "www." form worth trying.
    if not host or host.startswith("www.") or "." not in host or host.replace(".", "").isdigit():
        return url
    try:
        socket.getaddrinfo(host, None)
        return url                              # apex resolves — nothing to do
    except socket.gaierror:
        pass
    except Exception:                           # noqa: BLE001 — never fail a crawl on a name lookup
        return url
    try:
        socket.getaddrinfo("www." + host, None)
    except Exception:                           # noqa: BLE001 — www is no better; keep the original
        return url                              # so the failure is reported against the real seed
    netloc = p.netloc.replace(host, "www." + host, 1)
    log.info("seed %s does not resolve but www. does — seeding https://www.%s instead", host, host)
    return p._replace(netloc=netloc).geturl()


def _is_action_link(url: str) -> bool:
    """A URL that is a CONTROL or a generator artefact, not a page.

    Three families, all storing nothing and all spending a fetch the real pages need:

      * action links — login/logout/vote and ?goto= redirect controls. Measured on Hacker News,
        ~half of a page's links were vote?/hide?/login? (all carrying ?goto=), and the 429 flood
        they set off is what buried the news?p=4 pagination link.
      * generator traps — calendar exports and self-referential redirects, whose URL space has no
        end because software mints it on demand. See _GENERATOR_TRAP_RE for what each one cost.
      * markup junk — schemeless relative hrefs and Cloudflare's own endpoints, which every page on
        a site mints identically. See _BROKEN_RELATIVE_RE.

    Deliberately conservative — only patterns that are near-universally controls, never a plain
    content path. This predicate is also what prunes an ALREADY-QUEUED frontier
    (CrawlHistory.frontier_prune), so the rule that stops junk arriving and the rule that removes
    the junk already there cannot drift apart.
    """
    return bool(_ACTION_LINK_RE.search(url) or _GENERATOR_TRAP_RE.search(url)
                or _BROKEN_RELATIVE_RE.search(url))


def _incremental_skip_eligible(incremental: bool, depth: int, fresh_depth: int) -> bool:
    """Whether a page may take the 304 unchanged-skip on an incremental recrawl. Requires the job
    flag AND the page to sit BELOW the always-fresh discovery surface (depth > fresh_depth). Seeds
    and the top listing layer (depth <= fresh_depth) always re-parse so new children behind an
    unchanged-LOOKING hub (byte-stable JS/AJAX list, template Last-Modified) are still discovered —
    the 304-skip returns before _enqueue_links, so a skipped hub contributes no links. Deep leaf
    pages (the many) still get the fast skip, where the win is."""
    return incremental and depth > fresh_depth


# SPA shells that ship no server-rendered content: an httpx GET sees an empty app root with
# no links, so BFS/DFS dead-ends. These markers + a thin body ⇒ "needs a real browser".
_SPA_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", "__next_data__",
                "window.__nuxt__", "data-reactroot", "ng-version", "data-server-rendered")


# Statuses a WAF uses to refuse a client it does not like, rather than to describe a real problem
# with the resource. 429/451 are rate-limit and legal-block; 454/456 are Akamai/Imperva vendor
# codes. All of them cleared with a real-Chrome JA3 GET in live measurement.
_IMPERSONATE_STATUSES = frozenset({401, 403, 405, 406, 409, 429, 451, 454, 456})


def _looks_thin(fr: "FetchResult") -> bool:
    """A 200 that is really a block page or an app shell — too small to be content.

    Worth a JA3 GET before a browser tab: thalesgroup.com served a 212-byte shell to plain httpx
    and 146 KB of real content to curl_cffi, which is a cheaper fix than rendering it.
    """
    if fr is None or fr.error or (fr.status or 0) != 200 or not fr.is_html():
        return False
    return len(fr.text_html or "") < _env_int("CRAWLER_IMPERSONATE_THIN_BYTES", 2000)


# A TLS-level refusal never becomes a status code — the handshake or the connection dies and httpx
# raises. Keying the JA3 tier on the status alone therefore misses the very blocks it exists to
# clear. Measured from the data centre: plain httpx could not complete a request to
# kalashnikovgroup.ru or projectq.ai at all, while curl_cffi returned 86,905 and 409,379 bytes.
#
# DNS failures are excluded on purpose: no TLS fingerprint resurrects a host with no A record, and
# firing on them would add a wasted request to every dead domain in the catalogue.
_DNS_ERR = ("name or service not known", "nodename nor servname", "temporary failure in name",
            "getaddrinfo", "no address associated", "name resolution")


def _transport_blocked(fr: "FetchResult") -> bool:
    if fr is None or fr.status or not fr.error:
        return False
    return not any(m in str(fr.error).lower() for m in _DNS_ERR)


def _is_js_shell_signal(fr: "FetchResult") -> bool:
    """Did this httpx result show the host genuinely needs JAVASCRIPT — as opposed to merely
    having failed?

    `_needs_render` deliberately conflates the two, and for the immediate "should I open a browser
    for this page" decision that is right: a 403 and a JS shell both mean "try something else".
    For the PERMANENT per-host escalation they are completely different evidence, and treating them
    alike is how 91 of 105 always-render hosts got there wrongly.

    MEASURED: of 105 hosts marked always-render, 91 answer a plain httpx GET with HTTP 200 and
    real content TODAY — General Dynamics, Fincantieri, Embraer, GRSE among them. Each was escalated
    because 3 of its first 4 pages "needed a render", where needing a render meant a transient
    block, a timeout or a rate-limit. `_note_render` returns early for a host already in the set,
    so nothing ever re-tested them: one bad minute, browsers forever. That list is what filled the
    render lane with 4,514 queued items draining at 5 at a time.

    A failure is not evidence about JavaScript. Only a page that FETCHED FINE and was still a thin,
    link-less shell is.

    The shell test itself is `_needs_render`'s — deliberately, not a second copy. Once the failure
    statuses are excluded above, the ONLY ways it can still answer True are its empty-body, SPA-and-
    thin and thin-and-link-less branches, which is precisely the evidence this predicate is about.
    Writing the test out again here is how the enqueue rule and the escalation rule drift apart.

    MEASURED, and the reason this is spelled out: the first version of this function ended
    `return fr.is_html()`, which is True of EVERY successful HTML page — so every ordinary page
    escalated its host, and the always-render set grew from 105 hosts to 329 in a day. The docstring
    above was right the whole time; the body did something else.
    """
    if fr is None or fr.error or not fr.status or fr.status >= 400:
        return False
    if not fr.is_html():
        return False
    return _needs_render(fr)


def _lane_host(item) -> str:
    """Host key for lane admission. Bare and total: an unparseable URL must not raise on the
    worker hot path, and an empty key simply opts the item out of admission control."""
    try:
        return (urlsplit(getattr(item, "url", "")).hostname or "").lower()
    except (ValueError, UnicodeError):
        return ""


def _host_backoff_delay(deflects: int) -> float:
    """How long a host-blocked item waits before trying again — EXPONENTIAL, not a flat timer.

    Every deflected item came back on a fixed CRAWLER_HOST_BUSY_REQUEUE_S (3s in production, and the
    give-up budget above deliberately does NOT fire for a host that is merely slow). With ~20,000
    items queued behind saturated hosts that is ~6,600 pointless wake-ups per second, and it is not
    theoretical — measured across the live fleet:

        inst 10  deflected_host 409,311  fetched 247    (1,657 deflections PER fetched page)
        inst  7  deflected_host 357,423  fetched 166
        inst  5  deflected_host   3,772  fetched 1,471  <- the FASTEST instance

    Deflections correlate inversely with throughput almost perfectly, because the re-examination is
    what eats the CPU that would otherwise be fetching. Backing off does not slow a working host: a
    host with free slots never reaches this path at all, and an item's counter is only ever bumped
    when it was actually turned away.

    Capped so a slot that frees late is still picked up promptly, and jittered because thousands of
    items deflected in the same instant would otherwise return in the same instant — the thundering
    herd this exists to break.
    """
    base = _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0)
    cap = _env_float("CRAWLER_HOST_BUSY_REQUEUE_MAX_S", 60.0)
    n = max(0, int(deflects) - 1)
    delay = min(base * (2 ** min(n, 10)), cap)
    return delay * (0.75 + random.random() * 0.5)


def _needs_render(fr: "FetchResult") -> bool:
    """True if an httpx result should be re-fetched through Playwright — a failure/block, or an
    HTML page too thin / link-less to be the real content (a JS-rendered shell). A healthy static
    page (real text + links) returns False and is served straight from httpx (fast)."""
    if fr is None:
        return True
    # A page that is GONE is not a page that is BLOCKED. 404/410/451 all answered definitively, and
    # no browser makes a deleted page exist — the 451 note in errors._STATUS_POLICY already says
    # exactly that about legal takedowns; it is just as true of the other two.
    #
    # Measured: news article URLs pulled straight from the live frontiers came back 404 and were
    # queued for a browser anyway. The manufacturer corpus alone holds 20,937 http_404 URLs, so at
    # ~15s a render this is on the order of 80 browser-hours spent confirming pages are gone — while
    # real pages wait behind them in a render queue 18,066 deep.
    if fr.status and errors.policy_for_status(fr.status) == errors.GONE:
        return False
    # Nor is a page that is RATE-LIMITED or SERVER-BROKEN a page that is blocked. Both said, in so
    # many words, "ask again later" — 429/503 as COOLDOWN, 408/425/500/502/504 as RETRY_LATER — and
    # a browser creates neither quota nor a working origin. The retry path already handles them.
    #
    # This costs real capacity, not a rounding error. MEASURED via cgroup deltas (ps reports a
    # LIFETIME AVERAGE and is useless here — three samples returned an identical 303%): one instance
    # burned 4.97 cores with 4 renders in flight, ~1.2 cores per concurrent render, because the data
    # centre has no GPU and everything rasterizes through llvmpipe in software. 40 cores is therefore
    # ~33 concurrent renders for the WHOLE box, the fleet already admits 48, and render_q stood at
    # 104,040. Every render spent confirming a 429 is a render a real page waits behind.
    #
    # 403 stays renderable on purpose: its policy is DISGUISE, which is precisely "the origin objects
    # to WHO we appear to be" — the one failure a browser genuinely answers, and the reason C3/C4
    # exist. Transport errors stay renderable too: a reset or handshake failure can be a TLS/JA3
    # block that a real browser clears.
    if fr.status and errors.policy_for_status(fr.status) == errors.RETRY_LATER:
        return False
    # 429 is named rather than taken from its bucket, because its bucket-mate is 503 and the two are
    # NOT the same evidence. 503 is the classic WAF-challenge status — an interstitial a browser
    # genuinely clears — so it keeps its render even though the policy table files both as COOLDOWN.
    # 429 carries no such ambiguity: it is a quota statement, and a browser does not create quota.
    # Taking the whole COOLDOWN bucket here would have closed the Cloudflare-challenge door, which
    # test_gone_is_not_blocked was right to guard.
    if fr.status == 429:
        return False
    if fr.error or not fr.status or fr.status >= 400:
        return True                          # failed / WAF-blocked → a browser may clear it
    if not fr.is_html():
        return False                         # pdf/image/binary: httpx already has the bytes
    html = fr.text_html or ""
    if not html.strip():
        return True
    text = parse.visible_text(html)
    try:
        links = parse.extract_links(html, fr.final_url or fr.url)
    except Exception:
        links = []
    low = html.lower()
    spa = any(m in low for m in _SPA_MARKERS)
    thin = len(text) < _env_int("CRAWLER_THIN_TEXT_CHARS", 400)
    if spa and thin:
        return True                          # app shell with no server-rendered content
    if thin and len(links) < 3:
        return True                          # nothing to extract, nowhere to go → probably JS
    return False


def _capture_assets(job, html: str | None, base: str) -> tuple[list, list, list]:
    """Extract PDF attachment links per the job's capture list. L1 captures TEXT + HTML + PDF only —
    images/media were dropped. Returns (pdf_links, [], []) to keep the shared HarvestedPage shape for
    the C1/C3 paths. ponytail: image/media extraction removed, not the tuple shape."""
    pdf_links: list = []
    if html and "pdf" in job.capture:
        try:
            pdf_links = parse.extract_pdf_links(html, base)
        except Exception as exc:
            log.debug("pdf extract failed base=%s: %s", base, exc)
    return pdf_links, [], []


def _env_float(k: str, d: float) -> float:
    return float(os.environ.get(k, str(d)))


# Hosts that should bypass the C1 live fetch and go straight to the CamoFox C3 stealth engine.
# Operators can list high-WAF domains explicitly; careful hosts (.gov/.mil) are also high-risk.
_WAF_HOST_LIST = None

def _waf_host_list() -> set[str]:
    global _WAF_HOST_LIST
    if _WAF_HOST_LIST is None:
        raw = os.environ.get("CRAWLER_C3_HOSTS", "").strip()
        _WAF_HOST_LIST = {h.lower().lstrip("www.").strip() for h in raw.split(",") if h.strip()}
    return _WAF_HOST_LIST


def _is_block_body(fr: "FetchResult | None") -> bool:
    """True if an OK-looking HTML response is actually a WAF wall. Guarded by CRAWLER_BLOCK_BODY_CHECK
    so it can be switched off if a target ever trips it. Only inspects HTML — a PDF/binary body is
    never a block page, and errors.is_ip_block_page needs distinctive phrasing to fire."""
    if fr is None or os.environ.get("CRAWLER_BLOCK_BODY_CHECK", "1") == "0":
        return False
    try:
        if not fr.is_html():
            return False
    except Exception:
        return False
    return errors.is_ip_block_page(fr.text_html or "")


def _is_waf_host(host: str) -> bool:
    """Proactive C3 trigger: high-WAF domains listed by operator, or careful gov/mil hosts."""
    if not host:
        return False
    h = host.lower().lstrip("www.").strip()
    if h in _waf_host_list():
        return True
    return errors.is_careful_host(host)


def _ignore_https_errors() -> bool:
    """Opt-in: let Chromium proceed past cert errors (self-signed / chain issues). OFF by
    default — a broken cert is a real trust failure. Turn on (CRAWLER_IGNORE_HTTPS_ERRORS=1)
    for cert-quirky sites that would otherwise hard-fail and get the whole host circuit-broken."""
    return os.environ.get("CRAWLER_IGNORE_HTTPS_ERRORS", "0") == "1"


async def _swallow(coro) -> None:
    """Await a coroutine, ignoring any error (used to fire-and-forget popup.close/download.cancel)."""
    try:
        await coro
    except Exception:
        pass


# Runs in the page context: mark up to `limit` NON-<a href> clickables (buttons, onclick handlers,
# ARIA link/button roles, data-href/data-url, and cursor:pointer elements) with a data-__cd index
# so Playwright can target them, and return [{idx, sig}] where sig is a stable text/tag signature
# for dedup. Plain <a href=http…> is excluded — extract_links already enqueues those.
_CANDIDATE_JS = """
(limit) => {
  const sel = 'button,[onclick],[role="link"],[role="button"],[data-href],[data-url]';
  const set = new Set(document.querySelectorAll(sel));
  const all = document.querySelectorAll('body *');
  for (let i = 0; i < all.length && set.size < limit * 4; i++) {
    const e = all[i];
    try { if (getComputedStyle(e).cursor === 'pointer') set.add(e); } catch (_) {}
  }
  const out = [];
  let i = 0;
  for (const e of set) {
    if (i >= limit) break;
    if (e.tagName === 'A' && e.getAttribute('href') && /^https?:/i.test(e.href)) continue;
    e.setAttribute('data-__cd', String(i));
    const txt = (e.innerText || e.textContent || '').trim().slice(0, 60);
    const dh = e.getAttribute('data-href') || e.getAttribute('data-url') || '';
    out.push({ idx: i, sig: e.tagName + '|' + txt + '|' + (e.getAttribute('onclick') ? '1' : '') + '|' + dh });
    i++;
  }
  return out;
}
"""


def _job_respects_robots(job, caps: dict) -> bool:
    """Whether this job honors robots.txt. Precedence: explicit job.respect_robots >
    env CRAWLER_RESPECT_ROBOTS > seed capture default. False = bypass (fetch every page
    directly, no robots gate and no robots→C3 detour). The in-code fallback stays at the
    seed value so clearing the env fails safe (back to polite)."""
    if job.respect_robots is not None:
        return job.respect_robots
    env = os.environ.get("CRAWLER_RESPECT_ROBOTS")
    if env is not None:
        return env == "1"
    return bool(caps.get("respect_robots_txt", True))


def _proxy_config() -> dict | None:
    """Playwright proxy= dict from CRAWLER_PROXY_URL, or None (direct connection).

    The actual fix for IP-blocked hosts (war.gov etc.): route Chromium through a
    residential/mobile proxy gateway, which rotates the exit IP per request on the
    provider side. Datacenter proxies DON'T help — they're on the same blocklists as
    a server IP; the provider MUST be residential/mobile (BrightData/Oxylabs/IPRoyal/…).

    Format: CRAWLER_PROXY_URL=http://user:pass@gateway.provider.com:7777
    (scheme http or socks5; auth optional). Returns the dict Playwright wants:
      {server, username?, password?}
    """
    raw = os.environ.get("CRAWLER_PROXY_URL", "").strip()
    if not raw:
        return None
    p = urlsplit(raw)
    if not p.hostname:
        log.warning("CRAWLER_PROXY_URL set but unparseable: %r — ignoring", raw)
        return None
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    cfg: dict = {"server": server}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _feed_items(feed_url: str, timeout_s: float) -> list[dict]:
    """Fetch an RSS/Atom feed (un-gated) and return its items as dicts:
    {link, title, summary, published}. The fallback for WAF-blocked HTML — feeds
    are rarely bot-challenged, and the feed body ALREADY carries title+summary, so
    when the item pages are themselves blocked we emit the feed content directly.
    Handles RSS (<item>) and Atom (<entry>). [] on any error.
    ponytail: no feedparser dep; stdlib ElementTree covers RSS+Atom.
    """
    import httpx
    from xml.etree import ElementTree as ET

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA_FALLBACK,
                                   "Accept": "application/rss+xml, application/xml, */*"}) as c:
            r = c.get(feed_url)
        if r.status_code >= 400 or not r.content.strip():
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []

    def _txt(el, *names):
        for n in names:
            for ch in el:
                if ch.tag.rsplit("}", 1)[-1].lower() == n:
                    return (ch.get("href") or ch.text or "").strip()
        return ""

    items: list[dict] = []
    seen: set[str] = set()
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        link = _txt(el, "link", "id")
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        items.append({
            "link": link,
            "title": _txt(el, "title"),
            "summary": _txt(el, "description", "summary", "content"),
            "published": _txt(el, "pubdate", "published", "updated"),
        })
    return items


def _api_items(api_url: str, timeout_s: float) -> list[dict]:
    """Fetch an official JSON API (DVIDS) → items {link, title, summary, published}.

    The SANCTIONED path for WAF-blocked DoD sites: same news/press content that
    renders on war.gov/defense.gov, served WAF-free as JSON. DVIDS needs a free
    key (DVIDS_API_KEY env) appended as ?api_key=. Returns [] on any error / no key.
    Shape: DVIDS returns {"results":[{id,title,short_description,date_published,url}]}.
    ponytail: DVIDS-shaped for now; generalize the field map only if a 2nd JSON API appears.
    """
    import httpx

    key = os.environ.get("DVIDS_API_KEY", "").strip()
    if "dvidshub.net" in api_url and not key:
        return []                       # DVIDS search 400s without a key — don't waste the call
    params = {"api_key": key} if key else {}
    # Ask DVIDS for exactly the fields we map, newest first.
    if "dvidshub.net" in api_url:
        params.update({"fields": "id,title,short_description,date_published,url",
                       "sort": "date", "sortdir": "desc", "max_results": "50"})
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA_FALLBACK,
                                   "Accept": "application/json"}) as c:
            r = c.get(api_url, params=params)
        if r.status_code >= 400:
            return []
        data = r.json()
    except Exception:
        return []

    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []
    items: list[dict] = []
    for it in results:
        if not isinstance(it, dict):
            continue
        link = (it.get("url") or it.get("link") or "").strip()
        if not link.startswith("http"):
            continue
        items.append({
            "link": link,
            "title": (it.get("title") or "").strip(),
            "summary": (it.get("short_description") or it.get("description")
                        or it.get("summary") or "").strip(),
            "published": (it.get("date_published") or it.get("published") or "").strip(),
        })
    return items


# ── work item + per-job context ──────────────────────────────────────────────

@dataclass
class WorkItem:
    url: str
    depth: int
    ctx: "JobCtx"
    retries: int = 0        # in-run retry count (transient failures get one requeue)
    parent_url: str | None = None   # page this link was found on (None = seed root)
    deadhost_waits: int = 0  # times requeued to wait out a host cooldown (bounded; not a failure)
    captcha_flagged: bool = False   # a captcha limitation was already recorded → don't also netpath it
    needs_render: bool = False       # httpx already proved this page needs a browser
    render_attempts: int = 0        # renders spent on this URL; bounded by CRAWLER_RENDER_MAX_ATTEMPTS
    host_deflects: int = 0          # times bounced off the host-busy guard (named in the give-up evidence)
    first_deflect_at: float = 0.0   # monotonic time of the first bounce; a wall-clock budget then caps
    #                                 the spin so a blocked host can't livelock the render lane
    last_render_error: str = ""     # kept so the issues log can name the ACTUAL failure, not "gave up"
    last_body: str = ""             # last body seen for this URL, so the give-up cause can be
    #                                 classified from real markup instead of guessed from the error


class JobCtx:
    """Per-job state carried on every WorkItem. Mutated ONLY on the loop thread."""

    def __init__(self, job: Job, seed: Seed, kp, forward: bool) -> None:
        self.job = job
        self.seed = seed
        self.kp = kp                    # global keyword-corpus trie (keep-gate)
        self.crawl_run_id: str | None = None   # set by run() — batch lineage stamped on every doc

        # Forward every kept page to the Ingest API (Postgres + MinIO) when forward=True.
        forwarders: list[HttpIngestClient] = []
        self.forwarded_targets: list[str] = []
        if forward:
            forwarders.append(HttpIngestClient())
            self.forwarded_targets.append(config.INGEST_BASE_URL)
        self.ingest = CollectingIngestClient(forwarders=forwarders)

        # Asset-only fetcher: never renders (the pool renders); screenshots are captured
        # inline in _render_page and set on FetchResult.screenshot_png.
        caps = seed.capture_defaults
        self.fetcher = Fetcher(
            user_agent=caps["user_agent"], timeout_s=caps.get("timeout_seconds", 30),
            render_js=False, respect_robots=False, screenshot_wanted=False,
            # Per-host "Advanced -> Proceed anyway"; empty for every job that names no host.
            tls_insecure_hosts=list(getattr(job, "tls_insecure_hosts", None) or []),
        )

        self.seed_domains = {canonicalize_url(s) for s in job.seed_urls}
        self.seen: set[str] = set()
        # Shortest hop-distance known for each seen URL. Same keys as `seen`, so it costs one int
        # per URL already tracked. Exists because `seen` alone makes first sight final, and under
        # the LIFO frontier first sight is the depth of a DESCENT — see frontier_relax.
        self.depth_at: dict[str, int] = {}
        self.seen_hashes: set[str] = set()
        self.budget_used = 0
        self.done = False
        # Set by run()'s finalizer when the batch was cut short (operator STOP / wall-clock chop),
        # so summary()["stop_reason"] can say so instead of defaulting to "completed".
        self.interrupted = False
        # Latched the first time this job was refused for being in the drop set — an operator
        # switching its category off. Latched (unlike the check itself) because it decides the END
        # status, and a job cut off part-way MUST end "stopped" so its frontier survives.
        self.was_dropped = False

        # counters (the API summary shape)
        self.fetched = self.kept = self.sent = self.accepted = self.rejected = self.errors = 0
        self.not_modified = self.dropped_by_gate = self.skipped_unchanged = self.skipped_duplicate = 0
        # Split the old lumped "errors" into three honest buckets: real fetch failures (errors),
        # breaker/policy skips of queued URLs never fetched (skipped), and blocked-but-recovered
        # via C2/C3/API (recovered). errors_by_reason still holds every typed reason.
        self.skipped = self.recovered = 0
        self.gate_reasons: dict[str, int] = {}
        self.errors_by_reason: dict[str, int] = {}   # typed why-did-it-fail breakdown
        self.trap_skipped = 0                         # URLs dropped by trap heuristics
        self.scope_skipped = 0                        # URLs dropped for leaving job.path_scope
        # Phase-3 trap state: per (host,path) count of query-only variants seen (calendar/facet cap).
        self.query_variants: dict[str, int] = {}
        # Per-HOST count of DEEP paths enqueued — the menu-accumulation cap. See _should_enqueue.
        self.deep_paths: dict[str, int] = {}
        # Per-HOST count of QUERY-variant URLs enqueued — the many-paths-x-20-variants cap.
        self.host_queries: dict[str, int] = {}

    def reserve(self) -> bool:
        """Budget gate at dequeue — bounds total render attempts to max_pages (+in-flight).
        max_pages <= 0 means UNLIMITED: crawl the WHOLE site (bounded only by the frontier
        draining + the engine wall-clock), for 'fetch every page first, filter later' runs."""
        if self.job.max_pages > 0 and self.budget_used >= self.job.max_pages:
            self.done = True
            return False
        # The category switch, checked at the single choke point every fetch passes through. NOT
        # latched into `done`: that is a one-way gate, and switching a category back on mid-batch
        # would otherwise leave its jobs dead until the next dispatch.
        if job_dropped(self.job.job_id):
            self.was_dropped = True
            return False
        self.budget_used += 1
        return True

    def unreserve(self) -> None:
        """Give a page's budget back when a transient failure is requeued for one more try.
        Loop-thread-only mutation with no await between fail-detect and requeue → race-safe."""
        if self.budget_used > 0:
            self.budget_used -= 1

    def has_budget(self) -> bool:
        return self.job.max_pages <= 0 or self.budget_used < self.job.max_pages

    def bump_reason(self, reason: str) -> None:
        self.gate_reasons[reason] = self.gate_reasons.get(reason, 0) + 1

    def bump_error(self, reason: str) -> None:
        """A REAL fetch failure (DNS/refused/SSL/http-4xx-5xx/render/timeout)."""
        self.errors += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def bump_skip(self, reason: str) -> None:
        """A queued URL SKIPPED without a fetch (breaker host-down / known-gone / off-peak /
        robots). Not a failure — kept out of `errors` so the total isn't misleading."""
        self.skipped += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def bump_recovered(self, reason: str) -> None:
        """A blocked page RECOVERED via a fallback tier (C3 CamoFox / C2 archive / official API)."""
        self.recovered += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def summary(self) -> dict:
        return {
            "fetched": self.fetched, "not_modified_304": self.not_modified,
            "dropped_by_gate": self.dropped_by_gate, "skipped_unchanged": self.skipped_unchanged,
            "skipped_duplicate": self.skipped_duplicate, "kept": self.kept,
            "sent": self.sent, "accepted": self.accepted, "rejected": self.rejected,
            "errors": self.errors, "skipped": self.skipped, "recovered": self.recovered,
            "errors_by_reason": self.errors_by_reason,
            "trap_skipped": self.trap_skipped, "scope_skipped": self.scope_skipped,
            "gate_reasons": self.gate_reasons,
            "forwarded_to": self.forwarded_targets, "crawl_run_id": self.crawl_run_id,
            # Why this site stopped, so the run log can say it instead of leaving the operator to
            # guess whether a short crawl means "site is small" or "we cut it off at the budget".
            # Deliberately NOT `self.done`: that is a latch set the first time reserve() refuses, and
            # unreserve() hands budget back afterwards — so a job that merely grazed the cap mid-run
            # and then drained its frontier reported "max_pages" with budget_used far below it
            # (measured: budget_used=9 of max_pages=40 still claimed max_pages). Compare the FINAL
            # budget instead: still at the cap = we really were cut off.
            "max_pages": self.job.max_pages,
            "budget_used": self.budget_used,
            # Three outcomes, not two. This used to be max_pages-or-"completed", with "completed" as
            # a catch-all `else` — so a job the operator STOPPED reported success. Live consequence:
            # gem.gov.in was cut off after ONE page of a 68-URL frontier and the run log showed it
            # with a green "completed" badge, indistinguishable from a site that had genuinely been
            # crawled out. "stopped" is the same distinction _job_end_status draws for the Resume
            # list, and for the same reason — reaching max_pages IS finishing, being cut off is not.
            "stop_reason": ("max_pages"
                            if self.job.max_pages > 0 and self.budget_used >= self.job.max_pages
                            else _job_end_status(self, self.interrupted)),
        }


def _job_end_status(ctx, interrupted: bool) -> str:
    """How a job finished: "stopped" (keeps its frontier → shows in Resume) or "completed" (frontier
    dropped → gone from Resume).

    "Interrupted" is the operator hitting STOP or the engine wall-clock chopping the batch: the crawl
    still had work queued and a resume genuinely continues it.

    Reaching `max_pages` is NOT that, even though it also leaves URLs pending. The job did exactly
    what it was asked to do; a resume would restore the spent budget and stop again immediately, so
    listing it as resumable offers the operator a button that does nothing. Same for a job whose
    frontier simply drained — there is nothing left to pick up."""
    if ctx.job.max_pages > 0 and ctx.budget_used >= ctx.job.max_pages:
        return "completed"
    # Switched off by category mid-crawl. Same class as the operator STOP above and it MUST report
    # the same way: this job still has a live frontier, and "completed" is the status that drops it.
    if getattr(ctx, "was_dropped", False):
        return "stopped"
    return "stopped" if interrupted else "completed"


# ── CPU-feedback adaptive concurrency (fill the cores; the GIL only caps orchestration) ──────

_CPUSET_PREV: dict = {}


def _cpuset_cpu_percent() -> float | None:
    """How busy are the cores THIS container is allowed to run on. None when we own the whole box.

    Same defect as the RAM brake, in the other resource. `psutil.cpu_percent()` is host-wide, so a
    crawler pinned to 20 of 40 cores reads 50% while its own allocation is completely saturated —
    and CRAWLER_CPU_TARGET (85) then keeps admitting renders into cores that have nothing left.
    Over-admission on a starved lane is a failure this fleet has already had twice.

    Deliberately NOT our own cgroup's usage: twelve instances share one cpuset, and each backing off
    only when IT is hot would let the twelve of them cook the shared cores. The signal that was
    always right is "how hot are the cores I run on", counting every container on them — this just
    stops measuring cores we were never allowed to touch.

    Returns None when the affinity mask is every CPU, so an unpinned deployment keeps the exact
    host-wide reading its thresholds were tuned against.
    """
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return None
    if len(allowed) >= (os.cpu_count() or len(allowed)):
        return None                                  # unpinned — psutil's reading is the right one
    try:
        busy = idle = 0
        with open("/proc/stat") as fh:
            for line in fh:
                if not line.startswith("cpu") or line[3] == " ":
                    continue
                parts = line.split()
                try:
                    n = int(parts[0][3:])
                except ValueError:
                    continue
                if n not in allowed:
                    continue
                f = [int(x) for x in parts[1:8]]     # user nice system idle iowait irq softirq
                idle += f[3] + f[4]
                busy += f[0] + f[1] + f[2] + f[5] + f[6]
    except (OSError, IndexError, ValueError):
        return None
    prev = _CPUSET_PREV.get("v")
    _CPUSET_PREV["v"] = (busy, idle)
    if prev is None:
        return None                                  # first call primes, same contract as psutil
    db, di = busy - prev[0], idle - prev[1]
    if db + di <= 0:
        return None
    return round(100.0 * db / (db + di), 1)


def _cgroup_ram_percent() -> float | None:
    """Memory pressure against OUR OWN cgroup limit, or None when we are not capped.

    THE LIMIT THE ENGINE CANNOT SEE IS THE LIMIT THAT KILLS IT. Everything below reads
    /proc/meminfo, which inside a container is the HOST — so an instance capped at 28g on a 1007 GiB
    box reads ~10% RAM, never trips CRAWLER_RAM_HARD, and walks straight into an OOM kill with the
    brake untouched. That is not hypothetical: three instances nearly OOMed at a 20g cap while the
    host had 845 GB free, and the response at the time was to raise the cap to 40g — treating the
    symptom, because the governor was never going to fire at any cap.

    Page cache is subtracted. It counts toward memory.current and is reclaimed under pressure, so
    charging it as pressure would have the crawler shedding renders because it had read some files.

    Returns the percentage of the CAP in use, so the existing RAM_TARGET/RAM_HARD thresholds keep
    their meaning — they simply start measuring the right denominator.
    """
    try:                                            # cgroup v2
        with open("/sys/fs/cgroup/memory.max") as fh:
            raw = fh.read().strip()
        if raw == "max":
            return None
        limit = int(raw)
        with open("/sys/fs/cgroup/memory.current") as fh:
            used = int(fh.read().strip())
        cache = 0
        try:
            with open("/sys/fs/cgroup/memory.stat") as fh:
                for line in fh:
                    k, _, v = line.partition(" ")
                    if k == "inactive_file":
                        cache = int(v)
                        break
        except OSError:
            cache = 0
    except OSError:
        try:                                        # cgroup v1
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as fh:
                limit = int(fh.read().strip())
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as fh:
                used = int(fh.read().strip())
            cache = 0
        except OSError:
            return None
    # An "unlimited" v1 cgroup reports a number near 2^63; anything at host scale is not a cap.
    if limit <= 0 or limit >= (1 << 62):
        return None
    return round(100.0 * max(0, used - cache) / limit, 1)


def _ram_percent() -> float:
    """Memory pressure as a percentage of what this machine can ACTUALLY hand out.

    OUR CGROUP CAP WINS when there is one — see _cgroup_ram_percent. The host reading below is what
    matters on an uncapped process, and it is what every threshold was tuned against, but under a
    container limit it measures the wrong machine entirely.

    psutil's percent is the wrong input for a throttle on a ballooned guest. This is a VMware VM
    whose hypervisor holds ~590 GB of its 1082 GB: those pages are pinned inside the guest and
    counted as used, against a total the VM does not have. psutil therefore reads 63-65% while the
    crawler holds 4 GB of a 257 GB cap.

    That number feeds the AIMD limiter below, where CRAWLER_RAM_TARGET (85) gates growth and
    CRAWLER_RAM_HARD (92) sheds load outright. The balloon has been climbing — 52, 58, 63, 65 —
    and had it reached 85 the crawler would have throttled its own render pool on memory pressure
    that does not exist. Correcting the input is the fix; the thresholds are fine.

    Falls back to psutil when the balloon counters are absent (i.e. any non-ballooned host), so
    this changes nothing anywhere else.
    """
    cg = _cgroup_ram_percent()
    if cg is not None:
        return cg
    try:
        mi = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mi[k] = int(v.strip().split()[0]) * 1024
        total, avail = mi["MemTotal"], mi["MemAvailable"]
        ballooned = 0
        try:
            vm = {}
            with open("/proc/vmstat") as fh:
                for line in fh:
                    k, _, v = line.partition(" ")
                    vm[k] = int(v)
            ballooned = max(0, vm.get("balloon_inflate", 0)
                            - vm.get("balloon_deflate", 0)) * os.sysconf("SC_PAGE_SIZE")
        except Exception:                       # noqa: BLE001 — no balloon: ordinary reading
            ballooned = 0
        usable = max(1, total - ballooned)
        return round(100.0 * max(0, usable - avail) / usable, 1)
    except Exception:                           # noqa: BLE001
        try:
            return psutil.virtual_memory().percent if psutil is not None else 0.0
        except Exception:                       # noqa: BLE001
            return 0.0


def _aimd_limit(cpu: float, limit: int, target: int, step_up: int, lo: int, hi: int,
                ram: float = 0.0, ram_target: float = 101.0, ram_hard: float = 101.0) -> int:
    """Additive-increase / multiplicative-decrease toward a CPU target — the same self-damping rule
    TCP uses. Below (target-5) there's headroom → admit more renders; above target → back off.
    RAM-aware: a hard RAM brake (ram >= ram_hard) sheds load regardless of CPU, and growth is gated on
    RAM also having headroom (ram < ram_target) so we never fill the cores into an OOM. RAM defaults
    (101) make it a pure CPU controller when RAM isn't supplied. Clamped to [lo, hi]."""
    if ram >= ram_hard:
        limit = int(limit * 0.85)            # hard RAM brake — shed load before OOM, ignore CPU
    elif cpu < target - 5 and ram < ram_target:
        limit += step_up                     # headroom on BOTH cpu and ram → admit more renders
    elif cpu > target:
        limit = int(limit * 0.85)
    return max(lo, min(hi, limit))


class RenderDeferred(Exception):
    """httpx fetched the page, the page needs a browser, and the item has been moved to the render
    lane. Not a failure: the URL's links were already harvested from the httpx body and the render
    will run on a render-lane worker. Raised so the caller stops processing this item WITHOUT
    recording an error or storing the JS shell as a document."""


class HostSlotBusy(Exception):
    """This host's permits were all taken for longer than we are willing to hold a WORKER.

    The per-host acquire used to be unbounded, and it sits inside the per-item watchdog, so an item
    queued behind a busy host simply waited until the 240s watchdog killed it — and the worker
    awaiting it was pinned for those 240s and could serve no other host meanwhile. That is the
    starvation, not the rendering: measured 3,464 abandonments in twelve minutes, on hosts whose
    pages actually render in 4.7s and 8.6s. shvabe.com alone had 12,546 URLs queued against two
    permits, so items piled onto the semaphore and were killed there in their thousands.

    The render lane already had this guard (RenderBusy); the httpx path never did, on the reasoning
    that an httpx item holds no tab so waiting is free. It is not free — it costs the worker.
    """


class RenderBusy(Exception):
    """No render permit available for this item right now — requeue it, don't park a tab on it.

    Raised instead of waiting when the global render pool is saturated or this host already holds
    its share. Measured why: the adaptive gate is acquired BEFORE the per-host slot, so a tab
    waiting for a render permit is invisible to per-host accounting. One host held all 64 permits
    while every other host's render-needing items sat on the gate until the item watchdog killed
    them — 99 timeouts on one host in 3 minutes with zero HTTP requests issued."""


class AdaptiveGate:
    """A GLOBAL admission permit whose ceiling a CPU sampler grows/shrinks at runtime, so total
    concurrent renders track a CPU target instead of blindly running all 96 tabs (which either
    thrashes at 100% or, when render-heavy, is the only way to use the cores). asyncio.Semaphore
    can't be resized, so this is a counter + Condition. When ``limit == max_limit`` it never blocks
    (a no-op), so wrapping every render is safe even with the controller disabled."""

    def __init__(self, initial: int, min_limit: int, max_limit: int) -> None:
        self.min_limit = max(1, min_limit)
        self.max_limit = max(self.min_limit, max_limit)
        self.limit = max(self.min_limit, min(self.max_limit, initial))
        self.active = 0
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def slot(self, timeout: float | None = None):
        """Acquire a render permit. With *timeout*, raise RenderBusy rather than waiting forever —
        the caller requeues the item instead of holding a tab hostage on a saturated pool."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        async with self._cond:
            while self.active >= self.limit:
                if deadline is None:
                    await self._cond.wait()
                    continue
                rem = deadline - loop.time()
                if rem <= 0:
                    raise RenderBusy(f"no render permit in {timeout}s ({self.active}/{self.limit})")
                try:
                    await asyncio.wait_for(self._cond.wait(), rem)
                except asyncio.TimeoutError:
                    raise RenderBusy(
                        f"no render permit in {timeout}s ({self.active}/{self.limit})") from None
            self.active += 1
        try:
            yield
        finally:
            async with self._cond:
                self.active -= 1
                self._cond.notify()

    async def set_limit(self, n: int) -> int:
        n = max(self.min_limit, min(self.max_limit, n))
        async with self._cond:
            grew = n > self.limit
            self.limit = n
            if grew:
                self._cond.notify(n)         # wake blocked acquirers into the new headroom
        return n


# ── per-host politeness (shared across the whole pool) ───────────────────────

def _parse_host_overrides(raw: str) -> dict[str, int]:
    """CRAWLER_HOST_CONCURRENCY_OVERRIDE='sipri.org=3,mod.gov.my=1' → {host: concurrency}.
    For weak/flaky NO-BOT servers that drop connections: cap concurrency AND keep them on the light
    httpx path (no forced render, no proactive C3) — render just gives a flaky box more sockets to
    drop. Pure so it unit-tests without a browser."""
    out: dict[str, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        h, _, v = part.partition("=")
        h = h.strip().lower().lstrip(".")
        try:
            n = int(v.strip())
        except ValueError:
            continue
        if h and n > 0:
            out[h] = n
    return out


def _match_override(host: str, overrides: dict[str, int]) -> int | None:
    """Concurrency cap for host if it (or a parent domain) is in the override map, else None.
    Segment-suffix match: 'sipri.org' matches www.sipri.org but not evilsipri.org."""
    host = (host or "").lower().rstrip(".")
    for key, n in overrides.items():
        if host == key or host.endswith("." + key):
            return n
    return None


def _hkey(host: str) -> str:
    """The politeness identity of a host. `www.bhel.com` and `bhel.com` are ONE server, so they must
    share one semaphore and one delay clock.

    canonicalize_url deliberately keeps the `www.` in the fetch URL (stripping it breaks sites whose
    apex path-redirects to the www root), and this limiter keyed its dicts on that raw hostname — so
    a site linked both ways got two of everything and the origin took 2x the configured concurrency
    at 2x the rate. Measured on the 2026-07-28 batch: bhel.com and www.bhel.com ran as separate
    hosts for 4,446 issue events. Only the politeness KEY is normalised; the URL we fetch is
    untouched."""
    return (host or "").lower().rstrip(".").removeprefix("www.")


class HostLimiter:
    """≤ max_conc concurrent AND ≥ min_delay apart, per hostname. The politeness guarantee
    that lets 140 tabs run without hammering any single site."""

    def __init__(self, max_conc: int, min_delay: float, robots: RobotsCache | None = None,
                 base_timeout_s: float = 30.0) -> None:
        self.max_conc = max_conc
        self.min_delay = min_delay
        self.robots = robots
        self.base_timeout_s = base_timeout_s
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._delay: dict[str, float] = {}
        self._next_ok: dict[str, float] = {}
        self._inflight: dict[str, int] = {}
        self._timeout: dict[str, float] = {}      # per-host patient timeout (ms), seeded in _ensure
        self._cap: dict[str, int] = {}            # live concurrency cap per host (grown by retarget)
        self.peak_inflight: dict[str, int] = {}   # for verification (≤ the host's CURRENT _cap)
        # Hosts forced into careful-mode by a job with careful=True (not just .gov/.mil suffix).
        self.force_careful: set[str] = set()
        # Per-host concurrency caps for weak/flaky no-bot servers (see _parse_host_overrides).
        self.overrides = _parse_host_overrides(os.environ.get("CRAWLER_HOST_CONCURRENCY_OVERRIDE", ""))
        # Engine wires this to _mark_progress so a tab asleep on a host cooldown heartbeats the
        # idle-timer — a cooling host is legit progress, not a wedge. None until run() sets it.
        self._progress = None
        self._waiting = 0            # tabs currently parked in a slot cooldown/delay wait (diagnostic)
        self._queued: dict[str, int] = {}   # per-host tabs blocked on the permit (see slot/queue_depth)

    def timeout_ms(self, host: str) -> float:
        """Per-host patient timeout in ms (bigger for careful hosts; ratchets on TIMEOUT)."""
        host = _hkey(host)
        return self._timeout.get(host, self.base_timeout_s) * 1000

    def bump_timeout(self, host: str) -> None:
        """A slow gov origin timed out — extend its patience (bounded) before the breaker counts it."""
        host = _hkey(host)
        cap = _env_float("CRAWLER_MAX_TIMEOUT_S", 120.0)
        cur = self._timeout.get(host, self.base_timeout_s)
        self._timeout[host] = min(cur * 1.5, cap)

    def override_for(self, host: str) -> int | None:
        """The per-host concurrency cap for host (weak/flaky no-bot server), or None."""
        host = _hkey(host)
        return _match_override(host, self.overrides)

    async def _ensure(self, host: str) -> None:
        host = _hkey(host)
        if host not in self._sem:
            # Created synchronously before any await → no race creating two semaphores.
            # Precedence: an explicit per-host OVERRIDE (weak/flaky no-bot server) → its cap + a
            # patient timeout, staying on the light httpx path. Else CAREFUL (.gov/.mil / careful=True)
            # → concurrency 1, slow delay, patient timeout ("quiet quiet"). Else the pool default.
            ov = _match_override(host, self.overrides)
            careful = errors.is_careful_host(host) or host in self.force_careful
            if ov is not None:
                conc = ov
                self._delay[host] = max(self.min_delay, _env_float("CRAWLER_OVERRIDE_DELAY_S", 1.0))
                factor = _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)
            elif careful:
                conc = 1
                self._delay[host] = max(self.min_delay, _env_float("CRAWLER_CAREFUL_DELAY_S", 5.0))
                factor = _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)
            else:
                conc = self.max_conc
                self._delay[host] = self.min_delay
                factor = 1.0
            self._sem[host] = asyncio.Semaphore(conc)
            self._cap[host] = conc
            self._timeout[host] = self.base_timeout_s * factor
            self._next_ok[host] = 0.0
            self._inflight[host] = 0
            self.peak_inflight[host] = 0
            if self.robots:
                try:  # robots crawl_delay is a blocking httpx fetch → off-loop
                    d = await asyncio.to_thread(self.robots.crawl_delay, f"https://{host}/")
                    if d:
                        self._delay[host] = max(self.min_delay, float(d))
                except Exception:
                    pass

    def make_careful(self, host: str) -> None:
        """Mid-crawl promotion to careful (concurrency 1 + careful delay + patient timeout), matching the
        .gov/.mil path in _ensure. Idempotent. Safe mid-flight: slot() binds the acquired semaphore
        OBJECT at entry, so replacing self._sem[host] with a fresh Semaphore(1) lets in-flight holders
        drain the old object while new entrants throttle to 1 — no permit is corrupted (peak_inflight may
        briefly exceed 1; it decays as holders release). Only reached after a failure, so slot()→_ensure
        already built this host's state; we just overwrite _sem/_delay/_timeout."""
        host = _hkey(host)
        if errors.is_careful_host(host) or host in self.force_careful:
            return
        self.force_careful.add(host)
        self._sem[host] = asyncio.Semaphore(1)
        self._cap[host] = 1
        self._delay[host] = max(self._delay.get(host, self.min_delay),
                                _env_float("CRAWLER_CAREFUL_DELAY_S", 5.0))
        self._timeout[host] = self.base_timeout_s * _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)

    def retarget(self, host: str, target: int) -> None:
        """Grow this host's concurrency cap to *target* by releasing extra permits.

        GROWTH ONLY. Shrinking a live Semaphore would strand permits held by in-flight fetches;
        demotion is make_careful's job (it swaps in a fresh Semaphore(1)). Careful/override hosts
        keep their politeness cap — the fill loop must never widen a weak or .gov/.mil box."""
        host = _hkey(host)
        sem = self._sem.get(host)
        if sem is None:
            return
        if (errors.is_careful_host(host) or host in self.force_careful
                or self.override_for(host) is not None):
            return
        cur = self._cap.get(host, self.max_conc)
        if target <= cur:
            return
        for _ in range(target - cur):
            sem.release()
        self._cap[host] = target

    def cooldown(self, host: str, seconds: float) -> None:
        """429/503 backpressure: push this host's next-allowed time out by *seconds* (capped).
        Reuses the same _next_ok gate slot() already enforces — no new queueing machinery.
        ponytail: a long cooldown idles the ≤max_conc tabs holding that host's semaphore;
        fine at current scale — raise a circuit breaker if one host dominates a batch."""
        host = _hkey(host)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        cap = _env_float("CRAWLER_COOLDOWN_CAP_S", 300.0)
        target = loop.time() + min(max(seconds, 0.0), cap)
        self._next_ok[host] = max(self._next_ok.get(host, 0.0), target)

    @asynccontextmanager
    async def slot(self, url: str):
        host = _hkey(urlsplit(url).hostname or "")
        await self._ensure(host)
        # Bind the semaphore OBJECT (make_careful may swap self._sem[host] mid-flight; releasing a
        # different object than we acquired would corrupt the permit count). Count queue depth here
        # so _worker can refuse to park another tab on an already-deep host — this wait is unbounded
        # and sits inside the per-item watchdog, which is how 22 tabs burned 240s each on one host.
        sem = self._sem[host]
        self._queued[host] = self._queued.get(host, 0) + 1
        # BOUNDED, so a busy host costs a requeue rather than a worker. The wait above used to be
        # unbounded: an item queued behind a saturated host sat here until the 240s item watchdog
        # killed it, and the worker awaiting it was pinned for the whole 240s and could serve no
        # other host. Measured 3,464 abandonments in twelve minutes on hosts that render in under
        # nine seconds — the pages were never slow, the queue for their two permits was.
        #
        # Well under CRAWLER_ITEM_TIMEOUT_S (240s) on purpose: the point is to give the worker back
        # long before the watchdog would take it, so the deadline that fires is the recoverable one
        # (requeue, no error recorded) and not the destructive one (abandon, warning, budget spent).
        # 0 restores the old unbounded behaviour.
        wait_s = _env_float("CRAWLER_HOST_SLOT_WAIT_S", 45.0)
        try:
            if wait_s > 0:
                try:
                    await asyncio.wait_for(sem.acquire(), wait_s)
                except asyncio.TimeoutError:
                    raise HostSlotBusy(
                        f"no slot on {host} in {wait_s:.0f}s "
                        f"({self._queued.get(host, 0)} queued)") from None
            else:
                await sem.acquire()
        finally:
            self._queued[host] -= 1          # also on cancellation, or the depth leaks upward
        try:
            self._inflight[host] += 1
            self.peak_inflight[host] = max(self.peak_inflight[host], self._inflight[host])
            loop = asyncio.get_running_loop()
            wait = self._next_ok[host] - loop.time()
            if wait > 0:
                # Heartbeat the engine's idle-timer while asleep: a long 429/503 cooldown (up to
                # CRAWLER_COOLDOWN_CAP_S) parks every tab here with no fetch completing, which would
                # otherwise read as "idle" and kill the batch. Cooling is progress, not a wedge.
                hb = _env_float("CRAWLER_SLOT_HEARTBEAT_S", 30.0)
                end = loop.time() + wait
                self._waiting += 1
                try:
                    while (rem := end - loop.time()) > 0:
                        await asyncio.sleep(min(rem, hb))
                        if self._progress:
                            self._progress()
                finally:
                    self._waiting -= 1
            self._next_ok[host] = loop.time() + self._delay[host]
            yield
        finally:
            self._inflight[host] -= 1
            sem.release()

    def queue_depth(self, host: str) -> int:
        """Tabs currently blocked waiting for this host's permit (not counting the holder)."""
        host = _hkey(host)
        return self._queued.get(host, 0)


# ── async render (1:1 port of Fetcher._render_fetch core path) ───────────────

# Giant-binary extensions aborted before bytes flow — the render path can't cap response
# size, so we refuse the classic huge-download classes at the network layer instead.
_BLOCK_EXT = "**/*.{zip,exe,dmg,iso,msi,mp4,mkv,avi,mov,wmv,flv,gz,tgz,7z,rar,bin,pkg,deb,rpm}"


# URLs that serve a file rather than a page. Matched on the path only, so a query string like
# ?download=1 on a real HTML page does not misfire.
_DOWNLOAD_RE = re.compile(
    r"(\.(pdf|docx?|xlsx?|pptx?|csv|zip|rar|7z|gz|tgz|odt|ods|rtf)$"
    r"|/download/?$|/downloads?/[^/]+\.[a-z0-9]{2,5}$)", re.I)


def _looks_like_download(url: str) -> bool:
    try:
        return bool(_DOWNLOAD_RE.search(urlsplit(url).path))
    except Exception:
        return False


async def _abort_route(route) -> None:
    try:
        await route.abort()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


# Same giant-binary classes as _BLOCK_EXT, but as a URL regex so a single catch-all route handler
# ("**/*") can check them itself (Playwright's most-recently-added route runs first, and continue_
# is terminal — so one unified handler is cleaner than chaining two overlapping globs).
_BLOCK_EXT_RE = re.compile(
    r"\.(zip|exe|dmg|iso|msi|mp4|mkv|avi|mov|wmv|flv|gz|tgz|7z|rar|bin|pkg|deb|rpm)(?:[?#]|$)", re.I)
# Resource types a text/link/PDF crawl doesn't need — aborting them stops each render from waiting on
# megabytes of pixels/fonts it discards (the single biggest render-latency win). Kept: document,
# script, xhr/fetch, stylesheet (links + JS-injected content need those). enrich_assets downloads
# images/PDFs over httpx from the PARSED HTML, so blocking browser pixel loads does NOT reduce capture.
_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}


# Dispatch mouseover on nav candidates so hover-only dropdown submenus (whose <a href> aren't
# in the DOM until a mouseover fires) become visible for extract_links. JS events, not a real
# cursor — bounded to the first 24 nav-ish elements.
# Promote every lazy <img> to its real URL. Covers the common attribute conventions (data-src is
# near-universal; data-original is lazysizes/jQuery.lazyload; data-lazy-src is Slick/WP Rocket) and
# flips loading="lazy" to eager so anything still below the fold fetches too. Returns the count so
# the caller could log it. Deliberately does NOT touch <img> elements that already have a real src.
_LAZY_RESOLVE_JS = r"""
(() => {
  let n = 0;
  const lazy = [];
  const real = i => i.getAttribute('data-src') || i.getAttribute('data-original')
                 || i.getAttribute('data-lazy-src') || i.getAttribute('data-echo');
  document.querySelectorAll('img').forEach(i => {
    try {
      const s = real(i);
      // A placeholder is an empty src, a data: URI, or a spacer gif — anything else is the photo.
      const placeholder = !i.src || i.src.startsWith('data:') || /(^|\/)(blank|spacer|placeholder)\./i.test(i.src);
      // `i.src` is the PROPERTY, so it is always absolute; `data-src` is usually relative. The old
      // `i.src !== s` was therefore true for practically every data-src image, and re-assigning src
      // on an image that is already showing the right photo forces a needless re-fetch and drops it
      // back to undecoded until it lands. Measured on torquemag.io, whose 55 images are all decoded
      // before this runs: decoded count fell to 39-46. Resolve against the document base first, and
      // never touch an image that has already decoded its pixels.
      const decoded = i.complete && i.naturalWidth > 0;
      const abs = u => { try { return new URL(u, document.baseURI).href; } catch (e) { return u; } };
      if (s && (placeholder || (!decoded && abs(s) !== i.src))) { i.src = abs(s); n++; }
      const ss = i.getAttribute('data-srcset') || i.getAttribute('data-lazy-srcset');
      if (ss && (placeholder || !decoded)) i.srcset = ss;
      // Native lazy loading, which is now the common case: no data-src at all, just the attribute.
      // Measured on theguardian.com — 0 data-src images, yet decoded images went 20 -> 117 from
      // this line alone. Counted, or the returned number would report 0 work done.
      if (i.loading === 'lazy') {
        // Remember the URL BEFORE mutating. Flipping to eager is what recovers the image on a
        // site that merely defers the fetch (theguardian.com: 19 -> 103 decoded from this line).
        // But a site whose OWN observer completes a partial URL on intersection instead gets a
        // forced request for the stub: measured on bbc.com/news, 15 <img> carry extension-less
        // ichef URLs that 403, BBC's error handler then DELETES those elements, and the capture
        // goes 36 <img> -> 21 with an image URL lost, for zero decodes gained. The two cannot be
        // told apart before trying — so try, then put back whatever the page removed.
        const u = i.getAttribute('src');
        if (u && !u.startsWith('data:')) lazy.push([u, i.getAttribute('srcset') || '']);
        i.loading = 'eager'; n++;
      }
    } catch (e) {}
  });
  // <source> inside <picture> carries the responsive variants; miss it and the <img> falls back.
  document.querySelectorAll('picture source[data-srcset]').forEach(s => {
    try { s.srcset = s.getAttribute('data-srcset'); n++; } catch (e) {}
  });
  window.__mallory_lazy = lazy;
  return n;
})()
"""

# The restore half. Runs AFTER the settle, because the deletions are asynchronous — they happen
# when the forced request fails, not when the attribute changes. Re-inserts into a hidden holder,
# since the HTML snapshot is what asset capture reads. Cannot inflate any image metric: a URL that
# 403s never satisfies `complete && naturalWidth > 0`.
_LAZY_RESTORE_JS = r"""
(() => { try {
  const want = window.__mallory_lazy || [];
  if (!want.length) return 0;
  const have = new Set([...document.images].map(i => i.getAttribute('src')).filter(Boolean));
  const lost = want.filter(u => !have.has(u[0]));
  if (!lost.length) return 0;
  let holder = document.getElementById('__mallory_lazy_recovered');
  if (!holder) {
    holder = document.createElement('div');
    holder.id = '__mallory_lazy_recovered';
    holder.style.display = 'none';
    document.body.appendChild(holder);
  }
  for (const [src, srcset] of lost) {
    const i = document.createElement('img');
    i.setAttribute('src', src);
    if (srcset) i.setAttribute('srcset', srcset);
    holder.appendChild(i);
  }
  return lost.length;
} catch (e) { return -1; } })()
"""

# Scroll whatever is actually scrollable, and report ITS height so the caller's growth check reads
# the container that is growing. `document.body.scrollHeight` is both the wrong target and the
# wrong sentinel on an app-shell layout, where body and html are pinned to the viewport and a div
# does the scrolling: measured on mixcloud.com, body.scrollHeight == innerHeight == 720 while the
# real scroller (main, overflow-y:scroll) was 115px taller than its box — so the old loop scrolled
# nothing and then exited on step 1 because 720 == 720. documentElement covers the ordinary case
# where body is height:100% but the document is not.
_SCROLL_DEEPEST_JS = r"""
(() => { try {
  const H = Math.max(document.body ? document.body.scrollHeight : 0,
                     document.documentElement.scrollHeight);
  window.scrollTo(0, H);
  let deepest = H;
  for (const e of document.querySelectorAll('*')) {
    // Cheap test first: getComputedStyle on every node is the expensive part, so only ask for it
    // once the geometry already says this element overflows its own box.
    if (e.scrollHeight <= e.clientHeight + 50 || e.clientHeight < 100) continue;
    const o = getComputedStyle(e).overflowY;
    if (o !== 'auto' && o !== 'scroll' && o !== 'overlay') continue;
    e.scrollTop = e.scrollHeight;
    if (e.scrollHeight > deepest) deepest = e.scrollHeight;
  }
  return deepest;
} catch (e) { return -1; } })()
"""

_AUTO_HOVER_JS = """
() => {
  const sel = 'nav a,header a,[aria-haspopup],[class*="menu"] a,[class*="nav"] li,.dropdown,[role="menu"],[role="menuitem"]';
  const els = Array.from(document.querySelectorAll(sel)).slice(0, 24);
  const fire = (t, e) => { try { e.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window})); } catch(_){} };
  for (const e of els) { fire('pointerover', e); fire('mouseover', e); fire('mouseenter', e); }
  return els.length;
}
"""

# Find a visible "load more / show more" control and tag it for clicking.
# Matched by TEXT across every element, not by tag. The old tag list
# ('button,a,[role="button"],[onclick]') assumed the control is a button, a link, or carries an
# inline handler - and modern feeds ship neither. Measured on this corpus: newatlas.com uses
# <span class="Link">Load More</span> with the handler bound by addEventListener (so no onclick
# attribute and no role), and vsco.co ships a <grain-button> custom element. Both returned ZERO
# candidates from the old selector, and newatlas then scored "nothing moved" on a page that gives
# up 11,455 more characters and 35 more decoded images the moment the control is clicked.
# The deepest match wins, so the tag lands on the control itself and not on some ancestor section
# that merely contains it; the <= 40 char guard keeps a whole page section from ever matching.
_LOADMORE_JS = """
() => {
  const rx = /load more|show more|view more|see more|load all/i;
  let hit = null, bestN = 1e9;
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (t.length > 40 || !rx.test(t) || e.offsetParent === null) continue;
    const n = e.querySelectorAll('*').length;
    if (n < bestN) { bestN = n; hit = e; }
  }
  if (hit) { hit.setAttribute('data-__lm','1'); return true; }
  return false;
}
"""

# In-page content switchers: year filters, tab panels, category pills. These load real content over
# AJAX and change no URL, so link extraction cannot reach them and _LOADMORE_JS's wording test does
# not match them either — a control labelled "2015" says nothing about loading anything. Measured on
# scrapethissite.com/pages/ajax-javascript/, where <a class="year-link" id="2015"> (no href at all)
# takes the DOM from 101 nodes to 182 and paints a 17-row table that is otherwise never fetched.
#
# The safety property is structural, not a wordlist: an <a> with NO href, or href="#"/javascript:,
# cannot navigate anywhere. It can only run script on this page. So the blast radius is this page,
# and the deny-list below only has to cover controls that mutate STATE rather than view — which on
# a crawler's unauthenticated session is a short list.
_INPAGE_CLICK_JS = r"""
(idx) => {
  const bad = new RegExp("log\\s*out|sign\\s*out|log\\s*off|delete|remove|unsubscribe|cancel"
    + "|checkout|buy now|add to (cart|bag)|submit|confirm|\\bpay\\b|book now|apply now", "i");
  const els = Array.from(document.querySelectorAll('a')).filter(a => {
    if (a.offsetParent === null) return false;                 // not visible
    // The "it cannot navigate anywhere" argument above holds for a plain document and fails for a
    // single-page app, where a hrefless <a> in the site nav is a router link. Measured on
    // juejin.cn: one such click took the capture from 1,339 nodes / 2,030px / 3,551 chars down to
    // 362 / 720 / 336, throwing away everything the scroll had just gained. An in-page content
    // switcher lives in the content area; site navigation does not - so exclude the chrome.
    if (a.closest('nav,header,[role="navigation"],[role="banner"]')) return false;
    const h = (a.getAttribute('href') || '').trim().toLowerCase();
    if (h && h !== '#' && !h.startsWith('javascript:')) return false;   // real navigation, not ours
    const label = ((a.textContent || '') + ' ' + (a.id || '') + ' ' + (a.className || '')).trim();
    if (!label || label.length > 60) return false;
    return !bad.test(label);
  });
  if (idx >= els.length) return -1;
  try { els[idx].click(); } catch (e) { return -2; }
  return els.length;
}
"""


async def _auto_interact(tab) -> None:
    """Reveal links that only appear after scroll (lazy-load), hover (dropdown menus), or a
    'load more' click, so parse.extract_links sees them. Default-on (CRAWLER_AUTO_INTERACT!=0),
    bounded, every step swallowed (never fatal). Runs on RENDERED pages, just before content()."""
    try:                            # 1) bounded infinite scroll → trigger lazy-load
        # 8 steps, not 3. An infinite feed appends one screenful per scroll, so 3 steps captured
        # roughly the first three batches and the page was recorded as if that were all of it.
        # Cost is bounded anyway: the loop exits the moment scrollHeight stops growing, which is
        # the common case (a normal page exits after one step).
        steps = _env_int("CRAWLER_AUTOSCROLL_STEPS", 8)
        pause = _env_int("CRAWLER_AUTOSCROLL_PAUSE_MS", 600)
        # _SCROLL_DEEPEST_JS both scrolls and returns the height it scrolled, so the growth check
        # reads the same element the scroll moved. The old pair scrolled the WINDOW and watched
        # document.body — on an app-shell layout neither is the thing that grows.
        last = -1
        for _ in range(steps):
            new = await tab.evaluate(_SCROLL_DEEPEST_JS)
            await tab.wait_for_timeout(pause)
            if not isinstance(new, (int, float)) or new == last:
                break
            last = new
    except Exception:
        pass
    try:                            # 1b) force-resolve lazy IMAGES
        # Scrolling alone is not enough for images. A lazy <img> keeps its real URL in data-src and
        # only swaps it in when its IntersectionObserver fires — which needs the element in the
        # viewport, at the right moment, with the observer actually registered. Anything that misses
        # (a virtualised list, an observer bound after our scroll, an element still below the fold)
        # leaves a placeholder, and asset capture then harvests 1x1 spacers instead of photographs.
        # Reading the attribute directly is exact and costs one evaluate.
        await tab.evaluate(_LAZY_RESOLVE_JS)
        await tab.wait_for_timeout(_env_int("CRAWLER_LAZY_SETTLE_MS", 400))
        # AFTER the settle: the deletions above are asynchronous — they happen when the forced
        # request fails, not when the attribute changes — so this has to run once they have.
        await tab.evaluate(_LAZY_RESTORE_JS)
    except Exception:
        pass
    try:                            # 2) hover nav → reveal dropdown submenus
        await tab.evaluate(_AUTO_HOVER_JS)
        await tab.wait_for_timeout(250)
    except Exception:
        pass
    try:                            # 3) click "load more"/"show more" a few times
        for _ in range(_env_int("CRAWLER_LOADMORE_CLICKS", 5)):
            if not await tab.evaluate(_LOADMORE_JS):
                break
            try:
                await tab.locator('[data-__lm="1"]').first.click(timeout=3000, no_wait_after=True)
            except Exception:
                break
            await tab.wait_for_timeout(600)
            await tab.evaluate(
                "document.querySelectorAll('[data-__lm]').forEach(e=>e.removeAttribute('data-__lm'))")
    except Exception:
        pass
    try:                            # 3b) click in-page content switchers (year/tab/category filters)
        # Distinct from step 3: _LOADMORE_JS matches on WORDING, and a control labelled "2015" says
        # nothing about loading anything. These carry no href, so they cannot navigate — the worst
        # case is a wasted click on this page. Stops as soon as a click stops adding DOM, so a page
        # of inert anchors costs one click, not the whole budget.
        n_before = await tab.evaluate("document.querySelectorAll('*').length")
        for i in range(_env_int("CRAWLER_INPAGE_CLICKS", 6)):
            total = await tab.evaluate(_INPAGE_CLICK_JS, i)
            if not isinstance(total, (int, float)) or total < 0 or i >= total:
                break
            await tab.wait_for_timeout(_env_int("CRAWLER_INPAGE_SETTLE_MS", 700))
            n_after = await tab.evaluate("document.querySelectorAll('*').length")
            if n_after <= n_before and i >= 1:
                break                       # two clicks in, nothing has arrived — this is not a filter
            n_before = max(n_before, n_after)
    except Exception:
        pass


async def _wait_dom_settle(tab, cap_ms: int | None = None) -> bool:
    """Block until the DOM stops growing. Returns True if it was STILL growing at the cap.

    A JS app finishes when its DOM stops changing, which no fixed sleep can know: the same 2s that
    is wasted on a static page is nowhere near enough for a framework that hydrates and then fetches.
    So sample the document size and stop once it repeats — fast pages exit on the second sample
    (~300ms, quicker than the fixed budget it replaces), slow ones get the time they actually need.

    The cap exists because some pages never settle at all (carousels, tickers, live counters, ad
    rotators). Hitting it is reported rather than swallowed: the caller marks the page js_incomplete
    so a half-built capture is visible instead of being stored as if it were finished.
    """
    cap_ms = _env_int("CRAWLER_DOM_SETTLE_MAX_MS", 12000) if cap_ms is None else cap_ms
    step_ms = _env_int("CRAWLER_DOM_SETTLE_STEP_MS", 300)
    stable_needed = _env_int("CRAWLER_DOM_SETTLE_STABLE", 2)
    if cap_ms <= 0:
        return False
    # WALL CLOCK, not accumulated sleep. `waited += step_ms` counted only the naps and ignored the
    # probe between them, so the cap bounded the wrong quantity: measured on news pages, a cap set
    # to 4s produced 15.5s of real time, and the shipped 12s cap routinely cost 20-25s. Every
    # rendered page in the corpus paid this.
    # BOTH bounds, whichever comes first. Wall clock is the one that fixes production. The step
    # budget keeps the loop terminating on a virtual clock too — the test double advances time by
    # accumulating a counter rather than sleeping, which is the right way to test a polling loop and
    # would otherwise let it spin until its script ran out.
    deadline = time.monotonic() + cap_ms / 1000.0
    steps_left = max(1, cap_ms // max(1, step_ms))
    # ...and a probe that does NOT serialise the document. outerHTML.length builds a string of the
    # entire DOM — every 300ms, on pages whose whole problem is being large — purely to measure its
    # length. A live element count answers the only question here ("is it still GROWING?") without
    # materialising anything, which is what makes the cheaper cap achievable rather than just
    # shorter. It misses growth that adds no elements; that is the trade, and lazy-loading — the
    # thing this exists to wait for — always adds elements.
    last, stable = -1, 0
    while time.monotonic() < deadline and steps_left > 0:
        try:
            size = await tab.evaluate("document.getElementsByTagName('*').length")
        except Exception:      # navigated away / tab gone — let the caller's handler classify it
            return False
        if size == last:
            stable += 1
            if stable >= stable_needed:
                return False   # settled
        else:
            stable, last = 0, size
        steps_left -= 1
        if steps_left <= 0 or time.monotonic() >= deadline:
            break
        await tab.wait_for_timeout(step_ms)
    return True                # still moving when the cap hit


async def _render_page(tab, url: str, timeout_ms: int, capture: list[str],
                       interaction=None, discover: bool = True) -> FetchResult:
    """Render one URL on a persistent tab; snapshot to a plain FetchResult on the loop.
    Render-everything mode: always renders, no httpx/conditional (dedup is by content_hash).
    Before snapshotting, `_auto_interact` scrolls/hovers/load-mores to surface JS-revealed links —
    but ONLY when `discover` is True (this page's children will be enqueued). A leaf page at max
    depth has no children to reveal, so we skip the ~4-5s of scroll/hover there.
    If `interaction` is set (form-search/pagination/scroll), run it after load — that's what
    lets the pool search a gov tender portal."""
    networkidle_ms = _env_int("CRAWLER_NETWORKIDLE_MS", 1500)
    settle_ms = _env_int("CRAWLER_RENDER_SETTLE_MS", 500)

    nav_url = url
    try:
        resp = await tab.goto(nav_url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:
        www = _with_www(url)
        if not _is_tls_or_dns_error(exc) or www == url:
            return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                               error=f"nav:{exc}",
                               reason=errors.classify_failure(None, f"nav:{exc}"))
        try:
            await tab.wait_for_timeout(500)
            resp = await tab.goto(www, timeout=timeout_ms, wait_until="domcontentloaded")
            nav_url = www
        except Exception as exc2:
            return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                               error=f"nav:{exc2}",
                               reason=errors.classify_failure(None, f"nav:{exc2}"))

    try:  # networkidle exits early the instant the network idles; cap bounds non-idling sites
        await tab.wait_for_load_state("networkidle", timeout=networkidle_ms)
    except Exception:
        pass
    try:
        await tab.wait_for_timeout(settle_ms)
        # Wait for the DOM to stop GROWING, not for a fixed number of milliseconds. Measured on
        # this corpus at the old fixed budget (networkidle 1500ms + 500ms settle): renault-trucks
        # was snapshotted at 25% of its final DOM (44K of 174K), zf.com at 70%. Both were stored as
        # complete pages, because a networkidle timeout is swallowed above and nothing downstream
        # can tell a finished page from one caught mid-hydration.
        # Cheap for static sites — they are stable on the first two samples and exit sooner than
        # the old fixed sleep — and only slow where slowness is the whole point.
        grew = await _wait_dom_settle(tab)
        if discover and os.environ.get("CRAWLER_AUTO_INTERACT", "1") != "0":
            await _auto_interact(tab)      # scroll/hover/load-more to reveal hidden links
            # Short cap on the SECOND wait: the page already settled once, so this is only catching
            # lazy-loads that the scroll triggered, which arrive fast. At the full 12s cap
            # rohde-schwarz took 38.5s/page (12 settle + interact + 12 settle) — unusable on a
            # 2,455-page host.
            await _wait_dom_settle(tab, cap_ms=_env_int("CRAWLER_DOM_SETTLE_POST_MS", 3000))
        html = await tab.content()
    except Exception as exc:
        return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                           error=f"content:{exc}",
                           reason=errors.classify_failure(None, f"content:{exc}"))

    # Image-text captcha gate (whole page withheld behind a distorted-text image). Do NOT solve it
    # here in headless Chromium: sophisticated sites accept the typed answer but then soft-block
    # Chromium to an error page anyway (measured on joinindianarmy.nic.in → error.aspx), so the
    # "solve" stores junk. Flag it instead — _process escalates the gate to the C3 stealth browser,
    # which OCR-solves it (crawler/camofox_client) AND is let through to the real page. Cheap html
    # pre-filter first, then confirm with the classifier. Off with CRAWLER_CAPTCHA_OCR=0.
    if os.environ.get("CRAWLER_CAPTCHA_OCR", "1") == "1" and captcha.looks_image_text(html):
        try:
            ev = await tab.evaluate(captcha.EVIDENCE_JS)
        except Exception:
            ev = None
        if captcha.classify(ev if isinstance(ev, dict) else None).kind == "image_text":
            try:
                final_url = tab.url
            except Exception:
                final_url = nav_url
            return FetchResult(url=url, final_url=final_url, status=(resp.status if resp else None),
                               content_type="text/html", kind="html", text_html=html, tier=1,
                               fetched_at=_now_iso(), reason=errors.NEEDS_CAPTCHA_SOLVER)

    inner_text = None
    extra_links: list | None = None
    extra_pdf_links: list | None = None
    if interaction_async.has_any(interaction):
        try:                       # form-search / pagination / scroll — never fatal
            html, inner_text, extra_links, extra_pdf_links = \
                await interaction_async.run_interactions(tab, interaction)
        except Exception:
            pass

    # Post-hoc DOM cap — Playwright can't cap response size, so a giant page is only
    # measurable after content(). ponytail: memory is already spent here; this bounds
    # a runaway DOM (Chromium itself bounds the tab) rather than the wire response.
    if len(html) > _env_int("CRAWLER_MAX_HTML_BYTES", 20_971_520):
        return FetchResult(url=url, final_url=nav_url, status=(resp.status if resp else None),
                           fetched_at=_now_iso(), error="too_large", reason=errors.TOO_LARGE)

    status = resp.status if resp else None
    reason = errors.http_reason(status) if status and status >= 400 else None
    retry_after = None
    if status in (429, 503) and resp:
        try:
            retry_after = errors.parse_retry_after(await resp.header_value("retry-after"))
        except Exception:
            retry_after = None
    shot = None
    if "screenshot" in capture:
        try:
            shot = await tab.screenshot(full_page=True)
        except Exception:
            shot = None
    try:
        final_url = tab.url
    except Exception:
        final_url = nav_url

    # Capture HTTP validators from the render response too (Playwright exposes them) so an
    # incremental recrawl can send If-None-Match/If-Modified-Since and skip unchanged pages —
    # otherwise a rendered site (most of them) would store no validators and never get a 304.
    etag = last_modified = None
    if resp:
        try:
            h = await resp.all_headers()
            etag = h.get("etag")
            last_modified = h.get("last-modified")
        except Exception:
            pass

    # Harvest the render's cookies so a protected asset download (Akamai) can replay the
    # page session that just passed the WAF. Best-effort; plain data crosses to to_thread.
    cookies = None
    try:
        cookies = await tab.context.cookies()
    except Exception:
        cookies = None

    return FetchResult(url=url, final_url=final_url, status=status, content_type="text/html",
                       kind="html", text_html=html, inner_text=inner_text, screenshot_png=shot,
                       tier=1, fetched_at=_now_iso(), reason=reason, retry_after_s=retry_after,
                       cookies=cookies, etag=etag, last_modified=last_modified,
                       js_incomplete=grew,
                       extra_links=extra_links or None, extra_pdf_links=extra_pdf_links or None)


# ── the engine ───────────────────────────────────────────────────────────────

class AsyncEngine:
    def __init__(self, W: int, T: int, host: HostLimiter, seed: Seed, kp,
                 robots: RobotsCache | None = None) -> None:
        self.W = W
        self.T = T
        self.host = host
        self.seed = seed
        self.kp = kp                    # global keyword-corpus trie (keep-gate)
        self.robots = robots
        self._pw = None
        self.browsers: list[tuple] = []     # (browser, context)
        self.tabs: list = []
        self.history: CrawlHistory | None = None
        self.frontier: asyncio.Queue | None = None
        self._last_progress = 0.0
        # Circuit breaker: consecutive HARD failures (DNS/refused/SSL) per host; a host
        # crossing the threshold goes into dead_hosts and its remaining URLs are skipped.
        self.host_fails: dict[str, int] = {}
        # Consecutive 401/403 per host — a site REFUSING us rather than failing. Separate from
        # host_fails on purpose: mixing "broken" with "says no" means neither streak ever trips.
        self.host_forbidden: dict[str, int] = {}
        # Consecutive render_crash per host — the browser dying on this site's pages, which is
        # neither a network failure nor an HTTP answer and so reaches no other breaker.
        self.host_crashes: dict[str, int] = {}
        self.host_weak: dict[str, int] = {}   # conn_refused/timeout tally → auto-careful promotion
        self._render_hosts: dict[str, int] = {}  # per-host render permits held/queued (see _render_permit)
        # RENDER-LANE ADMISSION. _lane_hosts counts what each host currently has IN render_q;
        # _host_hold parks the surplus OUTSIDE it. A host with 70,000 queued URLs and three
        # render permits does not need 70,000 entries cycling through the lane to use them.
        # See _lane_put for the measurement that produced this.
        self._lane_hosts: dict[str, int] = {}
        self._host_hold: dict[str, list] = {}
        self._held_items = 0             # total parked; _drain must not exit while > 0
        self.dead_hosts: set[str] = set()
        # Cooling hosts revive for a fresh live attempt at self.host_cooldown[host] (loop time);
        # while cooling, their queued URLs are recovered via C2/C3 rather than dropped.
        self.host_cooldown: dict[str, float] = {}
        # When each host's render slots were FIRST seen full, so the deflect budget can be judged
        # per host rather than per item. Cleared the moment that host's queue has room again.
        self.host_busy_since: dict[str, float] = {}
        # Loop time of the last page this host actually served. The deflect budget uses it to tell a
        # host that is merely SLOW (permanently full slots, but delivering) from one that is wedged.
        self.host_last_ok: dict[str, float] = {}
        # host_busy give-ups per host. With host_last_ok this is the third circuit breaker: slots
        # never drain AND nothing has ever been served → the host is unreachable, not busy.
        self.host_stalled: dict[str, int] = {}
        # Asset downloads (images/PDFs in enrich_assets) get a DEDICATED thread pool, isolated from
        # asyncio.to_thread's shared default pool. A slow/hanging asset host (e.g. l3harris) would
        # otherwise fill the shared pool and starve page fetches/builds → the WHOLE crawl stalls with
        # every worker stuck in fetch_assets. Isolated here, backed-up asset downloads never block
        # page crawling (pages keep fetching; only asset capture lags).
        from concurrent.futures import ThreadPoolExecutor
        self._asset_pool = ThreadPoolExecutor(
            max_workers=_env_int("CRAWLER_ASSET_POOL", 24), thread_name_prefix="asset")
        # Auto render-mode: hosts escalated to always-render (httpx kept coming up empty), and
        # the per-host [rendered, total] tally over the first few pages that drives escalation.
        self.host_render_always: set[str] = set()
        self.host_render_stat: dict[str, list] = {}
        # Hosts a C4 host-solve has cleared. Once one page of an interactive-Cloudflare host is
        # solved, every other page on it is walled the same way — so route them STRAIGHT to the
        # warm host solver (~5s each) instead of repeating the httpx -> render -> C3 (57s, certain
        # to fail) -> C4 ladder per page. Mirrors host_render_always / the proactive-C3 WAF set.
        self.host_needs_c4: set[str] = set()
        # Requeued-transient count — guards _drain so a delayed retry isn't lost.
        # Mutated only on the loop thread.
        self._pending_retries = 0
        self._inflight_items = 0          # items dequeued but not yet task_done
        self._httpx_since_render = 0      # pulls served from the cheap lane since the last render one
        # CPU-feedback adaptive concurrency: a global gate on concurrent RENDERS whose ceiling the
        # _drain sampler ramps toward CRAWLER_CPU_TARGET. Ceiling can't exceed the tab count (W*T).
        # Disabled (or psutil-missing) → limit pinned at max, so the gate never blocks.
        self.adaptive_on = psutil is not None and os.environ.get("CRAWLER_ADAPTIVE_CONCURRENCY", "1") != "0"
        _tabs = self.W * self.T
        _amin = _env_int("CRAWLER_ADAPTIVE_MIN", 4)
        self.adaptive = AdaptiveGate(
            initial=(8 if self.adaptive_on else _tabs), min_limit=_amin,
            # Cap render admission below total tabs: renders that are I/O-blocked (a WAF-blocked page
            # loading its 403 wall, or a slow C3 escalation) show low CPU, so the CPU-fed controller
            # would otherwise ramp admission to every tab and saturate the pool.
            max_limit=min(_tabs, _env_int("CRAWLER_ADAPTIVE_MAX", 64)))
        # Bound concurrent C3/CamoFox renders. A WAF-blocked section (mass 403 → C3 escalation) must not
        # flood the single CamoFox service faster than it drains, or renders pile up, throughput
        # collapses, and the batch idle-terminates mid-frontier.
        self._camofox_sem = asyncio.Semaphore(_env_int("CRAWLER_CAMOFOX_CONCURRENCY", 6))
        # Deferred C3 lane (CRAWLER_DEFER_C3=1): blocked C1 pages are pushed here instead of parking a
        # tab-worker on the ~90s stealth render; a tab-less _c3_worker pool drains it at the CamoFox
        # cap. Created on the loop in run(). Counters mutate only on the loop thread (like _inflight_items).
        self.c3_queue: asyncio.Queue | None = None
        self._inflight_c3 = 0            # c3 items dequeued, not yet done — blocks _drain exit
        self._netpath_tagged = 0         # COUNT only (never a URL list): needs-network-path backlog size
        self._run_started_iso = ""       # set in run(); scopes the sqlite netpath reconstruction
        # Block image/media/font resource loads during renders (env-guarded). Flipped OFF in run() if
        # any job wants a screenshot (needs the pixels). Read per-request by _route_request.
        self._block_assets = os.environ.get("CRAWLER_BLOCK_ASSETS", "1") != "0"
        # Finalizer pool: the slow I/O tail (asset enrich + ingest POST) runs here so a worker's tab
        # returns to the frontier the instant a page is parsed, instead of blocking on PDF downloads +
        # the ingest round-trip. Created on the loop in run() (queue + N drainers).
        self._finalize_q: asyncio.Queue | None = None
        # Tab pool (CRAWLER_TAB_POOL): shared tabs borrowed for renders; workers pull the frontier
        # tab-less. n_workers is the frontier-consumer count (== tab count in legacy mode).
        self._tab_pool: asyncio.Queue | None = None
        self.n_workers = 0

    async def _route_request(self, route) -> None:
        """Single wire-level gate on every request: abort giant-binary downloads always; when
        asset-blocking is on AND the request host isn't careful, also abort image/media/font so a
        text+PDF render stops waiting on pixels it discards. Everything else continues. Never fatal."""
        try:
            req = route.request
            u = req.url
            if _BLOCK_EXT_RE.search(u):
                await route.abort()
                return
            # Third-party ad/analytics beacons — never a crawl target; abort for faster renders and
            # a smaller fingerprint surface. (Skipped on screenshot jobs, which use the narrow route.)
            if stealth.block_trackers_enabled() and stealth.is_tracker(urlsplit(u).hostname or ""):
                await route.abort()
                return
            if self._block_assets and req.resource_type in _BLOCK_RESOURCE_TYPES:
                host = (urlsplit(u).hostname or "").lower()
                if not (errors.is_careful_host(host) or host in self.host.force_careful):
                    await route.abort()
                    return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    async def start(self) -> None:
        # Borrow the PROCESS-WIDE pool; never fork a driver per batch. See _SHARED_POOL for the
        # measurements that made the per-batch cycle indefensible.
        browsers = await _shared_browsers(self.W)
        self._pw = _SHARED_POOL["pw"]
        self._pw_pids = _SHARED_POOL["pids"]
        try:
            await self._open_contexts(browsers)
        except BaseException:
            # A PARTIAL START MUST NOT SURVIVE ITS OWN FAILURE.
            #
            # chromium.launch is the call measured blowing its 180s timeout whenever the box is
            # loaded, and this method had no handler: the exception escaped to a caller that starts
            # the engine OUTSIDE its try/finally, so shutdown() never ran and the engine object —
            # the only reference to _pw and _pw_pids — was dropped. Driver and browsers unreachable,
            # permanently, with no later pass able to reclaim them.
            #
            # MEASURED 2026-08-19, two days after the teardown-kill fix: instance 6 held 31 node
            # drivers and 54 top-level browsers (595 chrome), fleet 2,539. The teardown fix was live
            # and had fired 19 times — it simply never sees this path. 31 drivers at W=2 should own
            # 62 browsers; the 8 missing are the ones that died mid-loop.
            #
            # shutdown() already closes tabs/contexts/browsers, stops the driver and kills whatever
            # refuses — so reuse it rather than writing a second teardown that can drift from it.
            await self.shutdown()
            raise

    async def _open_contexts(self, browsers: list) -> None:
        """One fresh context (+T tabs) per shared browser, for THIS batch only.

        Contexts are per batch on purpose - they hold cookies and session state, and carrying them
        into the next batch would bleed one site's session into another's crawl. The browsers
        underneath are shared and are never closed here; see _SHARED_POOL.
        """
        ua = self.seed.capture_defaults["user_agent"]
        proxy = _proxy_config()
        if proxy:
            log.info("proxy enabled via CRAWLER_PROXY_URL: %s", proxy["server"])
        for b in browsers:
            ctx = await b.new_context(user_agent=ua, viewport={"width": 1920, "height": 1080},
                                      locale="en-US", timezone_id="America/New_York",
                                      extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                                      ignore_https_errors=_ignore_https_errors(),
                                      proxy=proxy)
            if stealth.stealth_enabled():
                await ctx.add_init_script(stealth.STEALTH_INIT_JS)
            # Session/cookies: the browser already domain-scopes its cookie jar (host A's cookie
            # is never SENT to host B), and a gov search flow (fill→submit→paginate) runs on ONE
            # tab in this ONE context in a single _render_page call — so its session persists
            # exactly where a portal needs it. ponytail: full per-host BrowserContext isolation
            # (for a session-required MULTI-page BFS spread across the W browsers) is a
            # pool rearchitecture for a rare case — add host→browser affinity if it ever bites.
            try:
                if self._block_assets:
                    # Catch-all so the handler can abort image/media/font per-request (giant binaries
                    # always abort inside it). If _block_assets is later flipped off (a screenshot
                    # job), the handler degrades to giant-binary-only + continue.
                    await ctx.route("**/*", self._route_request)
                else:
                    await ctx.route(_BLOCK_EXT, _abort_route)   # cheap narrow route: giant downloads only
            except Exception:
                pass
            self.browsers.append((b, ctx))
            for _ in range(self.T):
                self.tabs.append(await ctx.new_page())
        log.info("engine started: %d browsers x %d tabs = %d workers",
                 self.W, self.T, len(self.tabs))

    async def _ensure_auth(self, ctxs: list["JobCtx"]) -> None:
        """Establish a login for every job that asked for one, and hand the cookies to the pool.

        Best-effort by design: a portal we cannot join is crawled anonymously (the attempt is in
        the issue log — see siteauth._finish), because a failed signup must not take the whole
        batch down with it. httpx picks the session up on its own via siteauth.cookie_header;
        only the browser contexts have to be told.
        """
        from . import siteauth
        want: dict[str, str] = {}                     # host -> a seed URL on it
        for c in ctxs:
            if not getattr(c.job, "auth", None):
                continue
            for s in c.job.seed_urls:
                h = (urlsplit(s).hostname or "").lower()
                if h:
                    want.setdefault(h, s)
        if not want:
            return
        for host, url in want.items():
            try:
                ok = await siteauth.ensure_session(url)
            except Exception as exc:
                log.warning("auth: %s failed hard (%s) — crawling anonymously", host, exc)
                continue
            if not ok:
                continue
            cookies = siteauth.playwright_cookies(url)
            if not cookies:
                continue
            # Every context, not one: a page for this host can land on any browser in the pool.
            # Cookies are domain-scoped by the browser, so adding a portal's cookies to a context
            # that later fetches some other host cannot leak them there.
            for _b, bctx in self.browsers:
                try:
                    await bctx.add_cookies(cookies)
                except Exception as exc:
                    log.debug("auth: add_cookies failed for %s: %s", host, exc)
            log.info("auth: %s crawling as a logged-in user (%d cookies)", host, len(cookies))

    async def run(self, jobs: list[Job], forward: bool) -> list[JobCtx]:
        self.history = CrawlHistory()                       # bound to this (engine) thread
        # Enlarge the default thread pool. httpx fetch + build_document + ingest.send all run via
        # asyncio.to_thread — the default ~28-thread pool (min(32, cpu+4)) caps a whole-site httpx
        # crawl regardless of tab count. A big IO pool lets httpx concurrency scale to the host/tab
        # limits (the lever for keyword=false whole-site sweeps). Renders stay governed by the gate.
        from concurrent.futures import ThreadPoolExecutor
        self._io_pool = ThreadPoolExecutor(
            max_workers=_env_int("CRAWLER_IO_THREADS", 256), thread_name_prefix="io")
        asyncio.get_running_loop().set_default_executor(self._io_pool)
        try:                                                # seed per-host render learning from prior runs
            self.host_render_always |= self.history.get_render_hosts()
        except Exception:
            pass
        try:                                                # seed weak-under-load careful hosts (prior runs)
            self.host.force_careful |= self.history.get_careful_hosts()
        except Exception:
            pass
        # Asset-blocking needs the browser to load images for a screenshot — if ANY job in the batch
        # captures screenshots, disable blocking for the whole (shared-context) pool.
        if any("screenshot" in getattr(j, "capture", []) for j in jobs):
            self._block_assets = False
        # BFS (FIFO) by default; CRAWLER_FRONTIER=dfs → LIFO so articles surface before the
        # seed's category mega-menu is fully drained. Same .get/.put_nowait/.task_done API.
        self.frontier = asyncio.LifoQueue() if _frontier_dfs() else asyncio.Queue()
        # SECOND LANE, deliberately separate from the frontier: pages that need a browser.
        #
        # A render costs 10-90s and a tab; an httpx fetch costs a connection and ~0.3s. Sharing one
        # FIFO makes the cheap work queue behind the expensive work, so a batch containing a few
        # render-heavy sites drags every other site down with it. Worse, a worker inside a render is
        # unavailable for httpx, so a run of render items can occupy every worker at once and httpx
        # throughput goes to zero while the CPU sits idle.
        #
        # Two rules fix that, both enforced in _next_item:
        #   1. the httpx lane is always drained FIRST — render work is genuinely last;
        #   2. at most CRAWLER_RENDER_LANE_FRAC of the workers may be inside the render lane at
        #      once, so a reserved share of the pool is permanently available for httpx.
        self.render_q: asyncio.Queue = asyncio.Queue()
        self._render_lane_used = 0
        self._render_lane_cap = 0        # set in run() once the worker count is known
        # Durable-frontier write buffers (job_id -> rows). Flushed in batches; see _flush_frontier.
        self._frontier_add_buf: dict[str, list] = {}
        self._frontier_done_buf: dict[str, list] = {}
        self._frontier_relax_buf: dict[str, list] = {}
        if _frontier_dfs():
            log.info("frontier: DFS (LIFO)")
        ctxs = [JobCtx(j, self.seed, self.kp, forward) for j in jobs]
        # One run id per engine run = per Run Batch (WORKERS>1 shards get one each). Stamped on every
        # doc for L2 batch lineage.
        self.run_id = "run_" + uuid.uuid4().hex[:12]
        for c in ctxs:
            c.crawl_run_id = self.run_id
        # Publish it immediately, with the hosts this run covers, so the per-site view is live for
        # the whole crawl instead of appearing after it ends. Hosts come from the seeds because
        # that is the only site list that exists before the first page lands.
        _RUN_STATE["run_id"] = self.run_id
        _RUN_STATE["hosts"] = sorted({h for c in ctxs for u in c.job.seed_urls
                                      if (h := (urlsplit(u).hostname or "").lower())})
        for c in ctxs:
            resumed = self._restore_frontier(c) if getattr(c.job, "resume", False) else 0
            if not resumed and self.history is not None and not getattr(c.job, "resume", False):
                # A fresh run must not inherit a half-finished frontier from a previous attempt at
                # the same job_id, or it would silently skip everything the old run had marked done.
                #
                # The danger is that `resume` defaults to FALSE, so any dispatch that forgets to set
                # it wipes whatever that job had queued. A re-dispatch of a large half-crawled site
                # therefore destroys hours of discovery before fetching a single page, and until
                # frontier_clear started logging, without a trace.
                try:
                    n = self.history.frontier_counts(c.job.job_id)
                    if n["pending"] >= _env_int("CRAWLER_KEEP_FRONTIER_ABOVE", 1000):
                        log.warning(
                            "job %s is starting WITHOUT resume but has %d pending url(s) from a "
                            "previous run — dropping them, which restarts discovery from the seed. "
                            "Set resume=true on the job if that is not intended.",
                            c.job.job_id, n["pending"])
                    self.history.frontier_clear(c.job.job_id)
                except Exception:
                    log.debug("frontier clear failed for %s", c.job.job_id, exc_info=True)
            for s in c.job.seed_urls:
                cu = _seed_with_dns(canonicalize_url(s))
                # A blocked host must not get in through the front door either. The link guard in
                # _should_enqueue covers discovered children, but a seed skips it entirely — so a
                # blocked domain re-added to the catalogue by accident would crawl its whole site.
                blk = blocklist.blocked(cu)
                if blk:
                    log.warning("seed %s is blocklisted (%s) — skipping it", cu, blk)
                    continue
                if c.job.careful:               # manual careful=True → force this host careful
                    h = (urlsplit(cu).hostname or "").lower()
                    if h:
                        self.host.force_careful.add(h)
                if cu not in c.seen:
                    c.seen.add(cu)
                    self.frontier.put_nowait(WorkItem(cu, 0, c))
                    self._frontier_track(c, cu, 0, None, False)

        # Store each job's definition up front, so Resume is a job_id and nothing else. Done here
        # rather than at the end because the run we most need to resume is the one that never
        # reached its end.
        self._save_job_states(ctxs, "running", with_config=True)
        # Log in BEFORE the first fetch. A tender portal serves the logged-out teaser to anyone,
        # so a crawl that authenticates halfway through stores two different versions of the site
        # and content-hash dedup then treats the good one as a revision of the teaser.
        await self._ensure_auth(ctxs)

        loop = asyncio.get_running_loop()
        self._last_progress = loop.time()
        self.host._progress = self._mark_progress   # a host cooling down counts as progress, not a wedge (see HostLimiter.slot)
        # Finalizer pool: drains the slow enrich+send tail off the tab-holding critical path so a
        # worker's tab returns to the frontier the instant a page is parsed.
        self._finalize_q = asyncio.Queue(maxsize=_env_int("CRAWLER_FINALIZE_QUEUE", 200))
        finalizers = [asyncio.create_task(self._finalizer_loop())
                      for _ in range(_env_int("CRAWLER_FINALIZERS", 32))]
        # Tab-less C3 drainer pool (CRAWLER_DEFER_C3=1): drains c3_queue off the tab-workers so a
        # WAF-blocked section never parks a tab on a stealth render. _camofox_sem is the real cap.
        self._run_started_iso = _now_iso()
        self.c3_queue = asyncio.Queue(maxsize=_env_int("CRAWLER_C3_QUEUE", 2000))
        c3_workers = [asyncio.create_task(self._c3_worker())
                      for _ in range(_env_int("CRAWLER_C3_WORKERS",
                                              _env_int("CRAWLER_CAMOFOX_CONCURRENCY", 6)))]
        # Tab pool (CRAWLER_TAB_POOL=1): N tab-less workers borrow a shared pool of tabs only for
        # renders, so httpx concurrency (N = CRAWLER_WORKERS_N) is set independently of tab RAM
        # (len(self.tabs)). Default off → one worker per tab, exactly as before. adaptive.max is
        # already min(len(tabs), ...), so concurrent renders never exceed the pool and borrow can't
        # block. Careful/render-heavy batches keep tabs high; httpx sweeps set few tabs + many workers.
        if _env_int("CRAWLER_TAB_POOL", 0):
            self._tab_pool = asyncio.Queue()
            for t in self.tabs:
                self._tab_pool.put_nowait(t)
            self.n_workers = max(1, _env_int("CRAWLER_WORKERS_N", len(self.tabs)))
            workers = [asyncio.create_task(self._pool_worker()) for _ in range(self.n_workers)]
            log.info("tab pool: %d workers over %d tabs", self.n_workers, len(self.tabs))
        else:
            self.n_workers = len(self.tabs)
            workers = [asyncio.create_task(self._worker(t)) for t in self.tabs]
        # Render-lane cap: how many workers may be inside a render at once. The remainder is the
        # httpx reserve — the whole point is that it can never reach zero, so a render-heavy batch
        # cannot stall the cheap work. Floor of 1 (a batch of pure render work must still progress)
        # and ceiling of n_workers-1 (the reserve must never be empty either).
        frac = _env_float("CRAWLER_RENDER_LANE_FRAC", 0.5)
        self._render_lane_cap = max(1, min(self.n_workers - 1, int(self.n_workers * frac))) \
            if self.n_workers > 1 else 1
        log.info("lanes: %d workers, render lane cap %d, httpx reserve %d",
                 self.n_workers, self._render_lane_cap, self.n_workers - self._render_lane_cap)
        _LANE_STATE.update(render_lane_cap=self._render_lane_cap,
                           httpx_reserve=self.n_workers - self._render_lane_cap)
        wall = _env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 0)  # 0 = no chop: run to budget/frontier-empty/idle/STOP
        idle = _env_int("CRAWLER_ENGINE_IDLE_S", 300)      # >= cooldown cap so a cooling host can't false-trip idle
        chopped = False
        try:
            await asyncio.wait_for(self._drain(idle), timeout=(wall or None))  # None => no wall-clock backstop
        except asyncio.TimeoutError:
            chopped = True      # cut off mid-crawl, exactly like an operator STOP → stays resumable
            log.warning("engine wall-clock backstop (%ss) hit — terminating batch", wall)
        for w in workers:
            w.cancel()
        for c in c3_workers:
            c.cancel()
        # Bounded: a cancelled worker still runs its `finally`, and that finally awaits (returning a
        # borrowed tab, closing a render). Against a wedged Chromium those awaits never complete, and
        # an unbounded gather here hangs the whole batch — the flag stays running=True with the
        # frontier empty and the CPU idle. Wait a reasonable moment, then leave them; the loop is
        # about to be torn down regardless.
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, *c3_workers, return_exceptions=True),
                timeout=_env_float("CRAWLER_WORKER_CANCEL_S", 30.0))
        except asyncio.TimeoutError:
            log.warning("workers did not finish cancelling in %.0fs — continuing shutdown",
                        _env_float("CRAWLER_WORKER_CANCEL_S", 30.0))
        # Final pass: retry the needs-network-path backlog through a US/residential egress (no-op unless
        # CRAWLER_NETPATH_FINAL=1 + a proxy is wired). Runs after _drain (idle timer disarmed) and before
        # the finalizer join so its recovered docs flush through the same finalizer pool.
        try:
            await self._netpath_final_pass(ctxs)
        except Exception:
            log.exception("netpath final pass failed")
        # Workers stopped queueing docs; drain the finalizer backlog (enrich + POST) so no kept doc is
        # lost, THEN cancel the finalizers. Bounded so a wedged ingest can't hang shutdown forever.
        try:
            await asyncio.wait_for(self._finalize_q.join(),
                                   timeout=_env_int("CRAWLER_FINALIZE_DRAIN_S", 120))
        except asyncio.TimeoutError:
            log.warning("finalizer drain timeout — some kept docs may be unsent")
        for f in finalizers:
            f.cancel()
        await asyncio.gather(*finalizers, return_exceptions=True)
        # Final durable-frontier write. This is the one that matters most: whatever is still
        # pending here is exactly what a resume must pick up, whether the run ended cleanly, on an
        # operator STOP, or on the engine wall-clock. Anything left unflushed would simply be
        # re-crawled next time — correct, but slow, which is the whole point of resume.
        # Stamped BEFORE the flush: the summary must report an interrupted run even if persisting
        # the frontier fails, or a STOP during a disk problem would still be reported as success.
        interrupted = stop_requested() or chopped
        for c in ctxs:
            c.interrupted = interrupted         # so summary()["stop_reason"] can report it
        try:
            await self._flush_frontier(force=True)
            statuses = {c.job.job_id: _job_end_status(c, interrupted) for c in ctxs}
            self._save_job_states(ctxs, statuses)
            if self.history is not None and _resume_tracking():
                for c in ctxs:
                    if statuses[c.job.job_id] == "completed":
                        # Finished: drop the frontier so the NEXT run of this job is a true re-crawl
                        # (delta) rather than resuming an already-drained one — and so the operator's
                        # Resume list holds ONLY crawls that were actually cut short.
                        #
                        # BUT "completed" is the DEFAULT end status: _job_end_status returns it for
                        # any batch that was not operator-stopped or wall-clock chopped. So a job
                        # that ended for some other reason — its chunk finishing, an instance being
                        # recreated, an unhandled path — was declared complete and had its whole
                        # queue deleted. MEASURED: ~389,000 pending dvidshub URLs vanished exactly
                        # this way, silently, and the cause took hours to find because nothing
                        # logged it.
                        #
                        # A job with tens of thousands of URLs still queued has not "completed" in
                        # any sense the word carries. Keep the frontier and say so. The only end
                        # that legitimately leaves a large queue behind is a max_pages budget, and
                        # that is checked FIRST in _job_end_status, so it never reaches here.
                        n = self.history.frontier_counts(c.job.job_id)
                        if n["pending"] >= _env_int("CRAWLER_KEEP_FRONTIER_ABOVE", 1000):
                            log.warning(
                                "job %s reported completed but still has %d PENDING url(s) — "
                                "keeping its frontier instead of dropping it; it will appear as "
                                "resumable rather than being silently discarded",
                                c.job.job_id, n["pending"])
                            statuses[c.job.job_id] = "stopped"
                            self._save_job_states([c], statuses)
                            continue
                        self.history.frontier_clear(c.job.job_id)
                    else:
                        n = self.history.frontier_counts(c.job.job_id)
                        if n["pending"]:
                            log.info("job %s left %d URLs pending — resumable",
                                     c.job.job_id, n["pending"])
        except Exception:
            log.warning("final frontier flush failed — resume may re-fetch some pages",
                        exc_info=True)
        self.history.close()
        return ctxs

    def _refill_host_caps(self) -> None:
        """Big-site fill — keep the pool busy once the small sites finish.

        The per-host cap is sized ONCE at batch start from the SEED host count
        (_adaptive_host_concurrency: 256 tabs // 89 hosts → 6) and then frozen into each host's
        Semaphore. So when 85 small sites complete, the 2-3 remaining big hosts (a 40k-page news
        site) stay pinned at 6 and ~250 of 256 workers sit idle — the batch tail crawls at 2% of
        the pool. Here, whenever work remains but the pool is underused, we grow the ACTIVE hosts'
        caps toward an even split of the pool.

        Self-limiting: growth stops as soon as _inflight_items climbs back over the busy mark, so a
        wide batch never widens at all. Growth-only, and careful/override hosts are skipped — a weak
        or .gov box is never sped up by this."""
        tabs = (self.n_workers or len(self.tabs)) or 1   # frontier concurrency budget (pool: workers)
        if self.frontier is None or self.frontier.qsize() == 0:
            return                                  # no queued work → widening buys nothing
        if self._inflight_items >= tabs * _env_float("CRAWLER_HOST_FILL_BUSY", 0.75):
            return                                  # pool already busy → leave politeness alone
        active = [h for h, n in self.host._inflight.items() if n > 0]
        if not active:
            return
        target = min(_env_int("CRAWLER_HOST_CONCURRENCY_MAX", 64),
                     max(1, tabs // len(active)))
        for h in active:
            self.host.retarget(h, target)

    async def _drain(self, idle_s: int) -> None:
        """Return when the frontier is fully drained (nothing queued, nothing in flight, no
        retry pending), or after idle_s with no completed page. Counters are all mutated on
        the loop thread, so a delayed requeue can never slip past this check."""
        loop = asyncio.get_running_loop()
        if self.adaptive_on and psutil is not None:
            psutil.cpu_percent(interval=None)       # prime — the first read is meaningless
            _cpuset_cpu_percent()                   # ditto: it is a delta, so it needs a baseline
        target = _env_int("CRAWLER_CPU_TARGET", 90)
        step = _env_int("CRAWLER_ADAPTIVE_STEP", 2)
        ram_target = _env_int("CRAWLER_RAM_TARGET", 85)     # grow only while RAM has headroom too
        ram_hard = _env_int("CRAWLER_RAM_HARD", 92)         # hard brake — shed load before OOM
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            if stop_requested():                    # operator hit STOP → halt now (file-based:
                log.warning("stop requested — terminating batch")   # reaches child worker procs)
                return
            # CPU+RAM feedback: every ~1s ramp the render-admission ceiling toward the CPU target
            # (additive-increase when BOTH cpu and ram have headroom, back off when cpu is over or ram
            # hits the hard brake). Only renders acquire the gate, so an httpx-only crawl is unaffected.
            tick += 1
            if self.adaptive_on and psutil is not None and tick % 2 == 0:
                # Our CPUSET if we are pinned, the host otherwise. psutil is still called either
                # way: it is a stateful delta and skipping it would corrupt the next reading.
                host_cpu = psutil.cpu_percent(interval=None)
                pinned = _cpuset_cpu_percent()
                cpu = host_cpu if pinned is None else pinned
                ram = _ram_percent()      # cgroup-aware; see the helper for why psutil is not
                new = _aimd_limit(cpu, self.adaptive.limit, target, step,
                                  self.adaptive.min_limit, self.adaptive.max_limit,
                                  ram=ram, ram_target=ram_target, ram_hard=ram_hard)
                if new != self.adaptive.limit:
                    await self.adaptive.set_limit(new)
                _ADAPTIVE_STATE.update(enabled=True, cpu=round(cpu, 1), ram=round(ram, 1),
                                       target=target, render_limit=self.adaptive.limit,
                                       active=self.adaptive.active)
                if tick % 20 == 0:                  # ~10s heartbeat (if the logger is wired to stdout)
                    log.info("adaptive: cpu=%.0f%% ram=%.0f%% render_limit=%d active=%d",
                             cpu, ram, self.adaptive.limit, self.adaptive.active)
            # Big-site fill (~5s cadence): widen the surviving big hosts once the small sites
            # finish, so the batch tail doesn't run at 6 of 256 workers. See _refill_host_caps.
            if tick % 10 == 0 and os.environ.get("CRAWLER_HOST_FILL", "1") != "0":
                self._refill_host_caps()
            await self._flush_frontier()          # batched; no-op until the buffer is worth a write
            c3q = self.c3_queue.qsize() if self.c3_queue is not None else 0
            self._release_held_all()          # edge-triggered releases can miss; this cannot
            _LANE_STATE.update(frontier=self.frontier.qsize(), render_q=self.render_q.qsize(),
                               held=self._held_items)
            if (self.frontier.qsize() == 0 and self.render_q.qsize() == 0
                    and self._held_items == 0        # parked work is OUTSTANDING work
                    and self._inflight_items == 0
                    and self._pending_retries == 0
                    and c3q == 0 and self._inflight_c3 == 0):   # deferred C3 lane must also be empty
                return
            if loop.time() - self._last_progress > idle_s:
                # Report what was stuck so a premature idle-trip is diagnosable: frontier>0 + inflight>0
                # = hung workers; frontier=0 + pending>0 = stuck retries; frontier>0 + inflight=0 = workers
                # not pulling; c3_q/inflight_c3 = deferred stealth backlog. Also count host-slot cooldown waits.
                log.warning("engine idle for %ss — terminating batch (frontier=%d inflight=%d "
                            "pending_retries=%d c3_q=%d inflight_c3=%d slot_waiting=%d)", idle_s,
                            self.frontier.qsize(), self._inflight_items, self._pending_retries,
                            c3q, self._inflight_c3, getattr(self.host, "_waiting", 0))
                return

    def _note_weak_host(self, host: str) -> None:
        """Tally a weak-under-load signal (conn_refused / timeout) and promote the host to CAREFUL
        (conc=1 + delay) once it crosses the threshold. Called from BOTH the fetch-failure policy and
        the per-item watchdog — a host slow enough to blow the watchdog never reaches the policy
        path, so counting only there let it keep full concurrency while parking a tab-worker per URL
        for the whole ITEM_TIMEOUT. That is how two hosts drained the pool and throughput collapsed."""
        if not host or errors.is_careful_host(host) or host in self.host.force_careful:
            return
        self.host_weak[host] = self.host_weak.get(host, 0) + 1
        if self.host_weak[host] >= _env_int("CRAWLER_HOST_WEAK_FAILS", 3):
            self.host.make_careful(host)
            if self.history is not None:
                self.history.set_careful_host(host, True)   # persist → next run starts careful
            self.host_weak[host] = 0                         # one-shot: don't re-persist every failure
            log.info("auto-careful host=%s (weak under load: conn_refused/timeout) — throttled to conc=1",
                     host)

    async def _worker(self, tab) -> None:
        while True:
            # Lane-aware even in legacy (one-tab-per-worker) mode: render work still goes last, and
            # the httpx reserve still applies. Without this the render lane would never be served at
            # all when CRAWLER_TAB_POOL=0, and deferred items would sit in it forever.
            item, took_lane = await self._next_item()   # counts _inflight_items at the pull
            q = self.render_q if took_lane else self.frontier
            processed = False               # see _pool_worker: only a worked page is marked done
            try:
                if stop_requested() or _item_dropped(item):
                    continue                # STOP: drop the item unprocessed so the frontier drains fast
                # Don't park another tab on a host whose permit queue is already deep. The wait in
                # HostLimiter.slot is unbounded and runs INSIDE the watchdog below, so on a conc=1
                # host every extra worker just idles a tab until the watchdog kills it — measured at
                # 97% of all item timeouts. Requeueing costs a few seconds; parking costs 240.
                # ponytail: queued-only, deliberately — deflecting on in-flight would throttle a
                # healthy conc=24 host. Damper, not a hard bound: a burst of workers can all read
                # depth before any reaches slot() and increments. Bounds the sustained queue (the
                # 22-tab case), not a single tick. Reserve at check time if that ever matters.
                qhost = (urlsplit(getattr(item, "url", "")).hostname or "").lower()
                # Through the helper, like the other deflect site: a per-host override that applied
                # on only one of the two paths would raise the limit and then be quietly re-imposed
                # by the other, which is worse than not having it.
                if qhost and self.host.queue_depth(qhost) >= self._host_queue_max(qhost):
                    _tick_stat("deflected_host")
                    self._schedule_busy(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
                    continue
                # Per-item watchdog: no single page may wedge a tab-worker forever (a hung finalizer put
                # on a slow/stuck ingest, a render that never reaches networkidle, a wedged to_thread).
                # Without it one stuck item pins _inflight_items > 0 and blocks the clean frontier-empty
                # exit until the 900s idle timer. Generous (240s) so only a genuine hang trips it; the
                # cancelled item releases its slots via their context managers and the tab is recycled.
                tab = await asyncio.wait_for(self._process(item, tab),
                                             timeout=_env_int("CRAWLER_ITEM_TIMEOUT_S", 240))
                processed = True
            except RenderDeferred:
                pass                    # moved to the render lane; still outstanding, NOT done
            except RenderBusy:
                # Render pool saturated (globally, or this host's share). Give the page budget back
                # and requeue — parking the tab here is exactly what starved the pool before.
                try:
                    item.ctx.unreserve()     # reserve() ran in _process before the render path
                except Exception:
                    pass
                _tick_stat("deflected_render")
                self._requeue_render(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
            except asyncio.TimeoutError:
                log.warning("item timeout — abandoning %s, recycling tab", getattr(item, "url", "?"))
                item.last_render_error = f"item watchdog ({_env_int('CRAWLER_ITEM_TIMEOUT_S', 240)}s) expired"
                self._retry_or_log_render(item)
                # Deliberately does NOT feed the weak-host tally. Measured: 97% of item timeouts are
                # on hosts ALREADY at concurrency 1 — they time out queueing for the permit, not
                # fetching. Promoting on this signal just makes more conc=1 hosts, i.e. more queues.
                # The queue cap in _worker is what actually stops the pool drain.
                try:
                    tab = await self._recycle(tab)
                except Exception:
                    pass                     # next item's slot()/recycle will replace a dead tab
            except Exception:
                log.exception("worker page failed: %s", getattr(item, "url", "?"))
            finally:
                # Park the tab on about:blank between items. A tab left sitting on the last
                # rendered page keeps compositing that page's animations forever (marketing
                # sites run rAF/CSS loops), and Playwright launches with
                # --disable-background-timer-throttling, so nothing throttles it. Measured per
                # idle tab: loaded page 5.8% of a core, about:blank 0.1%. At W*T tabs that idle
                # burn is ~9 cores, which the adaptive controller reads as "no CPU headroom" and
                # answers by throttling renders to the floor — the idle tabs starve the real
                # ones. Safe here: _process has already snapshotted to FetchResult, the
                # finalizer works off that snapshot, and cookies live in the BrowserContext,
                # not the page. Env-guarded only as an escape hatch for a capture mode that
                # needs the tab left intact after _process returns.
                if _blank_between_items():
                    try:
                        await tab.goto("about:blank")
                    except Exception:
                        pass                     # tab is dead/closing — _recycle handles it next item
                if took_lane:
                    self._release_lane()
                # Durable frontier: done ONLY if the page was really worked. A STOP-dropped, a
                # deferred and a requeued item are all still outstanding, and recording any of them
                # done would make a resume skip a page that was never fetched.
                if processed:
                    self._frontier_complete(item.ctx, item.url)
                self._inflight_items -= 1
                _LANE_STATE.update(frontier=self.frontier.qsize(), render_q=self.render_q.qsize())
                q.task_done()

    # ── durable frontier / resume ────────────────────────────────────────────
    def _restore_frontier(self, ctx: "JobCtx") -> int:
        """Reload a job's frontier from sqlite. Returns the number of pending URLs re-queued.

        `seen` is restored from pending AND done rows, which is the half that actually saves the
        time: without it, every already-crawled page would be re-discovered as a link, re-enqueued
        and re-fetched — content-hash dedup would keep the corpus correct while the crawl paid the
        full cost a second time, which is exactly the problem resume exists to solve."""
        if self.history is None:
            return 0
        jid = ctx.job.job_id
        try:
            pending = self.history.frontier_pending(jid)
            seen = self.history.frontier_seen(jid)
            state = self.history.job_state(jid) or {}
        except Exception:
            log.warning("resume: could not read frontier for %s — starting fresh", jid,
                        exc_info=True)
            return 0
        if not pending and not seen:
            log.info("resume: nothing stored for job %s — starting fresh", jid)
            return 0
        ctx.seen |= seen
        # Re-apply the trap guard to what we are about to re-queue. A saved frontier is a snapshot
        # of what the guards allowed AT THE TIME IT WAS WRITTEN, so without this every guard we add
        # is retroactively defeated by any crawl that stopped before it shipped — the URLs come
        # straight back off disk and are fetched. Measured on the frontier this ran against:
        # cyientdlm.com had 316 pending rows of a relative-link path-accumulation trap, all of them
        # refused by the guard that now ships, all of them about to be fetched again on resume.
        # Marked seen as well as skipped, so nothing re-discovers and re-enqueues them either.
        dropped = 0
        for url, depth, parent, needs_render in pending:
            if errors.looks_like_trap(url):
                ctx.seen.add(url)
                dropped += 1
                continue
            # Seed depth_at from the saved rows, or every resume forgets the relaxations the last
            # run earned and re-converges from scratch — on a site whose depths took a full pass to
            # collapse, that is the whole benefit thrown away once a night.
            da = getattr(ctx, "depth_at", None)
            if da is not None:
                da[url] = min(da.get(url, depth), depth)
            item = WorkItem(url, depth, ctx, parent_url=parent, needs_render=bool(needs_render))
            (self.render_q if needs_render else self.frontier).put_nowait(item)
        if dropped:
            log.info("resume job=%s: dropped %d saved url(s) the trap guard now refuses", jid, dropped)
        # Restore the spent budget too, or a resumed job gets a fresh max_pages allowance and
        # overshoots — a 500-page cap would become 500 MORE pages on every resume.
        done = int(state.get("pages_done") or 0)
        if done:
            ctx.budget_used = max(getattr(ctx, "budget_used", 0), done)
        log.info("resume job=%s: %d pending re-queued, %d urls already seen, %d pages already done",
                 jid, len(pending) - dropped, len(seen), done)
        return len(pending) - dropped

    def _frontier_track(self, ctx: "JobCtx", url: str, depth: int,
                        parent: str | None, needs_render: bool) -> None:
        """Buffer a newly-enqueued URL for the durable frontier. Buffered, never written inline:
        a crawl enqueues thousands of URLs a minute and a sqlite round trip on the frontier's hot
        path would cost more than the resume saves."""
        if self.history is None or not _resume_tracking():
            return
        self._frontier_add_buf.setdefault(ctx.job.job_id, []).append(
            (url, depth, parent, needs_render))

    def _frontier_relax(self, ctx: "JobCtx", url: str, depth: int) -> None:
        """Buffer a depth-min relaxation. Same buffered path as _frontier_track for the same reason:
        a shallow hub re-sights hundreds of already-seen URLs in one parse, and a sqlite round trip
        per link would put the cost on the hot path."""
        if self.history is None or not _resume_tracking():
            return
        # Created on demand as well as in __init__. _flush_frontier swallows its exceptions by
        # design (losing a frontier write must never end a crawl), so an engine built by a path that
        # forgot this buffer would not crash — it would log a warning once and silently stop
        # relaxing depths forever, which is the failure this whole change exists to prevent.
        if getattr(self, "_frontier_relax_buf", None) is None:
            self._frontier_relax_buf = {}
        self._frontier_relax_buf.setdefault(ctx.job.job_id, []).append((url, depth))

    def _frontier_complete(self, ctx: "JobCtx", url: str) -> None:
        """Buffer a processed URL so a resume knows not to fetch it again."""
        if self.history is None or not _resume_tracking():
            return
        self._frontier_done_buf.setdefault(ctx.job.job_id, []).append(url)

    async def _flush_frontier(self, force: bool = False) -> None:
        """Write the buffered frontier deltas. Called on the periodic tick and at shutdown.

        Writes INLINE on the loop thread, deliberately. The obvious optimisation — hand the sqlite
        work to asyncio.to_thread so it cannot stall the loop — is wrong here: a sqlite3 connection
        is bound to the thread that created it, so every such write raises "SQLite objects created
        in a thread can only be used in that same thread", and because frontier writes are
        best-effort that exception was swallowed as a warning. The result would have been a durable
        frontier that was never written and a resume that silently did nothing. Caught by
        tests/test_resume.py, which exercises this path rather than the DB directly.

        Inline is cheap enough: the batch is capped by CRAWLER_FRONTIER_FLUSH_N (200), and an
        executemany of a few hundred rows is ~1ms — the same loop-thread sqlite pattern every other
        history write already uses. Failures are logged, never raised: losing a frontier write
        costs resume precision for those URLs and must never end a crawl."""
        pending = sum(len(v) for v in self._frontier_add_buf.values())
        doneq = sum(len(v) for v in self._frontier_done_buf.values())
        relaxq = sum(len(v) for v in getattr(self, "_frontier_relax_buf", {}).values())
        if not force and (pending + doneq + relaxq) < _env_int("CRAWLER_FRONTIER_FLUSH_N", 200):
            return
        adds, dones = self._frontier_add_buf, self._frontier_done_buf
        relaxes = getattr(self, "_frontier_relax_buf", {})
        self._frontier_add_buf, self._frontier_done_buf = {}, {}
        self._frontier_relax_buf = {}
        try:
            for jid, rows in adds.items():
                self.history.frontier_add(jid, rows)
            # Relaxations AFTER the adds: a URL enqueued and re-sighted shallower within the same
            # flush window must find its row already present, or the UPDATE matches nothing and the
            # relaxation is silently lost.
            for jid, rows in relaxes.items():
                self.history.frontier_relax(jid, rows)
            for jid, urls in dones.items():
                self.history.frontier_done(jid, urls)
        except Exception:
            log.warning("frontier flush failed (%d adds, %d dones, %d relaxes) — resume may "
                        "re-fetch those", pending, doneq, relaxq, exc_info=True)

    def _save_job_states(self, ctxs: list, status: str | dict[str, str],
                         with_config: bool = False) -> None:
        """Persist per-job progress so a resume restores the spent budget.

        `status` is one string for the whole batch (the "running" tick at start) or a per-job map —
        how a job ended is its own business: one job in a batch can hit its budget while another is
        cut off mid-crawl, and only the second is resumable.

        `with_config` stores the job definition too (done once, at start). Resume then needs only a
        job_id — the operator does not have to re-supply seed_urls/max_depth/capture, where any
        difference would quietly make the "resume" a different crawl."""
        if self.history is None or not _resume_tracking():
            return
        for c in ctxs:
            try:
                cfg = None
                if with_config:
                    try:
                        d = c.job.model_dump() if hasattr(c.job, "model_dump") else c.job.dict()
                        d["resume"] = True          # stored so a replay of it resumes by default
                        cfg = json.dumps(d, default=str)
                    except Exception:
                        cfg = None
                st = status if isinstance(status, str) else status.get(c.job.job_id, "stopped")
                self.history.job_state_save(c.job.job_id,
                                            pages_done=int(getattr(c, "budget_used", 0)),
                                            status=st, job_json=cfg)
            except Exception:
                log.debug("job_state save failed for %s", c.job.job_id, exc_info=True)

    def _render_admit_cap(self) -> int:
        """How many workers may hold a render item RIGHT NOW.

        Two different limits were being conflated, and the gap between them was a livelock:

            _render_lane_cap   a WORKER-MIX limit — how many of the N workers may be doing render
                               work at once, so the httpx reserve is never empty. Sized from the
                               worker count: 128 here.
            adaptive.limit     how many renders may ACTUALLY RUN, moved by the CPU governor. Under
                               load it walks down to its floor: 4.

        Admission was gated on the first alone. So 128 workers pulled render items while only 4
        could ever get a permit; the other 124 raised RenderBusy and requeued on a flat 3s timer,
        forever. Measured live across the fleet:

            fetched 4,075 · deferred_render 4 · deflected_render 140,158

        Thirty-four bounces per fetch, for FOUR pages that genuinely needed a browser — and one
        instance sat at render_q=6,100 with active=4. The spin is self-feeding: the CPU it burns is
        what holds the governor at its floor, which keeps the gap open. That is the whole reason the
        re-crawl ran at 2-3 pages/s with 32 tabs per instance idle.

        Never returns 0: the render lane must keep making progress or a render-only site starves
        outright, which is the opposite failure and one this codebase has already had.
        """
        lim = getattr(getattr(self, "adaptive", None), "limit", None)
        if not isinstance(lim, int) or lim <= 0:
            return self._render_lane_cap
        return max(1, min(self._render_lane_cap, lim))

    async def _next_item(self) -> tuple[WorkItem, bool]:
        """Take the next item, preferring the cheap lane. Returns (item, took_render_lane).

        See `_pull_render` for why render items are not taken blindly.

        The caller MUST call `_release_lane()` when done if the flag is True.

        Order of preference, and why:
          1. anything in the httpx frontier — render work is last, as intended;
          2. a render item, but only if fewer than `_render_lane_cap` workers are already rendering,
             which is what keeps the httpx reserve non-empty;
          3. otherwise block on BOTH queues and take whichever produces first, so a worker never
             sleeps while there is work of either kind. Waiting on the frontier alone would deadlock
             a batch that is entirely render work; waiting on render_q alone would starve httpx.

        Rule 1 is a STRONG preference, not an absolute one. As strict priority it starved the render
        lane outright: the frontier has to be entirely empty before any worker looks at render_q, so
        one big httpx site in the batch means render work never runs at all. Measured on a 5-site
        wave — tender18.com (11k pages, httpx) kept the frontier non-empty for 84 minutes while
        gem.gov.in and eprocure.gov.in sat at ONE page each with 67 and 70 URLs stranded in the
        render lane. Those two are `.gov`, so they are careful hosts, so they skip the httpx probe
        entirely (see _fetch_page) and every one of their URLs is a render item. Crawled alone,
        gem.gov.in does 7 pages in 50s — nothing about it is slow; it simply never got a turn.
        So the cheap lane keeps priority for a bounded run of pulls only: after
        CRAWLER_RENDER_LANE_EVERY consecutive httpx pulls, a waiting render item goes first. That
        floors the render lane at ~1/(N+1) of all pulls no matter how deep the httpx queue is.
        """
        # _inflight_items is incremented HERE, the instant an item leaves a queue — NOT in the
        # worker after the await returns. The race that made a solved zoopla stop after page 1: a
        # render item was removed from render_q while `await asyncio.wait` resolved, but the worker
        # counted it in-flight only after the await, and in that window the drain sampled
        # render_q.qsize()==0 AND inflight==0, declared the batch done, and cancelled the workers
        # with 20 items in limbo — no logs, no records, just gone. Counting at the pull makes
        # "left the queue" and "counted in-flight" a single step with no yield between them.
        while True:
            # Anti-starvation turn: the cheap lane has had its bounded run, so a waiting render item
            # jumps the queue. Falls through when render_q is empty, which is the common case.
            if (self._httpx_since_render >= _env_int("CRAWLER_RENDER_LANE_EVERY", 4)
                    and self._render_lane_used < self._render_admit_cap()):
                item = self._pull_render()
                if item is not None:
                    self._render_lane_used += 1
                    self._inflight_items += 1
                    self._httpx_since_render = 0
                    _LANE_STATE["render_lane_used"] = self._render_lane_used
                    return item, True
            try:
                it = self.frontier.get_nowait()
                self._inflight_items += 1
                self._httpx_since_render += 1
                return it, False
            except asyncio.QueueEmpty:
                pass
            if self._render_lane_used < self._render_admit_cap():
                item = self._pull_render()
                if item is not None:
                    self._render_lane_used += 1
                    self._inflight_items += 1
                    self._httpx_since_render = 0
                    _LANE_STATE["render_lane_used"] = self._render_lane_used
                    return item, True
            # Both empty (or the render lane is full) — wait on whichever can serve us. When the
            # lane is full we deliberately wait on the frontier ONLY, so this worker stays part of
            # the httpx reserve instead of joining a queue it is not allowed to serve.
            waiters = [asyncio.ensure_future(self.frontier.get())]
            if self._render_lane_used < self._render_admit_cap():
                waiters.append(asyncio.ensure_future(self.render_q.get()))
            try:
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                for w in waiters:
                    w.cancel()
                raise
            got: tuple[WorkItem, bool] | None = None
            for i, w in enumerate(waiters):
                if w in done and got is None:
                    is_render = (i == 1)
                    item = w.result()
                    # The blocking waiter cannot filter by host the way _pull_render does, and this
                    # is exactly the end-game for a lane full of ONE capped site: frontier empty,
                    # render_q deep, every entry the same host. Taking it here would restart the
                    # slot→RenderBusy→requeue churn the pull-side check exists to stop. Put it back
                    # and let the loop re-enter, where _pull_render can skip past it.
                    if is_render and self._render_host_at_cap(getattr(item, "url", "")):
                        # PARK IT, do not put it back. Putting it back is what made this a
                        # busy-wait: with the frontier empty and the lane owned by one capped
                        # host, every worker took an item, saw the cap, returned it and slept
                        # 250ms - about four full laps a second each, for as long as the queue
                        # lasted. Through the hold it leaves the lane until a permit frees.
                        self._lane_release(item)
                        self._lane_put(item)
                        for other in waiters:
                            if other is not w and other not in done:
                                other.cancel()
                        await asyncio.sleep(_env_float("CRAWLER_RENDER_IDLE_S", 0.25))
                        got = None
                        break                     # re-enter the outer while, nothing taken
                    if is_render:
                        # THIS ITEM HAS LEFT THE LANE FOR GOOD, so the host's lane count must drop.
                        # Missing this is a one-way ratchet: _lane_hosts only ever grew for hosts
                        # whose work came through the blocking waiter, until it reached the
                        # admission cap and _release_held computed zero room forever. Measured on
                        # the fleet: frontier=0, render_q=0, held=1,895, render_limit=16, active=0
                        # -- every permit free, the lane empty, and thousands of pages parked in a
                        # hold that could no longer release them.
                        self._lane_release(item)
                        self._render_lane_used += 1
                        self._httpx_since_render = 0
                        _LANE_STATE["render_lane_used"] = self._render_lane_used
                    else:
                        self._httpx_since_render += 1
                    self._inflight_items += 1     # count at the pull; see the top-of-method note
                    got = (item, is_render)
                elif w in done:
                    # A second queue also produced while we were choosing. Do NOT drop it — put it
                    # back, or the item is silently lost and the frontier never drains.
                    itm = w.result()
                    q = self.render_q if i == 1 else self.frontier
                    q.put_nowait(itm)
                    q.task_done()
                else:
                    w.cancel()
            if got is not None:
                return got

    def _retry_or_log_render(self, item: WorkItem) -> None:
        """A render did not produce a page. Retry it a bounded number of times, then write it to the
        issues log with the most specific cause we can name — and let the crawl carry on.

        The bound is the point. A page whose render never completes ("endless render") previously
        either burned the watchdog repeatedly with nothing recorded, or vanished into a log line, so
        the operator had no list of what was still broken. CRAWLER_RENDER_MAX_ATTEMPTS (default 4)
        attempts, spaced by a widening backoff, then one issue record naming the obstacle.

        The cause is deliberately specific, not "render failed": the remedy is completely different
        between a Cloudflare interactive wall (unclearable — stop spending renders), a captcha
        (needs a solver), and a page that simply never reaches networkidle (needs a longer settle or
        a different capture). Whatever the crawl already learned about the URL is used to name it."""
        cap = _env_int("CRAWLER_RENDER_MAX_ATTEMPTS", 4)
        item.render_attempts += 1
        if item.render_attempts < cap:
            # Widening backoff: a host that is momentarily wedged usually recovers, and retrying
            # instantly just burns the remaining attempts against the same bad moment.
            delay = _env_float("CRAWLER_RENDER_RETRY_S", 5.0) * item.render_attempts
            log.info("render attempt %d/%d failed (%s) — requeueing %s in %.0fs",
                     item.render_attempts, cap, item.last_render_error[:80], item.url, delay)
            self._requeue_render(item, delay)
            return

        cause, action, evidence = self._classify_unresolved(item)
        # Two very different failures share this terminal branch. A HOST_BUSY item never asked the
        # browser pool for anything — the deflect budget expired while its host's slots stayed full,
        # and the code above forces render_attempts to the cap purely to reach this line. Counting
        # that as a render give-up is what put "1,345 render_gaveup" on the panel beside "3 of 64
        # render permits in use", which reads as a starved browser pool and is the opposite of true.
        _tick_stat("host_busy_gaveup" if cause == errors.HOST_BUSY else "render_gaveup")
        try:
            issues.log().record(item.url, cause, action=action, evidence=evidence,
                                job_id=getattr(item.ctx.job, "job_id", "") or "",
                                attempts=item.render_attempts,
                                detail={"depth": item.depth, "parent": item.parent_url or ""})
        except Exception:
            log.debug("issue log write failed for %s", item.url, exc_info=True)
        try:
            item.ctx.bump_reason(cause)
            item.ctx.unreserve()             # hand the page budget back; the URL is not stored
        except Exception:
            pass
        if self.history is not None:
            try:
                self.history.record_failure(item.url, status=None, category=cause,
                                            failed_at=_now_iso(),
                                            crawl_run_id=item.ctx.crawl_run_id,
                                            detail=evidence)
            except Exception:
                pass
        log.warning("giving up on %s after %d renders: %s (%s)",
                    item.url, item.render_attempts, cause, evidence[:120])

    def _classify_unresolved(self, item: WorkItem) -> tuple[str, str, str]:
        """(cause, action, evidence) for a page that exhausted its renders.

        Uses the last body the crawl actually saw, so the answer names a real obstacle rather than
        restating that the render failed. Falls back to the raw render error, which is still far
        more useful than a generic 'render_crash' because it carries the Playwright/net:: text."""
        body = getattr(item, "last_body", "") or ""
        err = item.last_render_error or "render produced no page"
        if body:
            try:
                det = captcha.detect_http(None, {}, body)
                if det.found:
                    solv = det.solvability
                    if solv == "not_injectable":
                        return (edgecase.CASE_ANTIBOT, edgecase.ACT_SKIP,
                                f"{det.kind} — provably cannot self-clear; further renders are wasted")
                    return (edgecase.CASE_CAPTCHA if solv in ("token_injection", "vendor_task",
                                                              "local_ocr")
                            else edgecase.CASE_ANTIBOT,
                            edgecase.ACT_SOLVE_CAPTCHA, f"{det.kind} (solvability={solv})")
                v = edgecase.diagnose(item.url, status=None, body=body)
                if v.case != edgecase.CASE_OK:
                    return v.case, v.action, v.evidence
            except Exception:
                pass
        low = err.lower()
        if low.startswith("host busy"):
            # The host's render slots never freed within the deflect budget — its in-flight pages are
            # wedged (a tarpit / hard block). Distinct from a network-path block: we reached the host,
            # we just never got a free slot. ACT_SKIP so the crawl stops spending on it this run.
            return (errors.HOST_BUSY, edgecase.ACT_SKIP, err[:180])
        if "watchdog" in low or "timeout" in low or "timed out" in low:
            return (errors.RENDER_TIMEOUT, edgecase.ACT_SKIP,
                    f"render never completed after {item.render_attempts} attempts: {err[:140]}")
        return (errors.RENDER_FAILED, edgecase.ACT_SKIP, err[:180])

    def _release_lane(self) -> None:
        self._render_lane_used = max(0, self._render_lane_used - 1)
        _LANE_STATE["render_lane_used"] = self._render_lane_used

    def _defer_render(self, item: WorkItem, why: str) -> None:
        """Move an item into the render lane instead of rendering it on this worker.

        Called when an httpx fetch has just revealed that the page needs a browser. Rendering here
        would hold this worker for 10-90s doing the most expensive work available, which is exactly
        the starvation the two lanes exist to prevent — so the page goes to the back of the render
        lane and the worker returns to cheap work immediately."""
        # Deliberately does NOT bump render_attempts: moving lanes is not an attempt, and counting
        # it here would spend one of the four renders the page is entitled to before a browser has
        # even touched it. The `not item.needs_render` guard at the call site is what prevents a
        # ping-pong, since a lane item never re-enters the defer branch.
        item.needs_render = True
        _tick_stat("deferred_render")
        log.debug("deferred to render lane (%s): %s", why, item.url)
        # No _pending_retries bookkeeping needed: the put is synchronous, so the item is in
        # render_q before this returns, and _drain's empty-check already includes render_q.qsize().
        # (_requeue_render DOES need it — that one defers the put via call_later, so there is a
        # window where the item is in neither queue.)
        self._lane_put(item)

    def _schedule_retry(self, item: WorkItem, delay: float) -> None:
        """Requeue a transient failure after *delay* seconds (one more attempt)."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries + 1,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits))

    def _schedule_busy(self, item: WorkItem, delay: float) -> None:
        """Requeue a URL we declined to start because its host's permit queue was full.

        Preserves retries AND deadhost_waits: a busy host is neither a failure nor a dead host, and
        bumping either would let a merely-popular host exhaust an item's budget and drop the URL.
        Keeps _pending_retries > 0 so _drain won't call the frontier empty while these are in the air."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits))

    def _schedule_wait(self, item: WorkItem, delay: float) -> None:
        """Requeue a URL held off a COOLING host after *delay*s. Preserves item.retries (a cooldown
        wait is not a failure retry) and bumps deadhost_waits so a permanently-dead host caps out.
        Keeps _pending_retries > 0 so _drain won't exit the single-host frontier during the wait."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits + 1))

    def _do_requeue(self, w: WorkItem) -> None:
        self._pending_retries = max(0, self._pending_retries - 1)
        if self.frontier is not None:
            try:
                self.frontier.put_nowait(w)
            except Exception:
                pass

    async def _emit_items(self, ctx: "JobCtx", items: list[dict],
                          parent_url: str | None = None, depth: int = 1) -> int:
        """Turn feed/API items ({link,title,summary,published}) into synthetic HTML
        pages and run each through the exact build_document → gate → dedup → send
        path as a real fetch. Shared by the feed and API fallbacks. Returns docs SENT.
        parent_url/depth stamp the same provenance C1 does (feed items sit under the seed)."""
        sent = 0
        for it in items:
            url = canonicalize_url(it["link"])
            if url in ctx.seen or not ctx.reserve():
                continue
            ctx.seen.add(url)
            title = it["title"] or url
            summary = it["summary"] or ""
            # Synthetic page: title + summary as minimal HTML — build_document parses
            # this the same as a real page body. Published date carried via fixture hint.
            html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>{summary}</p></body></html>"
            fr = FetchResult(url=url, final_url=url, status=200, content_type="text/html",
                             kind="html", text_html=html, tier=1, fetched_at=_now_iso(),
                             published_hint=it["published"] or None, from_fixture=True)
            hp = HarvestedPage(url=url, depth=depth, fetch=fr, pdf_links=[],
                               image_candidates=[], media_candidates=[], parent_url=parent_url,
                               served_by="feed_api")
            doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                          ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
            if doc is None:
                ctx.bump_reason("no_main_text")
                continue
            g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
            ctx.bump_reason(g.reason)
            if not g.keep:
                ctx.dropped_by_gate += 1
                _tick_stat('dropped_by_gate')
                continue
            if doc.content_hash in ctx.seen_hashes:
                ctx.skipped_duplicate += 1
                _tick_stat('skipped_duplicate')
                continue
            ctx.seen_hashes.add(doc.content_hash)
            ctx.kept += 1
            _tick_stat('kept')
            outcome = await asyncio.to_thread(ctx.ingest.send, doc)
            ctx.sent += 1
            _tick_stat('sent')
            ctx.accepted += 1 if outcome.accepted else 0
            ctx.rejected += 0 if outcome.accepted else 1
            sent += 1
        return sent

    async def _try_api_fallback(self, item: WorkItem, host: str) -> int:
        """Blocked host → emit its registry official-API (DVIDS) items as documents.
        The sanctioned, freshest path — tried before Wayback/feed. Returns docs SENT."""
        ctx = item.ctx
        src = ctx.seed.source_for_url(item.url)
        if src is None or not src.api_url:
            return 0
        timeout_s = float(ctx.seed.capture_defaults.get("timeout_seconds", 30))
        items = await asyncio.to_thread(_api_items, src.api_url, timeout_s)
        return await self._emit_items(ctx, items, parent_url=item.url, depth=item.depth + 1)

    async def _try_feed_fallback(self, item: WorkItem, host: str) -> int:
        """Blocked host → emit its registry RSS/Atom feed items as documents.
        The feed body carries title+summary+date, so we emit it directly rather than
        re-crawl the (same-host, blocked) item pages. Returns docs SENT."""
        ctx = item.ctx
        src = ctx.seed.source_for_url(item.url)
        if src is None or not src.feed_url:
            return 0
        timeout_s = float(ctx.seed.capture_defaults.get("timeout_seconds", 30))
        items = await asyncio.to_thread(_feed_items, src.feed_url, timeout_s)
        return await self._emit_items(ctx, items, parent_url=item.url, depth=item.depth + 1)

    def _c3_proxy_retry_budget(self) -> int:
        """Fresh-IP retries to attempt when a C3 render comes back IP-blocked. 0 (default) = off —
        set CRAWLER_C3_PROXY_RETRY=1 once CamoFox egresses through a rotating proxy (PolyProxy /
        residential), else fresh sessions just reuse the same blocked IP and retrying is wasted."""
        if os.environ.get("CRAWLER_C3_PROXY_RETRY", "0") != "1":
            return 0
        return max(1, _env_int("CRAWLER_C3_PROXY_RETRIES", 2))

    async def _c3_fresh_ip_retry(self, item: "WorkItem", timeout_s: float,
                                 solve_captchas: bool) -> dict | None:
        """A C3 render was an IP-block. Re-render up to the budget, each through a FRESH CamoFox
        session (unique userId ⇒ fresh upstream IP under a rotating gateway). Return the first snap
        whose body is NOT a block page, else None. No-op (None) when the budget is 0."""
        for _ in range(self._c3_proxy_retry_budget()):
            fresh = f"c3px-{uuid.uuid4().hex[:8]}"
            if solve_captchas:
                snap, _ci = await asyncio.to_thread(
                    camofox_client.render_with_solver, item.url, timeout_s, fresh)
            else:
                snap = await asyncio.to_thread(
                    camofox_client.render, item.url, timeout_s, False, fresh)
            if snap is None:
                continue
            if not errors.is_ip_block_page((snap.get("html") or "") or (snap.get("text") or "")):
                log.info("c3_proxy_retry cleared block job=%s url=%s user=%s",
                         item.ctx.job.job_id, item.url, fresh)
                return snap
        return None

    async def _try_camofox_fallback(self, item: WorkItem, host: str, solve_captchas: bool = False) -> bool:
        """Blocked host → C3: stealth-render THIS url through CamoFox and run it through
        the SAME build_document → gate → dedup → enrich → send path as a live fetch — so
        a C3 doc captures everything C1 does (html, text, images, screenshot, PDF, media/
        video links), per the job's capture list. Returns True if a doc was SENT. No-op
        (False) unless CAMOFOX_ENABLED=1. Budget is already reserved at dequeue."""
        if not camofox_client.enabled():
            return False
        ctx = item.ctx
        # Stealth render of a heavy page is inherently slower than a normal fetch — give C3
        # its own generous timeout (not the seed's ~30s httpx budget), tunable via env.
        timeout_s = _env_float("CRAWLER_CAMOFOX_TIMEOUT_S", 90.0)

        # Gate C3 renders through a small semaphore so a WAF-blocked section can't flood CamoFox.
        async with self._camofox_sem:
            if solve_captchas:
                snap, captcha_info = await asyncio.to_thread(camofox_client.render_with_solver, item.url, timeout_s)
            else:
                snap = await asyncio.to_thread(camofox_client.render, item.url, timeout_s)
                captcha_info = camofox_client.CaptchaInfo()

            if not snap:
                # Canonicalization strips www; many gov/mil apexes only serve on www — retry there
                # (mirrors the main render path's www fallback). Retry with a fresh user/session.
                www = _with_www(item.url)
                if www != item.url:
                    if solve_captchas:
                        snap, captcha_info = await asyncio.to_thread(camofox_client.render_with_solver, www, timeout_s)
                    else:
                        snap = await asyncio.to_thread(camofox_client.render, www, timeout_s)

            # LAST RESORT - the paid lane. Free C1 and free C3 have both been refused for this URL,
            # which is exactly the condition paid egress exists for, so free_tiers_tried is True by
            # construction here. Every spend still clears paid_proxy.allow(): master switch, host
            # allowlist, and a hard daily cap that survives restarts. A gate refusal is silent and
            # costs nothing; only a request that actually leaves is metered.
            if not snap and paid_proxy.allow(item.url, free_tiers_tried=True):
                paid_base = paid_proxy.base_url()
                log.info("c3_paid escalation url=%s (free C1+C3 refused)", item.url)
                try:
                    if solve_captchas:
                        snap, captcha_info = await asyncio.to_thread(
                            camofox_client.render_with_solver, item.url, timeout_s, None, paid_base)
                    else:
                        snap = await asyncio.to_thread(
                            camofox_client.render, item.url, timeout_s, False, None, paid_base)
                except Exception as exc:                 # noqa: BLE001 - the paid lane must never crash a crawl
                    log.warning("c3_paid failed url=%s: %s", item.url, exc)
                    snap = None
                paid_proxy.note_use(item.url)            # meter only a request that actually went out
                if snap:
                    _tick_stat("paid_recovered")
        if not snap:
            # C3 gave us nothing. If a captcha was detected on the way, say SO — otherwise this
            # returns silently and the caller books it as a generic render failure, which is how
            # thousands of captcha walls ended up indistinguishable from crashed browsers in the
            # backlog. "The browser died" and "a captcha stopped us" need different fixes.
            if captcha_info.captcha_type:
                self._flag_captcha_limitation(item, captcha_info)
            return False

        final_url = snap.get("final_url") or item.url
        real_html = (snap.get("html") or "").strip()

        # The render SUCCEEDED but the server may still have said no. C3 used to hardcode status=200
        # here, so a 404 body was stored as a real document AND no failure was ever recorded — which
        # is why is_gone() never fired and the same dead URL came back every single run. 19.7% of
        # everything C3 stored was error pages and interstitials, against 0.2% for C1.
        # A None status means the browser could not tell us; that is treated as "carry on", not as
        # an error, so an unavailable timing entry can never silently discard a good page.
        c3_status = snap.get("status")
        if isinstance(c3_status, int) and c3_status >= 400:
            reason = errors.http_reason(c3_status)
            ctx.bump_reason(reason)
            # Feeds dedup.is_gone(): 410 is permanent immediately, 404 needs two strikes. This is
            # the write that makes "it 404s every run" turn into "stop asking".
            try:
                self.history.record_failure(item.url, status=c3_status, category=reason,
                                            failed_at=_now_iso(), crawl_run_id=ctx.crawl_run_id)
            except Exception:
                log.exception("could not record C3 failure for %s", item.url)
            log.info("c3 refused url=%s status=%s — not stored", item.url, c3_status)
            return False

        # C3 stealth beat the browser fingerprint check but the CDN may still IP-block us (Akamai
        # "Access Denied"). That arrives as an HTTP-200 render, so the BODY is the only signal. If
        # it's a block, retry through a fresh proxy IP; if still blocked (or no proxy wired), fail
        # honestly so the ladder continues — never send a block page as served_by_camofox.
        via_proxy = False
        blocked_body = real_html or snap.get("text") or ""
        # Keep the wall we were shown, so whoever finally gives up on this URL can name the OBSTACLE
        # rather than guess. Without it _tag_netpath has nothing to read and every unclearable wall
        # is reported as "the IP is the blocker" — which for a Cloudflare interactive challenge is
        # measurably wrong and sends the operator to buy a proxy that cannot help.
        if blocked_body:
            item.last_body = blocked_body[:200_000]
        if errors.is_ip_block_page(blocked_body) or errors.is_captcha_wall(blocked_body):
            snap = await self._c3_fresh_ip_retry(item, timeout_s, solve_captchas)
            if snap is None:
                # A rendered anti-bot INTERSTITIAL (Cloudflare "Just a moment", a Turnstile/hCaptcha
                # challenge page) is a CAPTCHA limitation, not a generic IP block. Flag it as such and
                # log the URL. classify() may miss it under render timing, so the body markers are a
                # second, independent signal — either one routes it to the captcha lane; only a truly
                # unclassified block stays needs_network_path.
                if captcha_info.captcha_type:
                    self._flag_captcha_limitation(item, captcha_info)
                elif errors.is_captcha_wall(blocked_body):
                    self._flag_captcha_limitation(item, camofox_client.CaptchaInfo(
                        captcha_type="challenge_interstitial", solvability="wait_only",
                        error="anti-bot wall not cleared by C3"))
                else:
                    ctx.bump_reason(errors.NEEDS_NETWORK_PATH)
                    try:
                        self.history.record_failure(
                            item.url, status=None, category=errors.NEEDS_NETWORK_PATH,
                            failed_at=_now_iso(), crawl_run_id=ctx.crawl_run_id)
                    except Exception:
                        log.debug("could not record netpath for %s", item.url)
                return False
            via_proxy = True
            final_url = snap.get("final_url") or item.url
            real_html = (snap.get("html") or "").strip()

        # Anything <400 (or unknown) reaches here; carry the REAL status so the Document records
        # what the server actually said instead of a fabricated 200.
        ok_status = c3_status if isinstance(c3_status, int) else 200
        if real_html:
            # Real rendered HTML → build_document extracts main_text/title/meta/tables and
            # enrich_assets pulls images/PDFs/media exactly like a live page.
            fr = FetchResult(url=item.url, final_url=final_url, status=ok_status,
                             content_type="text/html", kind="html", text_html=real_html,
                             screenshot_png=snap.get("screenshot"), tier=1,
                             fetched_at=_now_iso(), from_fixture=True)
        else:
            # No HTML (e.g. no CAMOFOX_API_KEY) → fall back to the aria snapshot as body.
            wrap = f"<html><head><title>{item.url}</title></head><body></body></html>"
            fr = FetchResult(url=item.url, final_url=final_url, status=ok_status,
                             content_type="text/html", kind="html", text_html=wrap,
                             inner_text=snap["text"], screenshot_png=snap.get("screenshot"),
                             tier=1, fetched_at=_now_iso(), from_fixture=True)

        # Attach C3 solver / proxy provenance to the fetch result so it flows into the Document.
        if captcha_info.captcha_type:
            fr.captcha_type = captcha_info.captcha_type
            fr.captcha_solved = captcha_info.solved
            fr.captcha_solver = captcha_info.solver
            fr.captcha_solver_cost_usd = captcha_info.cost_usd
        if via_proxy:
            # A fresh-IP retry cleared the block. Stamp provenance (flows to Document.proxy_country).
            fr.proxy_country = os.environ.get("CAMOFOX_PROXY_COUNTRY") or "rotated"

        # Capture candidates from the rendered HTML, per the job's capture list (mirrors _process).
        base = final_url
        html = fr.text_html if fr.is_html() else None
        pdf_links, image_candidates, media_candidates = _capture_assets(ctx.job, html, base)
        hp = HarvestedPage(url=item.url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates,
                           parent_url=item.parent_url, served_by="c3_camofox", served_from=final_url)

        # Enqueue child links so a C3-served site keeps crawling into its max_pages budget —
        # without this, a fully WAF-blocked site (served entirely by CamoFox) stops at the seed.
        # Children carry item.url as their parent (same as C1), so the dashboard tree is consistent.
        self._enqueue_links(ctx, fr, item.url, item.depth)

        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return False
        # Propagate captcha/proxy provenance onto the document (build_document doesn't). Without this
        # a page the crawler got INTO by solving a captcha records captcha_type=None — the store can't
        # tell it was ever walled. Mirrors _run_finalize's C1 propagation.
        if fr.captcha_type:
            doc.captcha_type = fr.captcha_type
            doc.captcha_solved = fr.captcha_solved
            doc.captcha_solver = fr.captcha_solver
            doc.captcha_solver_cost_usd = fr.captcha_solver_cost_usd
        if fr.proxy_country:
            doc.proxy_country = fr.proxy_country
        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
        ctx.bump_reason(g.reason)
        if not g.keep:
            ctx.dropped_by_gate += 1
            return False
        if doc.content_hash in ctx.seen_hashes:
            # A DUPLICATE HASH IS NOT ALWAYS A DUPLICATE PAGE.
            #
            # When interactions ran we extract from the rendered inner_text, and on some portals
            # that is the shared shell for every URL — so distinct pages hash alike and all but
            # the first are discarded. Measured on serviziocontrattipubblici.it: 9 fetched, 1
            # kept, 8 "duplicates"; the same 9 URLs extracted from raw HTML give distinct text
            # (1943 / 1036 / 786 chars). We were not failing to REACH those pages, we were
            # throwing them away after paying for them.
            #
            # So re-extract from the markup before dropping, and keep it only if it really is a
            # page we do not have. Costs one CPU-bound extraction on the duplicate path only;
            # a genuine duplicate still hashes the same and is still dropped.
            alt = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                          ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id,
                                          static_text=True)
            if alt is not None and alt.content_hash not in ctx.seen_hashes:
                log.info("duplicate_recovered_from_html job=%s url=%s chars=%d",
                         ctx.job.job_id, item.url, len(alt.main_text))
                doc = alt
            else:
                ctx.skipped_duplicate += 1
                _tick_stat('skipped_duplicate')
                return False
        ctx.seen_hashes.add(doc.content_hash)
        ctx.kept += 1
        # Store the expensive assets (images/PDF/screenshot; media recorded as links) — same
        # enrich step C1 uses. The screenshot PNG captured above is reused (no re-render).
        await asyncio.to_thread(extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        ctx.accepted += 1 if outcome.accepted else 0
        ctx.rejected += 0 if outcome.accepted else 1
        log.info("camofox_fallback job=%s url=%s chars=%d imgs=%d pdfs=%d media=%d captcha=%s solver=%s",
                 ctx.job.job_id, item.url, len(doc.main_text),
                 len(image_candidates), len(pdf_links), len(media_candidates),
                 fr.captcha_type or "none", fr.captcha_solver or "-")
        return True

    async def _recover_deadhost(self, item: WorkItem, host: str) -> bool:
        """A cooling host's queued URL — recover it LIVE through C3 (CamoFox) instead of dropping.
        No-op unless CAMOFOX_ENABLED=1 (camofox_client.enabled() gates _try_camofox_fallback).
        Reserves budget itself (the dead-host check runs before _process's reserve()).

        ponytail: CRAWLER_DEADHOST_RECOVER is GONE. C2 left the ladder, so the var's only remaining
        distinction was "C3 on/off" — which CAMOFOX_ENABLED already says, and a second switch that
        can disagree with the first is worse than no switch. Its default 'c2c3' would have kept
        working BY ACCIDENT ("c3" in "c2c3") while lying about what it does, and a stale 'c2' would
        have silently meant "no recovery at all" ("c3" in "c2" is False).

        Archival recovery is an explicit, dated, EXCLUSIVE mode now (crawler/c2.py). The archive's
        undated "newest capture" was never the same page as the one that just failed, so stamping it
        as this crawl's result was quietly wrong.
        """
        if not item.ctx.reserve():
            return False        # BUGFIX: reserve() returns False WITHOUT incrementing budget_used,
                                # so the old unreserve() here handed back budget never taken —
                                # inflating the page budget of every job that hit a cooling host.
        try:
            if await self._try_camofox_fallback(item, host, solve_captchas=True):
                item.ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                return True
        except Exception:
            pass
        item.ctx.unreserve()                          # nothing recovered → give the budget back
        return False

    async def _on_failure(self, item: WorkItem, fr: FetchResult, tab, host: str, hold=None):
        """Dispatch a failed fetch per the errors policy table. Returns (fr, tab):
        a SUCCESSFUL fr (disguise worked) → caller proceeds down the success path; an fr still
        >=400 / None (or None) → caller counts it and moves on."""
        ctx = item.ctx
        reason = fr.reason or errors.classify_failure(fr.status, fr.error) or errors.OTHER
        status = fr.status
        policy = errors.policy_for_status(status) if status and status >= 400 else None

        # A timeout on a (slow gov) origin → extend its patience before the breaker counts it.
        if reason == errors.TIMEOUT:
            self.host.bump_timeout(host)

        # A crashed Chromium render says nothing about whether FIREFOX can load the page — different
        # engine, different fingerprint, different media/codec handling. _render_fallback has already
        # exhausted the cheap tiers (download refetch, httpx), so C3 is the last real option before
        # the page is written off. Without this, render_crash never escalated at all: the 4xx ladder
        # below is keyed on a status, and a crash has none.
        if reason == errors.RENDER_CRASH and camofox_client.enabled():
            try:
                # solve_captchas=True: a "render crash" is often a captcha wall that stalled the
                # render, so give the solver a shot here too (and record it as a captcha wall, not a
                # crash, if it still can't be cleared).
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    log.info("render_crash → c3 recovered url=%s", item.url)
                    return None, tab          # C3 sent the document itself; nothing left to store
            except Exception:
                pass

        # Any 4xx → ONE human-like browser retry: browser UA + real Accept-Language/locale
        # headers, like a person hitting the page. Careful hosts (.gov/.mil) are included —
        # we still make exactly one polite attempt, then back off. A survivor is retagged
        # NEEDS_NETWORK_PATH (below) so it routes to the different-network-path plan
        # (residential proxy / real headless session / source API), not another retry.
        blocked_4xx = bool(status and 400 <= status < 500)
        if blocked_4xx:
            tab = await self._ensure_tab(tab, hold)     # pool mode: borrow a tab for the browser retry
            try:
                await tab.set_extra_http_headers({
                    "User-Agent": _BROWSER_UA_FALLBACK,
                    "Accept-Language": "en-US,en;q=0.9",
                })
                # BOUNDED, exactly like the primary render path above (CRAWLER_RENDER_WAIT_S).
                # This acquire was the only unbounded one left inside the item watchdog: the retry
                # would wait for a global render permit for as long as it took, and on a saturated
                # pool that is until the 240s watchdog kills the item and pins its worker for the
                # whole time. Measured after bounding the per-host gate: shvabe.com still produced
                # 630 abandonments in six minutes while its pages render in 9.4s, because the wait
                # had simply moved one gate up. RenderBusy is already handled by the worker, which
                # requeues onto the render lane rather than recording a failure.
                async with self.adaptive.slot(timeout=_env_float("CRAWLER_RENDER_WAIT_S", 8.0)):
                    async with self.host.slot(item.url):
                        retried = await _render_page(tab, item.url, self.host.timeout_ms(host),
                                                     ctx.job.capture, ctx.job.interaction)
            except HostSlotBusy:
                # Re-raised past the broad handler below, which would otherwise turn a host being
                # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
                # worker requeues it; nothing about the page went wrong.
                raise
            except Exception as e:
                retried = FetchResult(url=item.url, final_url=item.url, status=None,
                                      fetched_at=_now_iso(), error=f"render:{e}",
                                      reason=errors.RENDER_CRASH)
                tab = await self._recycle_into(tab, hold)
            finally:
                try:
                    await tab.set_extra_http_headers({})
                except Exception:
                    pass
            if retried.status and 200 <= retried.status < 400 and not retried.error:
                return retried, tab                 # human-like retry worked → success path
            fr = retried
            status = fr.status
            # Still 4xx after a full human-like session. Before giving up, try the
            # source's un-gated RSS/Atom feed (if the registry has one): crawl its
            # item links instead of the blocked HTML. Only when there's no feed (or
            # it yields nothing) do we tag needs_network_path for the proxy/API plan.
            # A TRANSPORT-LEVEL REFUSAL NEVER HAD A STATUS TO GATE ON.
            #
            # The branch below fires on 4xx, which is the only impersonate call a render-first
            # job can reach — and a host that drops the connection (conn_refused, TLS reset)
            # produces NO status at all, so it fell straight through to needs_network_path
            # without the cheap Chrome-JA3 GET ever being tried. The httpx lane already treats
            # this case as impersonate-worthy via _transport_blocked(); the render lane did not,
            # and render-first is exactly where careful/always-render hosts live.
            #
            # LEARNED FROM SCRAPLING. On contrataciondelestado.es (es-pcsp, render+careful) it
            # took 55 pages in 57s while we recorded conn_refused and stored nothing. It has no
            # cleverer bypass — its stealth session simply presents a real browser TLS stack on
            # the FIRST byte, where we lead with plain httpx and only escalate on a status code
            # that a refused connection never produces. Same tier (C1's JA3 GET), one condition
            # wider.
            if _transport_blocked(fr):
                imp = await self._impersonate_try(item, item.url, host)
                if imp is not None:
                    ctx.bump_recovered(errors.SERVED_BY_IMPERSONATE)
                    log.info("impersonate_after_transport_block job=%s host=%s url=%s err=%s",
                             ctx.job.job_id, host, item.url, str(fr.error)[:80])
                    return imp, tab

            if status and 400 <= status < 500:
                # Cheap JA3 GET before the expensive C3 spin-up — a TLS-fingerprint block often
                # clears here (always-render/careful hosts never ran the httpx-path impersonate).
                imp = await self._impersonate_try(item, item.url, host)
                if imp is not None:
                    ctx.bump_recovered(errors.SERVED_BY_IMPERSONATE)
                    log.info("impersonate_fallback job=%s host=%s url=%s", ctx.job.job_id, host, item.url)
                    return imp, tab
                # DEFER the expensive C3 → API → feed tail onto the tab-less c3_queue (drained by
                # _c3_worker). Frees THIS tab immediately so a WAF-blocked section can't park the pool
                # on 90s stealth renders. Terminal GONE (404/410) never defers — it falls through to the
                # inline gone branch below. No-op (inline path unchanged) unless CRAWLER_DEFER_C3=1.
                if self._defer_c3_enabled(status):
                    self._enqueue_c3(item, host, status)
                    return None, tab
                # C3 first: CamoFox stealth engine renders the LIVE page with engine-level
                # fingerprint spoofing — the freshest content if the WAF can be bypassed.
                # No-op unless CAMOFOX_ENABLED=1, so the ladder falls through when C3 is off.
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)   # bypassed the block, not a failure
                    return None, tab
                # WAF-free content ladder — try the cheapest/fullest path that lands:
                # 1st: official JSON API (DVIDS) — sanctioned, freshest, full bodies.
                api_sent = await self._try_api_fallback(item, host)
                if api_sent:
                    ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)  # API was the workaround → recovered
                    log.info("api_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, api_sent)
                    return None, tab
                # 2nd: RSS/Atom feed — broader fallback (summaries of the source's items).
                # (C2/archive is NOT a rung here any more: it is a separate, explicitly dated mode —
                #  see crawler/c2.py. An undated "newest capture" was never the page that failed.)
                sent = await self._try_feed_fallback(item, host)
                if sent:
                    ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)  # feed was the workaround → recovered
                    log.info("feed_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, sent)
                    return None, tab                 # blocked HTML abandoned; feed docs already sent
                # The ladder ran and nothing rescued the page. For most 4xx that genuinely means
                # "needs a different network path" — but NOT for a 404/410. Those are DEAD, and
                # retagging them here made `record_failure` persist a deleted page as RETRYABLE:
                # the batch report told the operator it might come back, and the URL was re-crawled
                # every single run forever. Measured end-to-end on idrw.org/lt-k9-vajra-followon,
                # a genuinely deleted article, reported as needs_network_path after 2 attempts.
                # The GONE branch below already keyed off `policy`, so only the stored reason was
                # ever wrong — which is exactly the kind of lie that is hardest to notice.
                reason = (errors.status_reason(status)
                          if errors.policy_for_status(status) == errors.GONE
                          else errors.NEEDS_NETWORK_PATH)
                fr.reason = reason
            else:
                reason = fr.reason or errors.classify_failure(fr.status, fr.error) or errors.OTHER
            policy = errors.policy_for_status(status) if status and status >= 400 else None

        # Persist every failure so 'gone' and 'retry next run' survive across runs.
        if self.history is not None:
            # fr.error carries the raw text ("render:Timeout 30000ms exceeded", "nav:net::ERR_…").
            # It is the only thing that makes a render_crash bucket diagnosable after the run.
            self.history.record_failure(item.url, status=status, category=reason,
                                        failed_at=fr.fetched_at or _now_iso(),
                                        crawl_run_id=ctx.crawl_run_id, detail=fr.error)

        # 404/410 → permanently gone (wires dedup.classify's gone branch via record_failure/is_gone).
        if policy == errors.GONE:
            ctx.bump_error(reason)
            return fr, tab

        # 429/503 → cool the WHOLE host down (Retry-After honored, capped).
        if policy == errors.COOLDOWN:
            self.host.cooldown(host, fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 30.0))

        # Hard network failures (DNS/refused/SSL) feed the per-host circuit breaker. When SSL
        # bypass is on, a cert error is NOT a reason to kill the host (Chromium proceeds past it).
        if reason in errors.HARD_FAIL and not (reason == errors.SSL and _ignore_https_errors()):
            self.host_fails[host] = self.host_fails.get(host, 0) + 1
            # Back off the host on EVERY hard failure, not only when the breaker trips. Without
            # this there is no backoff at all between fail 1 and fail `thr` — the pool keeps
            # slamming a host that is already refusing, trips the breaker, and after each
            # cooldown the revived URLs re-trip it in `thr` requests. That oscillation is how one
            # refusing host (sipri.org) turned into 616 conn_refused / 769 skipped. Progressive:
            # 2s, 4s, 6s... so a host that refuses once gets room to recover before we try again.
            # ponytail: reuses the 429/503 _next_ok gate cooldown() already drives — no new
            # machinery, and cooldown() self-caps at CRAWLER_COOLDOWN_CAP_S.
            self.host.cooldown(host, _env_float("CRAWLER_HARD_FAIL_BACKOFF_S", 2.0)
                               * self.host_fails[host])
            # Careful gov/mil hosts get 2x headroom (their failures are often transient under load).
            thr = _env_int("CRAWLER_HOST_HARD_FAILS", 6)
            if errors.is_careful_host(host) or host in self.host.force_careful:
                thr *= 2
            if self.host_fails[host] >= thr:
                # COOL the host down (not permanent death): its queued URLs are recovered via
                # C2/C3 while cooling, then it revives for a fresh live attempt after the window.
                self.dead_hosts.add(host)
                self.host_cooldown[host] = (asyncio.get_running_loop().time()
                                            + _env_float("CRAWLER_HOST_COOLDOWN_S", 120.0))
                self.host_fails[host] = 0
                log.warning("host_cooldown host=%s (recovering queued URLs via C2/C3)", host)

        # A HOST THAT IS REFUSING US, as opposed to one that is broken. 401/403 is not a network
        # failure, so it never reached the breaker above — and the cost of that is not one wasted
        # request, it is the whole recovery ladder per URL: a CamoFox render, an API probe and a
        # feed probe, all of which also fail, because the site is not rate-limiting us, it is saying
        # no. Measured 2026-07-31: godrejenterprises.com answered 403 to 4,954 queued URLs and one
        # instance spent hours on it while storing nothing.
        #
        # CONSECUTIVE, and cleared by any success on the host (see the reset beside host_fails), so
        # a site with a members-only section keeps its public pages — the streak breaks the moment
        # one page comes back. Reuses dead_hosts + host_cooldown rather than adding a mechanism:
        # queued URLs are then recovered via C2/C3 while cooling, and the host revives for a fresh
        # attempt after the window, which is right — a WAF rule can be lifted.
        if status in (401, 403) and reason != errors.SERVED_BY_CAMOFOX:
            self.host_forbidden[host] = self.host_forbidden.get(host, 0) + 1
            if self.host_forbidden[host] >= _env_int("CRAWLER_HOST_FORBIDDEN_MAX", 15):
                self.dead_hosts.add(host)
                self.host_cooldown[host] = (asyncio.get_running_loop().time()
                                            + _env_float("CRAWLER_HOST_FORBIDDEN_COOLDOWN_S", 900.0))
                self.host_forbidden[host] = 0
                log.warning("host_forbidden host=%s — %s consecutive 401/403 with no page served; "
                            "cooling off (queued URLs recovered via C2/C3)", host,
                            _env_int("CRAWLER_HOST_FORBIDDEN_MAX", 15))

        # A HOST WHOSE RENDERS KEEP CRASHING. The fourth breaker, and the one whose absence was
        # measured most expensively: instance 0 burned 20.72 of the fleet's 36.9 cores — more than
        # the other six crawling instances combined — in a render_crash retry loop over
        # godrejenterprises.com and automation.honeywell.com, with 8,570 URLs queued behind it.
        # Every crash spawns a fresh browser context and re-queues, so the cost per URL is a whole
        # Chromium launch and the loop has no natural end.
        #
        # None of the other three could catch it: it is not a network failure (nothing refused us),
        # not a 401/403 (no HTTP answer at all), and the host is not saturated — it is the BROWSER
        # dying on that site's pages. Same consecutive-with-reset shape as the 403 breaker, so a
        # site that renders fine except for a handful of bad pages keeps its crawl.
        if reason == errors.RENDER_CRASH:
            self.host_crashes[host] = self.host_crashes.get(host, 0) + 1
            if self.host_crashes[host] >= _env_int("CRAWLER_HOST_CRASH_MAX", 25):
                self.dead_hosts.add(host)
                self.host_cooldown[host] = (asyncio.get_running_loop().time()
                                            + _env_float("CRAWLER_HOST_CRASH_COOLDOWN_S", 900.0))
                self.host_crashes[host] = 0
                log.warning("host_render_crash host=%s — the browser keeps dying on this site; "
                            "cooling off (queued URLs recovered via C2/C3)", host)

        # Weakness under load (conn_refused/reset/timeout) → auto-slow to CAREFUL (conc 1 + delay)
        # BEFORE the dead-host breaker trips (mirrors host_render_always: learn in-run, persist, seed
        # next run). Careful's throttle is what STOPS the refuse/reset cascade (the SIPRI case), so it
        # must fire below CRAWLER_HOST_HARD_FAILS. Reaches here for both CONN_REFUSED (a HARD_FAIL) and
        # TIMEOUT (which returns at the transient requeue just below) → count before that return.
        if reason in errors.WEAK_UNDER_LOAD:
            self._note_weak_host(host)

        # Transient (5xx/429/timeout/render_crash) → requeue and try again.
        transient = policy in (errors.RETRY_LATER, errors.COOLDOWN) or reason in errors.TRANSIENT
        if transient and item.retries < _env_int("CRAWLER_INRUN_RETRIES", 3):
            ctx.unreserve()                          # give the page budget back for the retry
            base = fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 8.0)
            # EXPONENTIAL, and jittered. Every URL that fails in the same instant (a host hiccup, a
            # browser pool wobble) was previously rescheduled to the identical second, so they all
            # came back together and re-created the pile-up that broke them. The spread is what
            # stops attempt 2 from being attempt 1 with more contention.
            delay = min(base * (2 ** item.retries), 120.0) * random.uniform(0.5, 1.5)
            self._schedule_retry(item, delay)
            log.info("retry job=%s url=%s reason=%s attempt=%d delay=%.0fs",
                     ctx.job.job_id, item.url, reason, item.retries + 1, delay)
            return None, tab                         # not counted — the retry counts if it fails again

        ctx.bump_error(reason)
        log.info("fetch_fail job=%s url=%s status=%s reason=%s",
                 ctx.job.job_id, item.url, status, reason)
        return fr, tab

    # ── deferred C3 lane (CRAWLER_DEFER_C3=1) ────────────────────────────────
    def _defer_c3_enabled(self, status=None) -> bool:
        """Defer the C3/API/feed tail to the tab-less c3_queue? Only with CRAWLER_DEFER_C3=1 and C3
        available, and NEVER for a terminal GONE (404/410 — those take the inline gone branch)."""
        return (os.environ.get("CRAWLER_DEFER_C3", "1") == "1"  # default 1 (audit F2): prod sets it,
                # and the only C4 rung for interactive walls lives in this deferred lane.
                and camofox_client.enabled()
                and (not status or errors.policy_for_status(status) != errors.GONE))

    def _enqueue_c3(self, item: WorkItem, host: str, status) -> None:
        """Push a blocked page onto the deferred C3 lane. Non-blocking (put_nowait) so a tab is never
        parked; if the lane is saturated, tag needs_network_path now instead of blocking."""
        try:
            self.c3_queue.put_nowait((item, host, status))
        except asyncio.QueueFull:
            self._tag_netpath(item, status)

    async def _c3_worker(self) -> None:
        """Drain the deferred C3 lane off the tab-workers (mirrors _finalizer_loop + the _worker STOP
        discipline). _camofox_sem inside _try_camofox_fallback is the real concurrency cap."""
        q = self.c3_queue
        while True:
            item, host, status = await q.get()
            self._inflight_c3 += 1
            try:
                if stop_requested():
                    continue                     # STOP: drop unprocessed so the lane drains fast
                await asyncio.wait_for(self._run_c3_ladder(item, host, status),
                                       timeout=_env_int("CRAWLER_C3_ITEM_TIMEOUT_S", 300))
            except asyncio.TimeoutError:
                log.warning("c3 item timeout — %s", getattr(item, "url", "?"))
                try:
                    self._tag_netpath(item, status)   # a hung stealth render → record needs_network_path
                except Exception:
                    pass
            except Exception:
                log.exception("c3 worker failed: %s", getattr(item, "url", "?"))
            finally:
                self._mark_progress()            # a slow stealth render is progress, not a wedge
                self._inflight_c3 -= 1
                q.task_done()

    async def _run_c3_ladder(self, item: WorkItem, host: str, status=None) -> None:
        """The expensive tail lifted from _on_failure (C3 → API → feed → tag needs_network_path), run
        off the tab. Behaviour is identical to the inline ladder, just not parking a worker.
        _try_camofox_fallback enqueues any C3-discovered child links back onto the frontier."""
        ctx = item.ctx
        if await self._try_camofox_fallback(item, host, solve_captchas=True):
            ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
            return
        if await self._try_api_fallback(item, host):
            ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)
            log.info("api_fallback job=%s host=%s", ctx.job.job_id, host)
            return
        if await self._try_feed_fallback(item, host):
            ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)
            log.info("feed_fallback job=%s host=%s", ctx.job.job_id, host)
            return
        # C4: an interactive Cloudflare wall is the ONE thing the host solver can clear, and it must
        # be tried BEFORE the captcha_flagged bail below. C3 CamoFox flags an interactive challenge
        # as a captcha limitation (captcha_flagged=True), and that early-return used to swallow the
        # page before C4 ever ran — measured: a zoopla seed reached C4 and was solved, but all 50 of
        # its children were captcha-flagged by C3 and stopped here, so the crawl "continued" but
        # every child dead-ended and only page 1 was kept. On success the recovered page + its links
        # flow into the normal store/enqueue path so the crawl carries on into the cleared site.
        if hostsolver.enabled():
            wall = self._unclearable_wall(item)
            if wall and wall[0] == "cloudflare_interactive":
                try:
                    if await self._solve_on_host(item, wall[1]):
                        return
                except Exception:
                    log.debug("C4 host solve failed for %s", item.url, exc_info=True)
        if item.captcha_flagged:
            return                     # already recorded as a captcha limitation — don't re-tag netpath
        self._tag_netpath(item, status)

    def _flag_captcha_limitation(self, item: WorkItem, captcha_info) -> None:
        """A captcha/anti-bot wall we could not clear → flag it as a captcha limitation (distinct
        from a generic IP block), record it in crawl_history, and append the URL to the durable
        captcha_limitations.jsonl log the operator watches. Reason is CAPTCHA_FAILED when a solver
        was actually tried, else NEEDS_CAPTCHA_SOLVER (no solver available for this wall type)."""
        reason = (errors.CAPTCHA_FAILED if captcha_info.solver
                  else errors.NEEDS_CAPTCHA_SOLVER)
        item.ctx.bump_reason(reason)
        try:
            self.history.record_failure(
                item.url, status=None, category=reason, failed_at=_now_iso(),
                detail=f"{captcha_info.captcha_type}/{captcha_info.solvability}"
                       f": {captcha_info.error or 'not solved'}",
                crawl_run_id=item.ctx.crawl_run_id)
        except Exception:
            log.exception("could not record captcha limitation for %s", item.url)
        log.info("captcha wall url=%s type=%s solvability=%s err=%s",
                 item.url, captcha_info.captcha_type, captcha_info.solvability, captcha_info.error)
        _log_captcha_limitation(item.url, captcha_info, item.ctx.crawl_run_id)
        item.captcha_flagged = True    # so _run_c3_ladder / _tag_netpath don't overwrite the reason

    def _tag_netpath(self, item: WorkItem, status) -> None:
        """A block even C3 couldn't clear → persist needs_network_path (queryable for the final pass /
        next run) + count it. Never a URL list in memory — just an int and the sqlite row.

        A GONE status is NOT a block. 404/410 means the page does not exist, and no proxy, VPN or
        residential egress will conjure it — measured: 2,024 of 4,111 needs_network_path rows (49%)
        were plain 404s, so the queue of "sites we cannot reach" was double its real size and the
        top of it was pages that were never there.
        """
        if status and errors.policy_for_status(status) == errors.GONE:
            reason = errors.http_reason(status)
            if self.history is not None:
                self.history.record_failure(item.url, status=status, category=reason,
                                            failed_at=_now_iso(),
                                            crawl_run_id=item.ctx.crawl_run_id)
            item.ctx.bump_error(reason)
            return

        # A wall that provably cannot clear is NOT a network-path problem, and saying it is sends
        # the operator to buy a residential proxy that cannot help.
        #
        # needs_network_path asserts "the IP is the blocker, not the browser". For a Cloudflare
        # INTERACTIVE challenge that is measurably false: zoopla.co.uk returned 469,746 chars of
        # real listings from this very datacenter IP once the challenge was solved with a
        # coordinate click — the egress was never the problem, and no proxy changes the outcome.
        # Cloudflare labels the tier itself in window._cf_chl_opt.cType, so the answer is in the
        # body we already have.
        # A C4 host-solve is attempted at the ASYNC give-up point in _run_c3_ladder (this method is
        # sync and cannot await). By the time an interactive wall reaches _tag_netpath, that attempt
        # has already run or the solver is not configured — so here we only classify and record.
        wall = self._unclearable_wall(item)
        if wall:
            kind, remedy = wall
            if self.history is not None:
                self.history.record_failure(item.url, status=status, category=kind,
                                            failed_at=_now_iso(), detail=remedy,
                                            crawl_run_id=item.ctx.crawl_run_id)
            item.ctx.bump_error(kind)
            try:
                issues.log().record(item.url, kind, action=edgecase.ACT_SKIP, evidence=remedy,
                                    job_id=getattr(item.ctx.job, "job_id", "") or "",
                                    attempts=max(1, item.render_attempts), status=status)
            except Exception:
                log.debug("issue log write failed for %s", item.url, exc_info=True)
            log.info("unclearable wall job=%s url=%s kind=%s", item.ctx.job.job_id, item.url, kind)
            return

        if self.history is not None:
            self.history.record_failure(item.url, status=status,
                                        category=errors.NEEDS_NETWORK_PATH, failed_at=_now_iso(),
                                        crawl_run_id=item.ctx.crawl_run_id)
        item.ctx.bump_error(errors.NEEDS_NETWORK_PATH)
        self._netpath_tagged += 1
        # Everything that reaches here is unresolved after the whole ladder, so it belongs in the
        # operator's issues list — not only in a counter.
        try:
            issues.log().record(item.url, errors.NEEDS_NETWORK_PATH,
                                action=edgecase.ACT_SKIP,
                                evidence=errors.reason_detail(errors.NEEDS_NETWORK_PATH, status),
                                job_id=getattr(item.ctx.job, "job_id", "") or "",
                                attempts=max(1, item.render_attempts), status=status)
        except Exception:
            log.debug("issue log write failed for %s", item.url, exc_info=True)
        log.info("needs_network_path job=%s url=%s status=%s", item.ctx.job.job_id, item.url, status)

    async def _solve_on_host(self, item: WorkItem, remedy: str) -> bool:
        """C4: fetch a Cloudflare-interactive page through the host solver, then run the recovered
        HTML through the SAME store + link-enqueue path C3 uses — so the crawl doesn't just rescue
        this one URL, it keeps discovering and fetching the rest of the (now-cleared) site.

        Returns True if the page was recovered and handled, False to fall through to the wall log."""
        ctx = item.ctx
        try:
            data = await asyncio.to_thread(hostsolver.solve, item.url)
        except Exception:
            log.debug("host solver call failed for %s", item.url, exc_info=True)
            data = None
        if not data:
            return False
        # Mark the host as needing C4 BEFORE enqueuing its children. build_document below awaits, and
        # a worker can pull a just-enqueued child during that await — if the host isn't marked yet,
        # the child misses the proactive-C4 fast path and takes the slow ladder. Marking first closes
        # that window.
        host = (urlsplit(item.url).hostname or "").lower()
        if host and hasattr(self, "host_needs_c4"):
            self.host_needs_c4.add(host)
        html = data.get("html") or ""
        final_url = item.url
        fr = FetchResult(url=item.url, final_url=final_url, status=200, content_type="text/html",
                         kind="html", text_html=html, tier=1, fetched_at=_now_iso(),
                         from_fixture=True, captcha_type="cloudflare_interactive",
                         captcha_solved=True, captcha_solver="host_cdp_solve_captcha",
                         captcha_solver_cost_usd=0.0)
        # Discovery first: enqueue child links so the whole cleared site gets crawled into the
        # max_pages budget, not just the page we solved. Same call C3 makes.
        try:
            self._enqueue_links(ctx, fr, item.url, item.depth)
        except Exception:
            log.debug("host-solve enqueue failed for %s", item.url, exc_info=True)

        pdf_links, image_candidates, media_candidates = _capture_assets(
            ctx.job, fr.text_html, final_url)
        hp = HarvestedPage(url=item.url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates,
                           parent_url=item.parent_url, served_by="c4_host_solver",
                           served_from=final_url)
        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return True                          # still handled — we cleared it; just no main text
        doc.captcha_type = fr.captcha_type
        doc.captcha_solved = fr.captcha_solved
        doc.captcha_solver = fr.captcha_solver
        doc.captcha_solver_cost_usd = fr.captcha_solver_cost_usd
        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
        ctx.bump_reason(g.reason)
        ctx.bump_recovered(errors.SERVED_BY_HOST_SOLVER)
        if not g.keep:
            ctx.dropped_by_gate += 1
            _tick_stat('dropped_by_gate')
            return True
        await self._finalize(ctx, doc, hp, fr, item.url)   # resolves the issue-log entry too
        log.info("C4 host-solver recovered %s (via=%s solved=%s %s chars)",
                 item.url, data.get("via"), data.get("solved"), data.get("chars"))
        return True

    def _unclearable_wall(self, item: WorkItem) -> tuple[str, str] | None:
        """(kind, remedy) when the last body shows a wall no amount of retrying or re-routing can
        clear, else None. Keyed on Cloudflare's own cType label rather than on our guess."""
        body = getattr(item, "last_body", "") or ""
        if not body:
            return None
        try:
            det = captcha.detect_http(None, {}, body)
        except Exception:
            return None
        if det.kind == "cloudflare_interactive":
            return ("cloudflare_interactive",
                    "Cloudflare INTERACTIVE challenge (cType=interactive): it never self-clears and "
                    "injects no Turnstile widget, so no render, retry or proxy can pass it — the IP "
                    "is NOT the blocker (measured: this page returns 469K chars from our own IP once "
                    "solved). Use scripts/solve_challenge.py <url> on the host (SeleniumBase CDP "
                    "solve_captcha), ~28s per URL.")
        return None

    async def _netpath_final_pass(self, ctxs) -> None:
        """After the frontier + c3_queue drain, retry the needs-network-path backlog through a
        US/residential egress — CRAWLER_C3_PROXY_RETRY rotates a fresh IP inside _try_camofox_fallback.
        URLs stream lazily from sqlite (a 100k backlog never materializes). No egress wired → log the
        honest count and leave the rows in crawl_pages for a future run."""
        if os.environ.get("CRAWLER_NETPATH_FINAL", "0") != "1" or self._netpath_tagged == 0:
            return
        if self._c3_proxy_retry_budget() == 0:
            log.warning("netpath: %d URLs need a residential/US egress; left in crawl_pages for a "
                        "future run (set CRAWLER_C3_PROXY_RETRY=1 + a proxy)", self._netpath_tagged)
            return
        by_host: dict[str, "JobCtx"] = {}
        for c in ctxs:
            for d in getattr(c, "seed_domains", []) or []:
                by_host.setdefault((urlsplit(d).hostname or "").lower(), c)
        limit = _env_int("CRAWLER_NETPATH_MAX", 5000)
        log.info("netpath final pass: retrying up to %d URLs via residential/US egress", limit)

        async def _runner():
            for url in self.history.iter_needs_network_path(self._run_started_iso, limit):
                if stop_requested():
                    break
                h = (urlsplit(url).hostname or "").lower()
                ctx = by_host.get(h)
                if ctx is None or not ctx.reserve():
                    continue
                await self._run_c3_ladder(WorkItem(url, 0, ctx), h)
                self._mark_progress()
        try:
            await asyncio.wait_for(_runner(), timeout=_env_int("CRAWLER_NETPATH_DRAIN_S", 600))
        except asyncio.TimeoutError:
            log.warning("netpath final pass timed out (%ss) — remaining URLs left for next run",
                        _env_int("CRAWLER_NETPATH_DRAIN_S", 600))

    def _job_is_producing_nothing(self, ctx: "JobCtx") -> bool:
        """Stop enqueueing for a job whose fetches are almost all duplicates.

        Every trap guard before this one matches a URL SHAPE, and shapes are endless. cyientdlm.com
        proved it twice in one day: the first shape was /about-us/about-us/capabilities/… (fixed by
        the repeated-segment rule), and the very next run produced
        /com/cyient/www/prlisting/about-us/industries/… — the site's own host written backwards
        into the path, nine segments deep, with only ONE segment repeating, so the new rule missed
        it too. That crawl had fetched 17,265 pages and stored 48.

        This guard reads the OUTCOME instead, which no new shape can dodge: if a job has fetched a
        meaningful number of pages and the overwhelming majority hashed to something already
        stored, the site is serving one page under an unbounded set of URLs and there is nothing
        left to find. Stop growing the frontier; what is already queued still drains, so a genuine
        late-arriving page is not lost.

        Per JOB rather than per host because these jobs are one seed domain each
        (same_domain_only), which makes the two the same thing here.
        ponytail: if a job ever carries many hosts, key this on the host instead."""
        floor = _env_int("CRAWLER_DUP_BREAKER_MIN", 300)
        if floor <= 0 or ctx.fetched < floor:
            return False
        ratio = ctx.skipped_duplicate / max(ctx.fetched, 1)
        if ratio < _env_float("CRAWLER_DUP_BREAKER_RATIO", 0.9):
            return False
        if not getattr(ctx, "_dup_breaker_logged", False):
            ctx._dup_breaker_logged = True
            log.warning("job %s: %d of %d fetches were duplicates (%.0f%%) — the site is serving "
                        "one page under unbounded URLs; not enqueueing more",
                        ctx.job.job_id, ctx.skipped_duplicate, ctx.fetched, 100 * ratio)
        return True

    def _enqueue_candidate(self, ctx: "JobCtx", cl: str, parent_url: str, depth: int) -> bool:
        """Apply the seen/domain/trap/query-variant guards to ONE already-canonicalized URL and
        enqueue it at depth+1. Returns True if enqueued. Shared by link-enqueue and click-discovery.
        Loop-thread-only; the caller wraps this so one bad candidate can't abort the rest."""
        # DEPTH-MIN RELAXATION, before the seen-check that would otherwise discard this sighting.
        #
        # `depth` is supposed to mean "hops from the seed". Under the LIFO (DFS) frontier the first
        # sighting is instead the depth of a descent — a fact about the walk, not the site — and the
        # seen-check below makes it permanent. MEASURED on al-monitor.com: 81,297 pages over 1,820
        # distinct depths, max 1,826, busiest level d1766 with 2,525 pages, all real articles reached
        # article -> related-article -> article. That site's own /archives index sits at d1 and every
        # monthly page under it at d2, so those articles are three hops from the seed.
        #
        # Every consumer of depth was being lied to: max_depth, the incremental fresh-depth surface
        # (a delta crawl treated the whole site as below it), resume's shallowest-first ordering.
        # Taking the MINIMUM over all sightings restores the meaning without touching traversal
        # order, so the DFS rationale above (articles before the mega-menu drains) still holds.
        #
        # Relaxation needs the shallow hub to actually be fetched and parsed to supply the shorter
        # path — see the convergence check in tests/test_depth_is_min_hops.py.
        nd = depth if _looks_paginated(cl) else depth + 1
        if cl in ctx.seen:
            if nd < ctx.depth_at.get(cl, nd):
                ctx.depth_at[cl] = nd
                self._frontier_relax(ctx, cl, nd)
            return False
        if self._job_is_producing_nothing(ctx):
            ctx.trap_skipped += 1
            return False
        if ctx.job.same_domain_only and not _allowed_offsite(cl, parent_url, ctx.seed_domains):
            return False
        if not _in_path_scope(cl, ctx.job.path_scope):
            ctx.scope_skipped += 1
            return False
        # Non-content action links (login/logout/vote, ?goto= redirects) store nothing and, on a
        # rate-limiting host, spend the request budget the real pages need — the flood of them is
        # what buried the pagination link on Hacker News. Skip cheaply, before the network ever runs.
        if _is_action_link(cl):
            ctx.scope_skipped += 1
            return False
        # OPERATOR BLOCKLIST. A site the operator deleted from the corpus must not walk back in as
        # somebody else's link. nordkurier.de and deki.dk were each purged from Postgres, MinIO, the
        # schedules and all twelve frontiers, and both returned — they are in no catalogue and no job,
        # so they arrive purely as children of a catalogued page or a cross-domain redirect. Without a
        # check HERE, on the enqueue path, every purge is temporary.
        blk = blocklist.blocked(cl)
        if blk:
            ctx.scope_skipped += 1
            return False
        # SSRF guard. A crawler follows links written by the pages it reads, so any page can steer
        # it — the prize target being http://169.254.169.254/latest/meta-data/iam/security-
        # credentials/, which returns live cloud credentials as JSON that we would then store as a
        # document. same_domain_only masks this for most jobs but is scope policy, not a security
        # boundary, and jobs do run with it off. Checked before anything else touches the URL.
        priv = edgecase.private_target(cl)
        if priv:
            log.warning("blocked internal-address link %s from %s (%s)", cl, parent_url, priv)
            ctx.trap_skipped += 1
            return False
        # Trap guards: URL-shape (loop/length) + calendar/facet query explosion.
        if errors.looks_like_trap(cl):
            ctx.trap_skipped += 1
            return False
        # Path-form calendar trap. The query-variant cap below keys on host+path, so a calendar
        # that varies a QUERY parameter (/events/?date=2035-01) is capped after N variants — but
        # one that varies the PATH (/events/2035-01/) produces a NEW key on every hop, so the cap
        # never fires, and looks_like_trap sees two short segments with nothing repeated. The
        # frontier then walks "next month" forever and the job never drains.
        if edgecase.calendar_trap(cl):
            ctx.trap_skipped += 1
            return False
        paginated = _looks_paginated(cl)
        qp = urlsplit(cl)
        if qp.query:
            # Pagination gets its OWN budget, separate from the facet-explosion cap. Both key on
            # host+path, so page 2..N of one listing used to consume the 20-variant facet budget and
            # the chain died at page 20. A facet explosion is combinatorial (?color=red&size=xl);
            # pagination is a single linear chain, so it is safe to allow far more of it.
            if paginated:
                key, cap = f"pag:{qp.hostname}{qp.path}", _env_int("CRAWLER_MAX_PAGINATION", 2000)
            else:
                key, cap = f"{qp.hostname}{qp.path}", _env_int("CRAWLER_MAX_QUERY_VARIANTS", 20)
            n = ctx.query_variants.get(key, 0) + 1
            if n > cap:
                ctx.trap_skipped += 1
                return False
            ctx.query_variants[key] = n
            # PER-HOST TOTAL, on top of the per-path cap above. The per-path cap (20 facet variants,
            # 2000 pagination) is right and stays — what it cannot see is a site with THOUSANDS of
            # paths each taking its full allowance. shvabe.com hangs `?PAGEN_1=N` on every news
            # article: ~2,588 article paths x ~18 variants = 42,010 pending URLs, none of which
            # trips the per-path cap because none of them exceeds 20 on its own.
            #
            # Bound from the same measurement as the deep-path cap: the largest genuine user across
            # all twelve frontiers is gdbiw.com at 6,668 query-variant URLs, then wartsila at 2,121
            # and everything else under 700. 10,000 leaves the real ones untouched with half again
            # of headroom and still removes more than half of shvabe's.
            hq = f"q:{qp.hostname}"
            hn = ctx.host_queries.get(hq, 0) + 1
            if hn > _env_int("CRAWLER_HOST_QUERY_CAP", 10000):
                ctx.trap_skipped += 1
                return False
            ctx.host_queries[hq] = hn
        # MENU ACCUMULATION WITH NO REPEATED SEGMENT — the one shape every stateless rule misses.
        #
        # A site that serves its whole nav bar as relative links grows a path per hop:
        #   /press  ->  /press/events  ->  /press/events/about  ->  /press/events/about/partners
        # Every segment is DISTINCT, so the 3x-loop rule, the two-segments-repeating rule and the
        # depth+repeat rule all pass it, and it stays under the 12-segment cap. Measured on
        # rusbitech.ru: 24,436 pending URLs, 21,251 of them six or more segments deep, against
        # THIRTY-FOUR pages actually stored. It also cannot be caught by URL shape at all, because
        # each individual URL looks perfectly ordinary — only the volume gives it away.
        #
        # VOLUME AT DEPTH is the discriminator: a real site is a pyramid (most pages shallow, few
        # deep), an accumulating menu inverts that and piles up at one deep level. Scored across all
        # twelve live frontiers before choosing the bound — the largest genuine site measured
        # (safran-group.com, 47,574 pages stored) holds 2,002 URLs at depth >= 6, while the two known
        # traps hold 21,251 and 8,101. The cap is 5,000: 2.5x above the biggest real site and well
        # under either trap.
        #
        # Only DEEP urls are counted, so a big site's shallow pages — which is where its content
        # actually lives — are never affected however many of them there are.
        deep_from = _env_int("CRAWLER_HOST_DEEP_DEPTH", 6)
        if len([s for s in qp.path.split("/") if s]) >= deep_from:
            dkey = f"deep:{qp.hostname}"
            dn = ctx.deep_paths.get(dkey, 0) + 1
            if dn > _env_int("CRAWLER_HOST_DEEP_CAP", 5000):
                ctx.trap_skipped += 1
                return False
            ctx.deep_paths[dkey] = dn
        # Pagination is a CONTINUATION of the same listing, not a level deeper: page 2 of a result
        # set is the same depth as page 1. Counting it as depth+1 is what capped a paginated crawl at
        # max_depth PAGES — measured on pixabay (?pagi=), the chain died at page 3 under max_depth=2
        # while the operator expected the full 14K-page listing. Same-depth keeps the chain alive; it
        # stays bounded by max_pages, ctx.seen, the pagination cap above and the deep-page trap.
        child_depth = depth if paginated else depth + 1
        ctx.seen.add(cl)
        ctx.depth_at[cl] = child_depth
        self.frontier.put_nowait(WorkItem(cl, child_depth, ctx, parent_url=parent_url))
        self._frontier_track(ctx, cl, child_depth, parent_url, False)
        return True

    def _enqueue_links(self, ctx: "JobCtx", fr: FetchResult, url: str, depth: int) -> None:
        """Extract child links from a fetched page and enqueue them within the depth/domain
        budget (loop thread; parse is cheap CPU). Shared by the live C1 path AND the C3 CamoFox
        fallback — WITHOUT this a site served entirely by C3 would dead-end at the seed page and
        never reach its max_pages budget, since only the frontier drives the crawl.

        Per-link resilient: canonicalize_url/urlsplit can raise on ONE malformed href (bad port,
        bad IPv6); we skip that link, NOT the whole page (which would drain the frontier → shallow
        crawl). canonicalize_url is now defensive too, so this is belt-and-suspenders."""
        base = fr.final_url or url
        # A SEED that redirects cross-domain legitimately moves the crawl to the host it landed on
        # (airbots.co.in 301s to airbotsaerospace.com — without this the crawl stops at one page).
        # A redirect on a DEEPER page does not: scope is anchored to seed_domains, so widening it
        # here is the only way a new host enters scope, and depth 0 is the only place that is the
        # operator's intent. Letting any depth widen it is what pulled 105 Google/LinkedIn hosts
        # into an rtx.com crawl.
        if depth == 0 and not same_site(base, url):
            # …unless it landed on a domain broker. An expired seed does not 404, it 301s to
            # whoever now sells the name, and that marketplace is a real site with thousands of
            # crawlable pages — so "the seed moved here" widens scope to the entire broker.
            # Measured: acmat.com (expired, from the catalogue) redirected to hugedomains.com and
            # the crawl stored 70 pages of for-sale listings for unrelated domains, 17% of
            # everything captured in that run, before the process died.
            # The landing page itself is still fetched and stored — one page saying the domain is
            # for sale is the evidence that the source is dead, and worth keeping.
            if is_parking_host(base):
                log.info("scope %s redirects to parking host %s — not widening scope", url, base)
            else:
                ctx.seed_domains.add(canonicalize_url(base))
        # The RFC 8288 pager header comes FIRST, ahead of the is_html() gate below. api.github.com
        # and every wp-json endpoint answer with a JSON array whose only next-page pointer is
        # `Link: <...>; rel="next"` — is_html() is False for those, so the gate returned before a
        # single link was ever considered and the API paged exactly once, forever.
        # getattr: fixtures and the render path build FetchResult-shaped stand-ins that predate
        # this field, and a missing pager header must never take out link extraction.
        link_hdr = getattr(fr, "link_header", None)
        if link_hdr and _depth_ok(depth, ctx.job.max_depth) and ctx.has_budget():
            for link in parse.links_from_link_header(link_hdr, base):
                try:
                    self._enqueue_candidate(ctx, canonicalize_url(link), base, depth)
                except Exception as exc:
                    log.debug("enqueue skip bad Link-header target %r on %s: %s", link, url, exc)
        # A JSON API answers with kind="other", so the is_html() gate below returned before a
        # single link was considered and every /api/ listing paged exactly once. Its pager is in
        # the BODY (crates.io meta.next_page, DataCite links.next, OpenAlex meta.next_cursor, or
        # ?page=/?offset= arithmetic) — links_from_json reads it, and returns nothing for a body
        # that is not a JSON listing, so this cannot widen the frontier on anything else.
        if (not fr.is_html() and fr.text_html and _depth_ok(depth, ctx.job.max_depth)
                and ctx.has_budget()):
            for link in parse.links_from_json(fr.text_html, base):
                try:
                    self._enqueue_candidate(ctx, canonicalize_url(link), base, depth)
                except Exception as exc:
                    log.debug("enqueue skip bad JSON pager %r on %s: %s", link, url, exc)
        if not (fr.is_html() and fr.text_html and _depth_ok(depth, ctx.job.max_depth) and ctx.has_budget()):
            return
        try:
            # job.link_relevance_keywords: follow only links whose ANCHOR TEXT mentions a keyword.
            # This existed in harvest.py but was never ported here, so it silently did nothing on
            # the async (production) engine — which is how a /about/defence seed on a newspaper
            # walked into its entertainment archive. Empty list = follow everything (default).
            if ctx.job.link_relevance_keywords:
                # Pagination is EXEMPT from the anchor-text filter. "2" / "Next" / "»" carries no
                # keyword, so a relevance-filtered crawl of a listing site used to stop dead at
                # page 1 — and on a tender portal every relevant item after the first 20 lives
                # behind that chain. The filter's job is to skip irrelevant CONTENT links, not to
                # sever the navigation that reaches the relevant ones. Shape-based (_looks_paginated),
                # so it needs no anchor text and cannot be spoofed by link text.
                pairs = parse.extract_links_with_text(fr.text_html, base)
                links = [u for u, t in pairs
                         if _link_is_relevant(t, ctx.job.link_relevance_keywords)
                         or _looks_paginated(u)]
                # A FILTER THAT MATCHES NOTHING IS A DEAD END, NOT A FOCUSED CRAWL.
                #
                # The gate reads ANCHOR TEXT, and a portal whose navigation is not in the
                # keyword list's language matches zero links — so the crawl stops at the seed
                # having fetched one page. Measured 2026-08-18 on eeslindia.org, whose nav
                # anchors are Hindi and generic English ("हमारे बारे में", "Home"): 1 page for
                # us against 10 each for Firecrawl and Scrapling, on a site that was neither
                # blocking nor empty. Same shape on gscl.assam.gov.in and vergabe.berlin.de.
                #
                # So when the filter empties the page, fall back to following it unfiltered.
                # This cannot cause the runaway the filter exists to prevent: it only applies
                # to a page that yielded NO match, the ordinary page budget and depth cap still
                # bound it, and a page whose links all matched is untouched. The alternative —
                # keeping a site the crawler can reach but will not walk — loses the source
                # outright, which is the more expensive failure.
                # SEED ONLY. The first version of this applied at every depth and made the
                # crawl worse while making it bigger: on mahatenders.gov.in the depth-1 chrome
                # (Awards, Debarment List, Cancelled Tenders, Standard Bidding Documents)
                # matches nothing, so the fallback fired there too and flooded the frontier
                # with its links. Measured 2026-08-18 — the 10-page crawl walked exactly that
                # chrome and never reached FrontEndListTendersbyDate, which the 3-page
                # filtered crawl had reached; the e-bus tender yield went from 1 to 0 on both
                # Delhi and Maharashtra. A page that matches nothing at depth >= 1 is chrome,
                # and expanding it is the newspaper-archive failure this gate exists to stop.
                if not links and pairs and depth == 0:
                    links = [u for u, _ in pairs]
                    log.info("follow-gate matched nothing on the seed %s — following %d "
                             "link(s) unfiltered rather than dead-ending", base, len(links))
            else:
                links = parse.extract_links(fr.text_html, base)
        except Exception as exc:
            log.debug("extract_links failed base=%s: %s", base, exc)
            return
        # <link rel="next"> lives in <head> and is not an <a>, so extract_links structurally cannot
        # see it — and on gutenberg.org that <head> link is the only next-page pointer there is.
        # Appended rather than merged into extract_links so _needs_render's "<3 links = JS shell"
        # test is unaffected.
        try:
            for u in parse.extract_rel_links(fr.text_html, base):
                if u not in links:
                    links.append(u)
        except Exception as exc:
            log.debug("extract_rel_links failed base=%s: %s", base, exc)
        # Front-load pagination: the "next page" link is usually the LAST link in a listing, so a
        # rate-limited or max_pages-capped crawl spends its slots on the nav/story links above it and
        # never follows the chain (HN: every stored page was top-nav, news?p=4 was 429'd at the back
        # of the burst). Stable sort → non-pagination order preserved. Ordering only: nothing is
        # added or dropped here.
        links.sort(key=lambda u: 0 if _looks_paginated(u) else 1)
        for link in links:
            try:
                # `base`, not `url`: the parent is the page these links were actually found on,
                # which after a redirect is a DIFFERENT host. Passing the pre-redirect URL made
                # same_domain_only compare every child against a host we never read, so under
                # CRAWLER_STRICT_DOMAIN (the default) a site that redirects across domains dropped
                # 100% of its links and stopped at exactly one page — measured on airbots.co.in,
                # which 301s to airbotsaerospace.com and has 32 followable links.
                self._enqueue_candidate(ctx, canonicalize_url(link), base, depth)
            except Exception as exc:
                log.debug("enqueue skip bad link %r on %s: %s", link, url, exc)
                continue

    async def _click_discover(self, ctx: "JobCtx", tab, origin_url: str, depth: int) -> int:
        """Click every NON-<a href> clickable (button/onclick/role/data-href/cursor:pointer) to
        surface JS/SPA navigation that extract_links can't see, enqueue same-domain discoveries at
        depth+1, and restore the tab to origin_url. Returns count enqueued. All awaits are on `tab`
        → stays on the loop thread. Bounded by CRAWLER_MAX_CLICKS_PER_PAGE so a busy page can't spin.
        Hazards handled: popups/downloads (opener-scoped auto-close/cancel), off-domain nav (skip +
        back), modal/no-nav (skip), any per-click error (logged, never fatal)."""
        import asyncio as _asyncio
        cap = _env_int("CRAWLER_MAX_CLICKS_PER_PAGE", 40)
        nav_wait = _env_int("CRAWLER_CLICK_NAV_WAIT_MS", 1500)
        click_to = _env_int("CRAWLER_CLICK_TIMEOUT_MS", 4000)
        enqueued = 0
        clicked: set[str] = set()

        def _close_popup(p):
            _asyncio.ensure_future(_swallow(p.close()))

        def _cancel_dl(d):
            _asyncio.ensure_future(_swallow(d.cancel()))

        # A "Load more" BUTTON creates no URL. It fires fetch('/ajax/products?offset=20') and
        # appends the HTML, so tab.url never changes and the check below sees nothing — which is
        # why a button-driven pager was invisible to the crawler at every tier. The request itself
        # is the missing URL, and Playwright hands it to us for free. Only xhr/fetch, only
        # same-site, and only if it already looks like a pager, so this cannot turn into a firehose
        # of analytics beacons.
        xhr_pagers: list[str] = []

        def _note_request(req):
            try:
                if req.resource_type not in ("xhr", "fetch"):
                    return
                u = req.url
                if _looks_paginated(u) and same_site(u, origin_url) and u not in xhr_pagers:
                    xhr_pagers.append(u)
            except Exception:
                pass

        tab.on("popup", _close_popup)
        tab.on("download", _cancel_dl)
        tab.on("request", _note_request)
        try:
            for _ in range(cap):
                if not ctx.has_budget():
                    break
                # Reset to a stable base if a prior click navigated us away (stale DOM otherwise).
                if tab.url != origin_url:
                    try:
                        await tab.goto(origin_url, timeout=click_to * 3, wait_until="domcontentloaded")
                    except Exception:
                        break
                try:
                    cands = await tab.evaluate(_CANDIDATE_JS, cap * 3)
                except Exception:
                    break
                pick = next((c for c in cands if c.get("sig") not in clicked), None)
                if pick is None:
                    break
                clicked.add(pick.get("sig"))
                try:
                    await tab.locator(f'[data-__cd="{pick["idx"]}"]').first.click(
                        timeout=click_to, no_wait_after=True)
                    await tab.wait_for_timeout(nav_wait)
                except Exception as exc:
                    log.debug("click_discover click failed on %s: %s", origin_url, exc)
                    continue
                self._mark_progress()               # long discovery must not look idle
                new = tab.url
                if new and new != origin_url:
                    if same_site(new, origin_url):
                        cl = canonicalize_url(new)
                        try:
                            if self._enqueue_candidate(ctx, cl, origin_url, depth):
                                enqueued += 1
                        except Exception:
                            pass
                    # Return to origin to keep clicking (go_back, else cold goto).
                    try:
                        await tab.go_back(timeout=click_to * 2, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    if tab.url != origin_url:
                        try:
                            await tab.goto(origin_url, timeout=click_to * 3, wait_until="domcontentloaded")
                        except Exception:
                            break
        finally:
            try:
                tab.remove_listener("popup", _close_popup)
                tab.remove_listener("download", _cancel_dl)
                tab.remove_listener("request", _note_request)
            except Exception:
                pass
        # Enqueue the AJAX pager URLs the clicks revealed. Done here rather than inline so one
        # burst of identical requests cannot enqueue the same URL forty times.
        for u in xhr_pagers:
            try:
                if self._enqueue_candidate(ctx, canonicalize_url(u), origin_url, depth):
                    enqueued += 1
                    log.debug("click_discover captured AJAX pager %s from %s", u, origin_url)
            except Exception:
                continue
        return enqueued

    def _render_host_at_cap(self, url: str) -> bool:
        """Is this URL's host already holding every render permit it is allowed?"""
        try:
            h = (urlsplit(url).hostname or "").lower()
        except (ValueError, UnicodeError):
            return False                      # unparseable: let _render_permit decide
        # getattr, not attribute access: this runs on the worker hot path, and an engine built
        # without full __init__ (fixtures, the C3-only path) must not raise here — an
        # AttributeError inside _next_item kills the worker task and takes its lane slot with it.
        return getattr(self, "_render_hosts", {}).get(h, 0) >= _env_int("CRAWLER_HOST_RENDER_MAX", 8)

    def _lane_admit_max(self, host: str) -> int:
        """How many of ONE host's URLs may sit in render_q at once.

        Not politeness - that is CRAWLER_HOST_RENDER_MAX, applied at permit time. This bounds the
        POLLING: every queued entry is re-examined by _pull_render's scan and by the blocking
        waiter, so a host with tens of thousands of entries costs thousands of pointless wake-ups
        per second to service the two or three renders it is actually allowed. A small multiple of
        the render cap keeps the lane deep enough that no permit ever idles waiting for a refill,
        and shallow enough that the scan finds workable hosts instead of rotating one site.
        """
        cap = _env_int("CRAWLER_HOST_RENDER_MAX", 8)
        return _env_int("CRAWLER_HOST_LANE_MAX", 0) or max(8, 2 * cap)

    def _lane_put(self, item) -> None:
        """THE ONLY DOOR INTO render_q. Over the admission cap, the item is held instead.

        Held items are not lost and not deferred to a timer: they are released by _release_held the
        moment the host gives a render permit back, so the lane refills from work that can actually
        run. _drain's empty-check counts them, or a batch would declare itself finished with
        thousands of pages parked and mark them done in the durable frontier - pages that were
        never fetched and that a resume would then skip.
        """
        h = _lane_host(item)
        if getattr(self, "_lane_hosts", None) is None:      # engine without a full __init__
            self.render_q.put_nowait(item)
            return
        if h and self._lane_hosts.get(h, 0) >= self._lane_admit_max(h):
            self._host_hold.setdefault(h, []).append(item)
            self._held_items += 1
            return
        if h:
            self._lane_hosts[h] = self._lane_hosts.get(h, 0) + 1
        self.render_q.put_nowait(item)

    def _lane_release(self, item) -> None:
        """An item has left render_q for good (taken, or moved to the hold)."""
        h = _lane_host(item)
        if not h or getattr(self, "_lane_hosts", None) is None:
            return
        n = self._lane_hosts.get(h, 1) - 1
        if n > 0:
            self._lane_hosts[h] = n
        else:
            self._lane_hosts.pop(h, None)

    def _release_held(self, host: str) -> None:
        """Refill the lane for *host* now that it has room. Event-driven: called when a render
        permit is returned, which is the only moment the answer can have changed.

        getattr, not attribute access — the same rule as _render_host_at_cap. This is reached from
        _render_permit's finally, which runs on engines built without a full __init__ (fixtures,
        the C3-only path); an AttributeError there escapes through the context manager and kills
        the worker task holding the permit.
        """
        held = getattr(self, "_host_hold", {}).get(host)
        if not held:
            return
        room = self._lane_admit_max(host) - self._lane_hosts.get(host, 0)
        while room > 0 and held:
            item = held.pop()
            self._held_items -= 1
            self._lane_hosts[host] = self._lane_hosts.get(host, 0) + 1
            self.render_q.put_nowait(item)
            room -= 1
        if not held:
            self._host_hold.pop(host, None)

    def _release_held_all(self) -> None:
        """Safety net on the _drain tick. Every release above is edge-triggered, and an edge that
        fires while the hold is empty is lost - a host whose last permit came back before its
        surplus arrived would keep it parked forever. Bounded work: one pass over the held hosts.

        It also REPAIRS the lane counts, because they are the fragile part of this design: three
        call sites increment or decrement _lane_hosts, and one of them missing its decrement is
        invisible until the hold silently stops draining. An empty render_q is proof that every
        lane count should be zero, so say so rather than trusting the arithmetic. Without this a
        single missed decrement parks a host's work permanently.
        """
        if not self.render_q.qsize() and getattr(self, "_lane_hosts", None):
            self._lane_hosts.clear()
        for host in list(getattr(self, "_host_hold", {})):
            self._unpark_if_stalled(host)
            self._release_held(host)

    def _unpark_if_stalled(self, host: str) -> None:
        """A hold whose host never drains must not pin the engine for the life of the process.

        THE LEAK THIS CLOSES. An item in render_q is examined, ages against
        CRAWLER_HOST_DEFLECT_BUDGET_S and eventually gives up. An item in _host_hold is in NO
        queue: nothing examines it, nothing ages it, and no budget reaches it. So an engine whose
        remaining work is one jammed host can never satisfy _drain's `_held_items == 0`, and it
        cannot fall out through the idle watchdog either, because HostLimiter.slot deliberately
        heartbeats _last_progress on every cooldown wait so a merely slow host is not killed.

        Both doors shut, the coroutine lives forever on the shared engine loop. Measured on the
        fleet 2026-08-21: api-9 held THREE live engines thirteen hours in having taken no crawl
        request for an hour, api-8 two. Each keeps its own adaptive gate, so the render limit stops
        being global - three engines at render_limit=16 admit 48 concurrent renders on a lane sized
        for 16, at roughly 1.2 cores each. That is the cost, and it compounds per dispatch.

        Reuses the deflect path's clocks rather than inventing a second budget. host_busy_since is
        already host-keyed and already SELF-CLEARS the moment the host has queue room, so a reading
        older than the budget genuinely means still-jammed; host_last_ok is what separates a wedged
        host from a merely saturated one that is serving pages perfectly well.

        Past the budget the hold is handed to render_q with the admission cap ignored. It is not a
        refill: every one of those items is already over budget, so the deflect path gives up on
        each at first sight and the host drains in ONE pass - the behaviour the host-keyed budget
        was built for. They leave as host_busy, which buckets recoverable, not terminal.

        ponytail: the dump is unbounded, so a 5,000-item hold makes render_q briefly 5,000 deep.
        The scan is capped at CRAWLER_RENDER_SCAN and each entry leaves on its first look, so it is
        one pass; cap the dump only if a hold ever gets big enough for that pass to matter.
        """
        held = getattr(self, "_host_hold", {}).get(host)
        if not held:
            return
        # getattr, not attribute access: this is on the drain tick, which also runs on engines
        # built without a full __init__ (fixtures, the C3-only path). Raising here escapes into
        # _drain and kills the batch - the same rule as _render_host_at_cap.
        first = getattr(self, "host_busy_since", {}).get(host)
        if first is None:                      # no jam recorded; the ordinary cap still applies
            return
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        waited = now - first
        last_ok = getattr(self, "host_last_ok", {}).get(host)
        if last_ok is not None:
            # SATURATED IS NOT WEDGED. A host whose every page needs a browser sits at its queue
            # cap while working fine; measured once on yakovlev.ru, 184 give-ups on a site that
            # was serving us. A working host keeps resetting this by delivering.
            waited = min(waited, now - last_ok)
        if waited < _env_float("CRAWLER_HOST_DEFLECT_BUDGET_S", 180.0):
            return
        log.warning("hold stalled host=%s - unparking %d page(s) after %.0fs with no delivery; "
                    "they will be given up on as host_busy", host, len(held), waited)
        for item in held:
            self._lane_hosts[host] = self._lane_hosts.get(host, 0) + 1
            self.render_q.put_nowait(item)
        self._held_items -= len(held)
        self._host_hold.pop(host, None)

    def _pull_render(self):
        """Next render item whose HOST can actually render right now, or None.

        Taking render work blindly is what let ONE site own the whole lane. Measured: 128 of 128
        lane slots held by condornaoletal.com.br while its per-host cap allowed 24 renders, so 104
        workers each took a slot, hit RenderBusy in _render_permit, released the slot and requeued
        on a 3s timer — 385,296 deflections against 1,871 fetched pages. Every other host's render
        work sat behind that churn, and the lane read 100% busy while 52 of 64 browser permits were
        idle. Raising the per-host cap only moved the number; the lane still filled with one host.

        Checking at PULL time costs one dict lookup and keeps the lane serving hosts that can use
        it. Skipped items rotate to the back, so nothing is dropped and ordering degrades to
        round-robin under contention — which is the behaviour we wanted anyway.

        Bounded by CRAWLER_RENDER_SCAN: a queue that is entirely one capped host must degrade to
        "no render work for me right now" rather than rotating thousands of entries on every pull.
        """
        scan = min(self.render_q.qsize(), _env_int("CRAWLER_RENDER_SCAN", 64))
        rotated, got = [], None
        for _ in range(max(1, scan)):
            try:
                it = self.render_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not self._render_host_at_cap(getattr(it, "url", "")):
                got = it
                self._lane_release(it)
                break
            rotated.append(it)
        for it in rotated:                     # back of the queue, original order preserved
            self.render_q.put_nowait(it)
        return got

    @asynccontextmanager
    async def _render_permit(self, host: str):
        """Per-host share of the GLOBAL render pool, then a bounded wait for a permit.

        The adaptive gate is host-agnostic and is acquired before the per-host slot, so without a
        per-host cap one site's render-heavy frontier takes every permit and starves every other
        host. Both failure modes raise RenderBusy so the caller requeues instead of parking a tab."""
        cap = _env_int("CRAWLER_HOST_RENDER_MAX", 8)
        if self._render_hosts.get(host, 0) >= cap:
            raise RenderBusy(f"host {host} already holds {cap} render permits")
        self._render_hosts[host] = self._render_hosts.get(host, 0) + 1
        try:
            async with self.adaptive.slot(timeout=_env_float("CRAWLER_RENDER_WAIT_S", 8.0)):
                yield
        finally:
            n = self._render_hosts.get(host, 1) - 1
            if n > 0:
                self._render_hosts[host] = n
            else:
                self._render_hosts.pop(host, None)   # don't grow a dict entry per host forever
            # A permit came back, so this host has room it did not have a moment ago. This is
            # the wake-up that replaces the timer: the lane refills from the hold exactly when
            # the answer changed, instead of every queued item asking every 60 seconds.
            self._release_held(host)

    def _note_render(self, host: str, rendered: bool) -> None:
        """Track how often a host's pages need the browser. Once the first few pages are
        mostly renders (httpx keeps coming up empty), escalate the host to always-render so
        we stop paying the wasted httpx round-trip — this is the "site won't open without JS
        → switch to render_js always" behaviour, decided automatically and per-host."""
        if host in self.host_render_always:
            return
        st = self.host_render_stat.setdefault(host, [0, 0])       # [rendered, total]
        st[0] += 1 if rendered else 0
        st[1] += 1
        probe = _env_int("CRAWLER_AUTO_PROBE", 4)
        if st[1] >= probe and st[0] >= max(1, probe - 1):         # ≥3 of the first 4 needed JS
            self.host_render_always.add(host)
            hist = getattr(self, "history", None)
            if hist is not None:                                  # persist so next run skips the probe
                hist.set_render_host(host, True)
            log.info("render mode: host %s → always-render (%d/%d early pages needed JS)",
                     host, st[0], st[1])

    async def _impersonate_try(self, item: "WorkItem", url: str, host: str) -> "FetchResult | None":
        """Cheap curl_cffi real-Chrome-JA3 GET for a TLS-fingerprint-blocked page — clears many WAF
        403/401s without spinning a browser tab or C3. Runs under the host politeness slot. Returns
        good HTML (<400, not a 200 block page) or None. No-op when CRAWLER_IMPERSONATE_FETCH=0."""
        try:
            async with self.host.slot(url):
                fr = await asyncio.to_thread(item.ctx.fetcher._impersonate_fetch, url)
        except HostSlotBusy:
            # Re-raised past the broad handler below, which would otherwise turn a host being
            # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
            # worker requeues it; nothing about the page went wrong.
            raise
        except Exception:
            return None
        if (fr is not None and fr.is_html() and fr.text_html and fr.status and fr.status < 400
                and not errors.is_ip_block_page(fr.text_html)):
            log.info("impersonate_fetch cleared block job=%s url=%s status=%s",
                     item.ctx.job.job_id, url, fr.status)
            return fr
        return None

    async def _fetch_page(self, item: "WorkItem", tab, host: str, hold=None):
        """Fetch item.url per the render mode; returns (FetchResult, tab).
        auto  → httpx first, render only JS-dependent/blocked pages, escalate the host if it
                keeps needing the browser. never → httpx only. always → Playwright only."""
        ctx = item.ctx
        url = item.url
        mode = _render_mode(ctx.job)
        # A page we must screenshot needs a browser regardless, so in auto we render it INLINE
        # (one pass grabs html + screenshot) rather than httpx-fetching and then relaunching a
        # browser per page just for the shot. So the httpx fast path only applies when no
        # screenshot is wanted — drop "screenshot" from capture to crawl a whole site at httpx speed.
        wants_shot = "screenshot" in ctx.job.capture
        # Careful gov/mil hosts: skip the httpx probe entirely. A bot-UA httpx hit is what draws
        # Akamai connection-resets (a HARD_FAIL that trips the breaker) — render with a real
        # browser UA at concurrency-1 instead. So auto/never both become render for these hosts.
        # A per-host override host is a gentle-httpx host (weak/flaky no-bot server) — take the light
        # httpx path even if it's .gov/.mil, since render just gives a flaky box more sockets to drop.
        careful = (errors.is_careful_host(host) or host in self.host.force_careful) \
            and self.host.override_for(host) is None
        # A deferred page returns on the render lane with needs_render already set — httpx +
        # impersonate were tried on the first pass and failed, so re-running them here is pure
        # waste (audit F5). Skip straight to render. never mode keeps httpx: it has no browser.
        try_httpx = not careful and ((mode == "never") or (
            mode == "auto" and not wants_shot and not item.needs_render
            and host not in self.host_render_always))
        # A URL that plainly serves a FILE must never reach the browser. Chromium answers a download
        # with ERR_ABORTED, which looked like a crashed render — renault-trucks.com/en/media/1119/
        # download was counted as render_crash when nothing was wrong: the PDF was right there.
        # httpx fetches the bytes, which is what the capture wanted in the first place.
        if _looks_like_download(url):
            try_httpx = True

        http_fr = None
        if try_httpx:
            try:
                async with self.host.slot(url):
                    http_fr = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
            except HostSlotBusy:
                # Re-raised past the broad handler below, which would otherwise turn a host being
                # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
                # worker requeues it; nothing about the page went wrong.
                raise
            except Exception as e:
                http_fr = FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                                      error=f"http:{e}", reason=errors.OTHER)
            if mode == "never":
                return http_fr, tab
            # Keep the wall markup from the FIRST tier that saw it. C3 does not always produce a
            # snapshot — when the stealth render itself fails there is no body at all — and then
            # whoever finally gives up has nothing to classify and falls back to "the IP is the
            # blocker". The httpx 403 body already carries window._cf_chl_opt.cType, which is the
            # one thing that distinguishes an unclearable interactive challenge from a real IP
            # block. Measured on zoopla: without this the URL was still filed as needs_network_path.
            if (http_fr.status or 0) >= 400 and http_fr.text_html:
                item.last_body = http_fr.text_html[:200_000]
            if not _needs_render(http_fr):
                self._note_render(host, False)
                return http_fr, tab
            # A TLS-fingerprint block usually clears with a real-Chrome JA3 GET — try that cheap
            # tier before spending a browser tab. Success ⇒ no render, no escalation signal.
            #
            # 401/403 alone was too narrow. Measured from the data centre against catalogued sites
            # that had been sitting at zero pages, curl_cffi cleared EVERY one of these and the
            # engine never reached the tier because of the status code:
            #
            #     thalesgroup.com   200 + 212-byte shell  ->  146,278 bytes
            #     siatt.com.br      429                   ->   17,526
            #     xads.com          454                   ->   10,533
            #
            # So: any status a WAF uses to refuse a client it dislikes, AND the thin-shell case —
            # a 200 that is really a block or an app shell is exactly what a JA3 GET fixes, and it
            # costs a single request against a browser tab's 10-90 seconds.
            if (http_fr.status in _IMPERSONATE_STATUSES or _looks_thin(http_fr)
                    or _transport_blocked(http_fr)):
                imp = await self._impersonate_try(item, url, host)
                if imp is not None:
                    self._note_render(host, False)
                    return imp, tab
            # DEFER instead of rendering inline. This worker is in the httpx lane; a render here
            # would hold it for 10-90s on the most expensive work available, which is precisely the
            # starvation the two lanes exist to prevent. The links are harvested from the httpx body
            # FIRST, so discovery keeps running at httpx speed and the render only has to recover
            # this page's own content. Skipped when we are already ON the render lane (that is what
            # a render-lane worker is for) and when attempts are exhausted (let the ladder finish so
            # _render_fallback / C2 / C3 still get their turn).
            if (not item.needs_render
                    and os.environ.get("CRAWLER_DEFER_RENDERS", "1") != "0"
                    and item.render_attempts < _env_int("CRAWLER_RENDER_MAX_ATTEMPTS", 4)):
                try:
                    self._enqueue_links(ctx, http_fr, url, item.depth)
                except Exception:
                    log.debug("defer: link enqueue failed for %s", url, exc_info=True)
                item.last_body = (http_fr.text_html or "")[:200_000]
                ctx.unreserve()          # reserve() ran before the fetch; the render pass re-reserves
                self._defer_render(item, f"httpx status={http_fr.status} needs a browser")
                raise RenderDeferred(url)
            # else: JS-dependent / blocked → fall through to the browser (counts toward escalation)

        # Render path: always mode, an escalated host, a screenshot page, or an auto page that
        # httpx couldn't serve. In pool mode this is where a tab is first borrowed for this item.
        tab = await self._ensure_tab(tab, hold)
        try:
            # Render permit (per-host share + bounded wait on the global gate), then host slot.
            async with self._render_permit(host):
                async with self.host.slot(url):
                    fr = await _render_page(tab, url, self.host.timeout_ms(host),
                                            ctx.job.capture, ctx.job.interaction,
                                            discover=_depth_ok(item.depth, ctx.job.max_depth))
        except RenderBusy:
            raise                       # deflect to _worker's requeue; NOT a render crash
        except HostSlotBusy:
            # Re-raised past the broad handler below, which would otherwise turn a host being
            # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
            # worker requeues it; nothing about the page went wrong.
            raise
        except Exception as e:
            tab = await self._recycle_into(tab, hold)
            fr = await self._render_fallback(item, url, host, http_fr, e)
        # httpx was tried and wasn't enough → escalation signal, but ONLY when the httpx result was
        # a real page that happened to be a JS shell. A block, a timeout or a rate-limit says
        # nothing about whether this host needs JavaScript, and escalating on it is permanent:
        # _note_render early-returns once a host is in the set, so there is no way back. That is how
        # 91 of 105 always-render hosts got there — all of them serving fine over httpx today.
        if try_httpx and mode == "auto" and _is_js_shell_signal(http_fr):
            self._note_render(host, True)
        # Keep the wall from the RENDER too, not only from httpx. A render_js=true job takes
        # mode="always", so try_httpx is False and the httpx capture above never runs — which is
        # exactly how a zoopla job (render_js=true) still ended up filed as needs_network_path
        # after the httpx-side capture was added. Whatever tier saw the interstitial, its markup is
        # what lets _unclearable_wall read window._cf_chl_opt.cType instead of guessing.
        if not item.last_body and fr is not None and (fr.status or 0) >= 400 and fr.text_html:
            item.last_body = fr.text_html[:200_000]
        return fr, tab

    async def _render_fallback(self, item: "WorkItem", url: str, host: str,
                               http_fr: "FetchResult | None", exc: Exception) -> "FetchResult":
        """A render died — get the page by other means before calling it a failure.

        Previously this only REUSED an httpx result that happened to have been fetched already. When
        the render path was chosen up front (render-always host, screenshot job, careful host) there
        was no such result, so a crashed tab meant the page was simply lost and logged as
        `render_crash` — 2,616 of them, with no attempt to fetch the page any other way.

        The ladder here is ordered by cost, and every rung is a genuinely different mechanism, which
        is the point: a Chromium crash tells you nothing about whether httpx or Firefox can load it.
            1. the URL is a FILE (ERR_ABORTED) → fetch the bytes, which is what it needed all along
            2. an httpx result already in hand → use it
            3. httpx not tried yet → try it now
            4. C3 CamoFox — a different browser engine (Firefox), so a Chromium-specific crash,
               fingerprint block or codec fault can still succeed
        Only when all four are exhausted is it recorded as a failure, and by then `render_crash`
        means "nothing could fetch this", not "the first thing we tried broke".
        """
        err = f"render:{exc}"
        ctx = item.ctx

        # 1 · Not a page at all. Chromium aborts navigation when the response is a download, so the
        #     URL is fine — the browser was the wrong tool. Refetching over httpx gets the PDF.
        if errors.is_download_abort(err):
            try:
                async with self.host.slot(url):
                    got = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
                if got is not None and (got.body_bytes or got.text_html) and \
                        got.status and got.status < 400:
                    log.info("render→httpx (download) url=%s kind=%s", url, got.kind)
                    _tick_stat("recovered")
                    return got
            except HostSlotBusy:
                # Re-raised past the broad handler below, which would otherwise turn a host being
                # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
                # worker requeues it; nothing about the page went wrong.
                raise
            except Exception:
                pass

        # 2 · An httpx body we already paid for.
        if (http_fr is not None and http_fr.is_html() and http_fr.text_html
                and http_fr.status and http_fr.status < 400):
            return http_fr

        # 3 · httpx was never tried (render-always / screenshot / careful host). Try it now: a
        #     server-rendered page does not need the browser that just died.
        if http_fr is None:
            try:
                async with self.host.slot(url):
                    got = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
                if got is not None and got.is_html() and got.text_html and \
                        got.status and got.status < 400 and not _needs_render(got):
                    log.info("render→httpx recovered url=%s", url)
                    _tick_stat("recovered")
                    return got
                http_fr = got
            except HostSlotBusy:
                # Re-raised past the broad handler below, which would otherwise turn a host being
                # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
                # worker requeues it; nothing about the page went wrong.
                raise
            except Exception:
                pass

        # 4 · A different browser engine (CamoFox/Firefox) is worth trying after a Chromium crash,
        #     but it is NOT done here: _try_camofox_fallback sends the document itself, so returning
        #     anything from this function would store it a second time. Falling through with a
        #     render_crash reason hands the URL to _on_failure, which owns the C3 escalation and
        #     already knows how to end the item when C3 succeeds.
        #
        # Keep the ORIGINAL render error as the detail — it is the most specific thing we know, and
        # it is what makes the render_crash bucket diagnosable after the run.
        reason = errors.classify_failure(None, err) or errors.RENDER_CRASH
        return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                           error=err, reason=reason)

    async def _conditional_unchanged(self, item: "WorkItem", host: str) -> bool:
        """Incremental recrawl: send a conditional GET (If-None-Match/If-Modified-Since from
        crawl_history). A 304 means the page is unchanged since last run → the caller skips the
        expensive render + re-ingest. Bumps last_seen (COALESCE preserves the stored hash).
        Careful gov/mil hosts are exempt — a bot-UA probe draws the same WAF resets that trip
        the breaker, so those recrawl by rendering normally."""
        if self.history is None:
            return False
        if errors.is_careful_host(host) or host in self.host.force_careful:
            return False
        cond = self.history.conditional_headers(item.url)
        if not cond:                          # never seen → nothing to be conditional about
            return False
        try:
            async with self.host.slot(item.url):
                fr = await asyncio.to_thread(item.ctx.fetcher._http_fetch, item.url, cond)
        except HostSlotBusy:
            # Re-raised past the broad handler below, which would otherwise turn a host being
            # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
            # worker requeues it; nothing about the page went wrong.
            raise
        except Exception:
            return False
        if fr is not None and fr.not_modified:
            self.history.upsert(item.url, content_hash=None,
                                etag=fr.etag or cond.get("If-None-Match"),
                                last_modified=fr.last_modified, status=304, fetched_at=_now_iso(),
                                crawl_run_id=item.ctx.crawl_run_id)
            return True
        return False

    async def _process(self, item: WorkItem, tab, hold=None):
        # hold is the tab-pool holder (CRAWLER_TAB_POOL): None in legacy mode (every _ensure_tab /
        # _recycle_into below is then a pass-through and this runs exactly as before). In pool mode
        # `tab` starts None and each browser op borrows the shared tab into hold; the early `return
        # tab` lines return None, which the pool worker ignores (it returns hold["tab"] to the pool).
        ctx = item.ctx
        # Pick up any depth-min relaxation that landed while this item waited in the queue. Without
        # it the WorkItem carries the depth it was created with — so the page is judged, and its
        # children enqueued, against the descent depth this fix exists to correct. Items can sit in
        # the frontier for hours, which is exactly the window in which a shallow hub is found.
        if ctx is not None:
            relaxed = getattr(ctx, "depth_at", {}).get(item.url)
            if relaxed is not None and relaxed < item.depth:
                item.depth = relaxed
        url = item.url
        host = (urlsplit(url).hostname or "").lower()

        # THE BLOCKLIST, checked again here and not only on enqueue. _should_enqueue guards the
        # frontier, but by the time a host is blocked there are already URLs past that gate — in the
        # in-memory queues, in the render lane, in a retry timer — and every one of them still gets
        # fetched and STORED. Measured during the 2026-07-30 purge: 80,289 rows deleted and blocked
        # hosts kept arriving at ~16 rows per 20s from work that was already in flight, so the purge
        # could not converge while the crawl ran. This is the last gate before the fetch, so a host
        # blocked mid-crawl stops immediately instead of after its in-flight work drains.
        blk = blocklist.blocked(url)
        if blk:
            ctx.bump_skip(errors.BLOCKLISTED)
            return tab

        # GENERATOR TRAPS, checked here for exactly the same reason and with the same shape.
        # Tightening _should_enqueue governs DISCOVERY only, so on the day this landed the fleet
        # still held 13,388 queued URLs it would no longer follow — and a crawl empties its queue
        # long before it discovers anything new. Pruning the durable frontier cleans an instance
        # that is idle, but the instances holding the junk are by definition the ones running, and
        # a prune cannot reach into a live engine's in-memory queue in any case. This gate can: the
        # item is dropped as it comes up, at no network cost, so a running instance drains its own
        # backlog instead of waiting for someone to stop it.
        if _is_action_link(url):
            ctx.bump_skip(errors.TRAP)
            return tab

        # Circuit breaker + known-gone skips — BEFORE reserve() so a cooling host or a
        # permanently-gone URL never burns this job's page budget.
        if host in self.dead_hosts:
            loop = asyncio.get_running_loop()
            if loop.time() >= self.host_cooldown.get(host, 0.0):
                self.dead_hosts.discard(host)        # cooldown elapsed → revive, try live again
                self.host_fails[host] = 0
            elif await self._recover_deadhost(item, host):
                return tab                            # recovered via C2/C3 (counted as recovered)
            elif item.deadhost_waits < _env_int("CRAWLER_DEADHOST_MAX_WAITS", 4):
                # Host cooling, no C2/C3 recovery. Do NOT drop the URL — on a single-host crawl that
                # drains the frontier to empty and ends the job before the 120s revival fires. Requeue
                # it to retry when the cooldown lifts; jitter so ~1500 held URLs don't revive in one
                # thundering herd and instantly re-trip the breaker. _mark_progress keeps the engine
                # from idle-terminating during the wait (CRAWLER_ENGINE_IDLE_S == the cooldown); safe
                # because deadhost_waits is capped.
                wait = max(1.0, self.host_cooldown.get(host, 0.0) - loop.time()) + random.uniform(0.0, 5.0)
                self._schedule_wait(item, wait)
                self._mark_progress()
                return tab
            else:
                ctx.bump_skip(errors.HOST_DOWN)       # waited out the cap → host really is dead; SKIP
                return tab
        if self.history is not None and self.history.is_gone(url):
            ctx.bump_skip(errors.GONE_SKIP)
            return tab
        if errors.careful_off_peak_now(host):        # gov host, outside allowed hours
            ctx.bump_skip(errors.OFF_PEAK)
            return tab

        # Incremental recrawl (opt-in): a conditional GET says 304 Not-Modified → the page is
        # unchanged since last run, so skip it (no render, no re-ingest). Runs BEFORE reserve()
        # so unchanged pages don't burn the page budget — the budget goes to new/changed pages.
        # The depth gate keeps the shallow discovery surface fresh (see _incremental_skip_eligible);
        # CRAWLER_INCREMENTAL_FRESH_DEPTH sets it (default 1 = seeds + top listing layer, 0 = seeds only).
        fresh_depth = _env_int("CRAWLER_INCREMENTAL_FRESH_DEPTH", 1)
        # CRAWLER_FORCE_INCREMENTAL=1 turns on 304-skip for EVERY job without editing job configs —
        # for re-crawls where most pages are unchanged, a conditional GET (If-None-Match) 304s and
        # skips the fetch+hash (and the render, if it would have escalated), instead of downloading
        # and hashing the body just to drop it. Only helps where the origin sends validators.
        incremental = ctx.job.incremental or os.environ.get("CRAWLER_FORCE_INCREMENTAL", "0") == "1"
        if (_incremental_skip_eligible(incremental, item.depth, fresh_depth)
                and await self._conditional_unchanged(item, host)):
            ctx.not_modified += 1
            _tick_stat('not_modified_304')            # ...and make it visible in /v1/metrics
            self._mark_progress()
            _tick_page()                              # incremental recrawl: 304-checked page counts
            return tab

        if ctx.done or not ctx.reserve():
            return tab

        # Proactive C4: this host has already been cleared once by the host solver, so every page on
        # it is behind the same interactive Cloudflare wall. Skip the whole httpx -> render -> C3
        # ladder (which for an interactive challenge is ~57s of certain-to-fail C3 work per page)
        # and go straight to the WARM host solver (~5s each). This is what lets a solved site crawl
        # to max_pages instead of dead-ending after page 1. No-op unless the solver is configured.
        if host in self.host_needs_c4 and hostsolver.enabled():
            try:
                if await self._solve_on_host(item, "host known to need C4 (warm session)"):
                    _tick_page()
                    return tab
            except Exception:
                log.debug("proactive C4 failed for %s", url, exc_info=True)
            # The warm solve did not land — hand back the page budget and fall through to the full
            # ladder, which may still recover it (or record the wall).
            try:
                ctx.unreserve()
            except Exception:
                pass

        # Proactive C3: for known high-WAF hosts, skip the noisy C1 live fetch and go
        # straight to CamoFox with captcha solving. Only if C3 fails do we fall through
        # to the normal live fetch / C2 / API ladder. Skipped for override hosts — a flaky no-bot
        # box (mod.gov.my) is served by light httpx, not a heavy stealth render from the same IP.
        if _is_waf_host(host) and camofox_client.enabled() and self.host.override_for(host) is None:
            try:
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    self._audit(url, host, 200, errors.SERVED_BY_CAMOFOX, "allow")
                    _tick_page()                          # proactive-C3 page counts toward speed too
                    return tab
            except Exception:
                log.info("proactive_c3_failed job=%s url=%s", ctx.job.job_id, url, exc_info=True)

        robots_decision = "off"
        # Consult robots only when the shared cache exists AND this job respects robots — a bypass
        # job in a mixed batch skips the consult and the robots→C3 detour, fetching directly.
        if self.robots and _job_respects_robots(ctx.job, self.seed.capture_defaults):
            try:                                  # allow | deny | no_robots (richer than a bool)
                robots_decision = await asyncio.to_thread(self.robots.decision, url)
            except Exception:
                robots_decision = "allow"
            if robots_decision == "deny":
                # robots.txt forbids the live fetch — don't just drop it: route the URL through C3
                # (CamoFox), which the operator opted into for exactly these hosts. If that comes up
                # empty we record the robots block and skip. (C2/archive is no longer a rung here —
                # it is a separate, explicitly dated mode; see crawler/c2.py.)
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    self._audit(url, host, None, errors.SERVED_BY_CAMOFOX, robots_decision)
                    return tab
                ctx.bump_skip(errors.ROBOTS)
                self._audit(url, host, None, errors.ROBOTS, robots_decision)
                return tab

        fr, tab = await self._fetch_page(item, tab, host, hold)
        self._mark_progress()
        _tick_page()                                  # live speed metric: one page worked through
        self._audit(url, host, fr.status,
                    fr.reason or errors.classify_failure(fr.status, fr.error), robots_decision)

        # Image-text captcha gate: the whole page is behind a distorted-text image. The render path
        # flags it (NEEDS_CAPTCHA_SOLVER, precise); the httpx path is caught by the cheap html filter
        # so a non-render_js crawl escalates too. Either way, hand it to the C3 stealth browser, which
        # OCR-solves it with the local vision model and is let through to the real page. If C3 can't
        # clear it either, record the wall (don't store the gate). Skipped for override hosts.
        image_text_gate = fr.reason == errors.NEEDS_CAPTCHA_SOLVER or (
            os.environ.get("CRAWLER_CAPTCHA_OCR", "1") == "1"
            and fr.is_html() and captcha.looks_image_text(fr.text_html or ""))
        if image_text_gate and camofox_client.enabled() and self.host.override_for(host) is None:
            try:
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    self._audit(url, host, 200, errors.SERVED_BY_CAMOFOX, "allow")
                    return tab
            except Exception:
                log.info("image_text→c3 failed url=%s", url, exc_info=True)
            ctx.bump_skip(errors.NEEDS_CAPTCHA_SOLVER)
            return tab

        # A WAF wall served with HTTP 200 is still a block, not content. CloudFront/Cloudflare do
        # exactly this, so the status check alone stored 709 "403 ERROR / Request blocked" bodies as
        # real documents — and worse, recorded those URLs as CRAWLED, so incremental recrawl skipped
        # them forever. Rewrite it to a 403 so it takes the normal failure ladder (C3 escalation +
        # record_failure), which is what should have happened at fetch time.
        if _is_block_body(fr):
            log.info("block page served as 200 — treating as 403: %s", url)
            fr.status, fr.reason = 403, errors.NEEDS_NETWORK_PATH
        if fr.error or not fr.status or fr.status >= 400:
            fr, tab = await self._on_failure(item, fr, tab, host, hold)
            if fr is None or fr.error or not fr.status or fr.status >= 400:
                return tab
            if _is_block_body(fr):        # the ladder came back with another wall — don't store it
                return tab
        ctx.fetched += 1
        _tick_stat('fetched')
        self.host_fails[host] = 0        # a success clears the host's breaker count
        # …and the refusal streak. This is what keeps the 401/403 breaker from killing a site that
        # merely gates part of itself: one page served resets it, so only a host saying no to
        # EVERYTHING in a row ever trips.
        self.host_forbidden[host] = 0
        self.host_crashes[host] = 0      # …and the crash streak: this site CAN be rendered
        # Proof of life for the deflect budget: this host is delivering, however full its slots look.
        try:
            self.host_last_ok[host] = asyncio.get_running_loop().time()
        except RuntimeError:
            pass                         # no running loop (sync test path) — the budget just won't reset

        # Enqueue child links within depth/domain budget (loop thread; parse is cheap CPU).
        self._enqueue_links(ctx, fr, url, item.depth)
        # Links harvested from pagination pages 2+ (opt-in interaction.paginate) — enqueue them
        # too so a paginated tender listing's later-page links reach the frontier.
        if fr.extra_links and _depth_ok(item.depth, ctx.job.max_depth) and ctx.has_budget():
            for link in fr.extra_links:
                try:
                    self._enqueue_candidate(ctx, canonicalize_url(link), url, item.depth)
                except Exception:
                    continue

        # Click-discovery (opt-in, render_js only): after href links are enqueued, click the
        # NON-href clickables to surface JS/SPA navigation. Runs inside a re-acquired host slot
        # for politeness (the render slot already closed). A wedged tab is recycled.
        if (ctx.job.click_discovery and ctx.job.render_js and fr.is_html()
                and os.environ.get("CRAWLER_CLICK_DISCOVERY", "1") != "0"
                and _depth_ok(item.depth, ctx.job.max_depth) and ctx.has_budget()
                and host not in self.dead_hosts):
            try:
                async with self.host.slot(url):
                    await self._click_discover(ctx, await self._ensure_tab(tab, hold),
                                               fr.final_url or url, item.depth)
            except HostSlotBusy:
                # Re-raised past the broad handler below, which would otherwise turn a host being
                # busy into a recorded FAILURE for this URL. Same precedent as RenderBusy: the
                # worker requeues it; nothing about the page went wrong.
                raise
            except Exception as exc:
                log.debug("click_discovery failed %s: %s", url, exc)
                tab = await self._recycle_into(tab, hold)

        # Content-level wall check. A login form, a "page not found" body, a paywall teaser and a
        # consent gate all arrive as a perfectly ordinary HTTP 200 with valid HTML, so every check
        # upstream of here passes them — and the wall gets stored as the document. Worse, a site's
        # walls are near-identical to each other, so they also poison near-duplicate detection.
        # Links were ALREADY enqueued above, deliberately: a login wall on one page says nothing
        # about its neighbours, and dropping the whole subtree here would silently shrink the crawl.
        # Anti-bot/captcha walls are excluded (CONTENT_WALLS) — the C1->C2->C3 ladder owns those.
        if (fr.is_html() and fr.text_html and (fr.status or 200) < 400
                and os.environ.get("CRAWLER_WALL_CHECK", "1") != "0"):
            v = edgecase.diagnose(url, status=fr.status, body=fr.text_html,
                                  final_url=fr.final_url or url)
            if v.case in edgecase.CONTENT_WALLS and v.action != edgecase.ACT_STORE:
                ctx.bump_reason(v.case)
                log.info("wall %s on %s — %s", v.case, url, v.evidence)
                if self.history is not None:
                    self.history.record_failure(
                        url, status=fr.status, category=v.reason,
                        failed_at=fr.fetched_at or _now_iso(),
                        crawl_run_id=ctx.crawl_run_id, detail=v.evidence)
                return tab

        # Capture candidates → HarvestedPage (mirrors harvest.py:161-166).
        base = fr.final_url or url
        html = fr.text_html if fr.is_html() else None
        pdf_links, image_candidates, media_candidates = _capture_assets(ctx.job, html, base)
        if fr.extra_pdf_links:                      # PDFs harvested from pagination pages 2..N-1
            seen = set(pdf_links)
            pdf_links = pdf_links + [u for u in fr.extra_pdf_links if u not in seen]
        if getattr(fr, "insecure_tls", None):
            ctx.bump_recovered(errors.TLS_UNVERIFIED)
            log.warning("stored an UNVERIFIED-TLS page: %s (original cert error: %s)",
                        url, fr.insecure_tls)
        hp = HarvestedPage(url=url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates,
                           parent_url=item.parent_url,
                           # Its OWN tier name, never c1_live. served_by is a column the dashboard
                           # already groups by, so an untrusted fetch is its own row instead of
                           # hiding inside the live-fetch count, and `WHERE served_by <>
                           # 'c1_live_tls_unverified'` is how a consumer excludes them. None keeps
                           # today's shape (build_document defaults it to c1_live).
                           served_by=("c1_live_tls_unverified"
                                      if getattr(fr, "insecure_tls", None) else None))

        # build_document (CPU + possible translate) off-loop; plain data only.
        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return tab

        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
        ctx.bump_reason(g.reason)
        # Per-page keyword gate: capture ONLY pages that themselves hit >=1 corpus keyword
        # (freshness drop included in g.keep). The child links were ALREADY enqueued above,
        # so the whole site is still crawled for discovery — we just skip storing/forwarding
        # any page with no keyword match. (An empty corpus fails open: g.keep is True for all.)
        if not g.keep:
            ctx.dropped_by_gate += 1
            _tick_stat('dropped_by_gate')
            return tab
        await self._finalize(ctx, doc, hp, fr, url)
        return tab

    async def _finalize(self, ctx: "JobCtx", doc, hp, fr, url) -> None:
        """Critical path only (tab held, fast): dedup verdict + history upsert + seen-hash. The slow
        I/O tail (asset enrich + ingest POST) is handed to the finalizer pool so the worker's tab
        returns to the frontier immediately instead of blocking on PDF downloads + the ingest POST.
        Dedup/history stay HERE on the loop thread so two near-simultaneous pages can't double-emit."""
        # This URL came back. If it was on the unresolved list, take it off — otherwise the list
        # only ever grows and a page that started working again keeps claiming to be broken.
        try:
            issues.log().resolve(url)
        except Exception:
            pass
        stored = self.history.get(url)
        verdict = classify(stored, status=fr.status, content_hash=doc.content_hash)
        self.history.upsert(url, content_hash=doc.content_hash, etag=fr.etag,
                            last_modified=fr.last_modified, status=fr.status,
                            fetched_at=fr.fetched_at, js_heavy=ctx.job.render_js,
                            crawl_run_id=ctx.crawl_run_id)
        if verdict == "unchanged":
            ctx.skipped_unchanged += 1
            _tick_stat('skipped_unchanged')
            return
        if doc.content_hash in ctx.seen_hashes:
            # See the same recovery on the C3 path: when interactions ran we extract from the
            # rendered inner_text, which on some portals is the shared shell for every URL, so
            # distinct pages hash alike and all but the first are thrown away AFTER being
            # fetched. serviziocontrattipubblici.it, measured 2026-08-18: 9 fetched, 1 kept, 8
            # "duplicates" — and those same URLs extracted from raw HTML differ (1943 / 1036 /
            # 786 chars). This is the MAIN render path, which is where that job actually ran.
            alt = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                          ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id,
                                          static_text=True)
            if alt is not None and alt.content_hash not in ctx.seen_hashes:
                log.info("duplicate_recovered_from_html job=%s url=%s chars=%d",
                         ctx.job.job_id, url or hp.url, len(alt.main_text))
                doc = alt
                self.history.upsert(url, content_hash=doc.content_hash, etag=fr.etag,
                                    last_modified=fr.last_modified, status=fr.status,
                                    fetched_at=fr.fetched_at, js_heavy=ctx.job.render_js,
                                    crawl_run_id=ctx.crawl_run_id)
            else:
                ctx.skipped_duplicate += 1
                _tick_stat('skipped_duplicate')
                return
        ctx.seen_hashes.add(doc.content_hash)
        ctx.kept += 1
        _tick_stat('kept')
        # Hand the enrich+send tail to the finalizer pool (frees the tab). A full queue awaits here —
        # natural backpressure, bounded RAM. No pool (tests) → run it inline for identical behavior.
        # `url` rides along: it is the CANONICAL url, the key history is stored under, and the tail
        # needs it to undo this upsert if the document never reaches the store (see _run_finalize).
        if getattr(self, "_finalize_q", None) is not None:
            await self._finalize_q.put((ctx, doc, hp, fr, url))
        else:
            await self._run_finalize(ctx, doc, hp, fr, url)

    async def _run_finalize(self, ctx: "JobCtx", doc, hp, fr, url: str | None = None) -> None:
        """The slow tail: download assets (DEDICATED asset pool, isolated from the shared to_thread
        pool so a slow asset host can't starve page fetches), then POST to ingest. Runs on a finalizer
        coroutine (or inline when there's no pool). Counters mutate on the loop thread after each await,
        matching the engine's one-loop-one-thread model. (getattr fallback keeps __init__-skipping
        tests working.)"""
        pool = getattr(self, "_asset_pool", None)
        if pool is not None:
            await asyncio.get_running_loop().run_in_executor(
                pool, extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        else:
            await asyncio.to_thread(extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        # Propagate C3 captcha/solver provenance from the FetchResult to the Document.
        fr = hp.fetch
        if fr.captcha_type:
            doc.captcha_type = fr.captcha_type
            doc.captcha_solved = fr.captcha_solved
            doc.captcha_solver = fr.captcha_solver
            doc.captcha_solver_cost_usd = fr.captcha_solver_cost_usd
        if fr.proxy_country:
            doc.proxy_country = fr.proxy_country
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        _tick_stat('sent')
        if outcome.accepted:
            ctx.accepted += 1
            _tick_stat('accepted')
        else:
            ctx.rejected += 1
            # The page was fetched fine but never reached the store. _finalize has ALREADY written
            # its content_hash to history, so without this the next run reads "unchanged" and skips
            # it forever — silent, permanent, self-concealing loss. Undo that write so the page is
            # re-fetched and re-sent next run.
            #
            # Only for STORE-side failures. A validation rejection means the document is genuinely
            # unacceptable; clearing history for that would re-crawl a permanently-invalid page on
            # every run, which is a different bug wearing the same fix.
            if url and is_retryable_failure(outcome.failing_rule):
                try:
                    await asyncio.to_thread(self.history.mark_unsent, url)
                    log.warning("ingest failed for %s (%s) — cleared history so it re-sends next run",
                                url, outcome.failing_rule)
                except Exception:
                    # If we cannot undo the upsert, the document IS permanently lost. Say so loudly:
                    # this is the one place where a swallowed error costs data.
                    log.exception("LOST: %s failed ingest and history could not be cleared — this "
                                  "document will never be re-sent", url)

    async def _finalizer_loop(self) -> None:
        """Drain the finalize queue: enrich + send each kept doc. One task per CRAWLER_FINALIZERS."""
        q = self._finalize_q
        while True:
            item = await q.get()
            try:
                fctx, doc, hp, fr, furl = item
                await self._run_finalize(fctx, doc, hp, fr, furl)
            except Exception:
                log.exception("finalizer failed")
            finally:
                q.task_done()

    # ── tab pool (CRAWLER_TAB_POOL=1) ────────────────────────────────────────────────────────────
    # Decouples httpx concurrency from Chromium tabs. In the legacy model one _worker owns one tab
    # for its whole life, so the frontier-consumer count == the tab count: a 97%-httpx crawl still
    # launches W*T Chromium tabs that do nothing but hold RAM (measured: 256 tabs, ~10GB, on a crawl
    # where the tab is touched on 3% of pages), and the only way to cut that RAM was to cut tabs,
    # which also cut httpx concurrency and thus speed. With the pool, N tab-less workers pull the
    # frontier and BORROW a tab only for an actual render, so you can run a large N (high httpx
    # concurrency) against a small tab pool (low RAM). Invariant that makes borrow non-blocking:
    # concurrent renders are capped by the adaptive gate at <= len(tabs) == pool size, and a _process
    # holds at most one tab, so a free tab always exists when a render is admitted.
    # hold is a per-item {"tab": page|None}; the worker guarantees the borrowed tab returns to the
    # pool. hold is None in legacy mode, and every helper below is then a pass-through — the legacy
    # path is byte-identical.
    async def _ensure_tab(self, tab, hold):
        """The tab to use for a browser op. Legacy: the worker's own tab. Pool: borrow once into hold."""
        if hold is None:
            return tab
        if hold.get("tab") is None:
            hold["tab"] = await self._tab_pool.get()
        return hold["tab"]

    async def _recycle_into(self, tab, hold):
        """Recycle a crashed tab; keep the holder in sync so pool mode returns the fresh one."""
        new = await self._recycle(tab)
        if hold is not None:
            hold["tab"] = new
        return new

    async def _return_tab(self, tab) -> None:
        """Park a borrowed tab and put it back — ALWAYS a live tab, so the pool never shrinks.
        A dead tab is replaced with a fresh page rather than poisoning the pool."""
        try:
            if _blank_between_items():
                await tab.goto("about:blank")     # park (idle loaded page burns a core) + liveness probe
        except Exception:
            try:
                tab = await self._recycle(tab)    # dead → fresh page from a live context
            except Exception:
                tab = None
        if tab is None:
            try:
                tab = await self._recycle(None)   # mint a replacement so pool size stays constant
            except Exception:
                return                            # give up: pool shrinks by 1 (rare; renders still bounded)
        self._tab_pool.put_nowait(tab)

    async def _pool_worker(self) -> None:
        """Tab-less lane consumer (CRAWLER_TAB_POOL=1). Owns no tab: it borrows one into `hold` only
        when _process renders, and the finally always returns a live tab to the pool.

        Pulls via _next_item so the httpx lane is drained first and only a bounded share of workers
        can be inside a render at once."""
        while True:
            item, took_lane = await self._next_item()   # counts _inflight_items at the pull
            q = self.render_q if took_lane else self.frontier
            hold: dict = {"tab": None}
            # Only a page that was actually WORKED is marked done in the durable frontier. The
            # `continue` below runs the finally block, so marking done there recorded every item a
            # STOP dropped as if it had been crawled — measured on a live interrupted run: 608 URLs
            # persisted, 0 pending, and resume had nothing to resume. Which is precisely the case
            # resume exists for.
            processed = False
            try:
                if stop_requested() or _item_dropped(item):
                    continue
                # Host-queue deflection — RENDER ITEMS ONLY.
                #
                # This guard exists to stop a worker parking a TAB on a host whose permit queue is
                # already deep. In pool mode an httpx item holds no tab: it is a coroutine and a
                # socket, and waiting on the host slot costs nothing. Deflecting it was pure loss,
                # and expensive loss — _schedule_busy parks the item OUT of the frontier via
                # call_later for CRAWLER_HOST_BUSY_REQUEUE_S (25s in production), so with a handful
                # of hosts and HOST_QUEUE_MAX=2 only ~10 items could ever be in flight while
                # thousands sat in timers. MEASURED on the live batch: 2,662 host-deflections
                # against 243 fetched pages (11 per page), CPU at 0%, 3 of 64 render permits in
                # use, 80 tabs idle, 0.086 pages/sec. The pool was not saturated — it was starved
                # by its own deflection loop.
                if item.needs_render or self._will_render_first(item):
                    qhost = (urlsplit(getattr(item, "url", "")).hostname or "").lower()
                    # RESET THE MOMENT THE HOST HAS ROOM. Without this the host clock is one-way:
                    # a site that was briefly saturated would keep its start time for the rest of
                    # the run, and every later item on that host would inherit an expired budget
                    # and be given up on instantly — turning a transient jam into a permanent one.
                    if qhost and self.host.queue_depth(qhost) < self._host_queue_max(qhost):
                        self.host_busy_since.pop(qhost, None)
                    if qhost and self.host.queue_depth(qhost) >= self._host_queue_max(qhost):
                        _tick_stat("deflected_host")
                        now = asyncio.get_running_loop().time()
                        if not item.first_deflect_at:
                            item.first_deflect_at = now
                        item.host_deflects += 1
                        # THE BUDGET IS THE HOST'S, NOT THE ITEM'S — and that is the whole fix.
                        #
                        # Judged per item, the budget is unenforceable at scale: the check only runs
                        # when an item is DEQUEUED, a deflected item goes back on render_q, and
                        # render_q was 45,825 deep — so one lap took about forty minutes. Every
                        # give-up message read "2 deflections over 2,187s": the item was examined
                        # exactly twice in 36 minutes, so "180s of patience" meant one lap of the
                        # queue, and 24,000 items each waited their own 40 minutes before quitting.
                        #
                        # Keyed on the host, the FIRST item still serves the full 180s, and every
                        # item behind it inherits that clock and gives up immediately. A blocked
                        # host drains in one pass instead of cycling for hours, and the render lane
                        # is freed for hosts that can actually be crawled.
                        first = self.host_busy_since.setdefault(qhost, now)
                        waited = max(now - first, now - item.first_deflect_at)
                        # BUT SATURATED IS NOT THE SAME AS WEDGED, and the clock above cannot tell
                        # them apart. A host whose pages ALL need a browser and whose slots are
                        # capped at CRAWLER_HOST_QUEUE_MAX runs permanently full while working
                        # perfectly — 295 queued pages at ~10s a render is 25 minutes of legitimately
                        # full slots, and 180s of patience threw most of them away. Measured on
                        # yakovlev.ru: 184 give-ups, "after 5 renders: host_busy", on a site that was
                        # serving us fine.
                        #
                        # So measure what actually distinguishes the two: has this host produced a
                        # page recently. A slow host keeps resetting its own budget by delivering;
                        # a wedged one delivers nothing and still drains in one pass, which is the
                        # livelock this budget exists to stop.
                        last_ok = self.host_last_ok.get(qhost)
                        if last_ok is not None:
                            waited = min(waited, now - last_ok)
                        # Give up on WALL-CLOCK, not a deflect count. The requeue delay
                        # (CRAWLER_HOST_BUSY_REQUEUE_S) is 25s in prod and ~0 in tests, so a fixed
                        # count would mean minutes here and nothing there. The deflect runs BEFORE
                        # _process, so it never bumps render_attempts and the normal give-up never
                        # fires: a batch drained to a wedged host (dieselgenerator, 52
                        # needs_network_path) spun ~300 deflections per fetched page, frontier empty,
                        # 0.11 pages/sec, done stuck at 0. After the budget the page is logged
                        # host_busy and the batch completes. ponytail: 180s patience, env-tunable.
                        if waited < _env_float("CRAWLER_HOST_DEFLECT_BUDGET_S", 180.0):
                            self._requeue_render(item, _host_backoff_delay(item.host_deflects))
                            continue
                        item.last_render_error = (
                            f"host busy: {item.host_deflects} deflections over {waited:.0f}s, "
                            f"{qhost} slots never drained")
                        # THE THIRD BREAKER, and the one that was missing. Two already exist: hard
                        # network failures (DNS/refused/TLS) cool a host down, and 401/403 does now
                        # too. Neither covers a host whose RENDERS simply never complete — the
                        # budget above is per ITEM, so a host that yields nothing is re-entered by
                        # the next queued URL, and the next, for as long as the queue lasts.
                        #
                        # Measured 2026-07-31: uacrussia.ru and yakovlev.ru both time out after 50s
                        # from this network — last page stored three days and seventeen hours ago —
                        # and between them produced 801 give-ups in six minutes, each one having
                        # occupied a render slot first.
                        #
                        # Gated on host_last_ok: a host that has served ANY page this run is slow,
                        # not wedged, and keeps its queue however full its slots look. Only a host
                        # that has delivered nothing at all is parked, and it parks into the same
                        # cooldown the other two breakers use, so its URLs are recovered via C2/C3
                        # and it revives for a fresh attempt.
                        self.host_stalled[qhost] = self.host_stalled.get(qhost, 0) + 1
                        if (qhost not in self.host_last_ok
                                and self.host_stalled[qhost]
                                >= _env_int("CRAWLER_HOST_STALLED_MAX", 50)):
                            self.dead_hosts.add(qhost)
                            self.host_cooldown[qhost] = (now + _env_float(
                                "CRAWLER_HOST_STALLED_COOLDOWN_S", 900.0))
                            self.host_stalled[qhost] = 0
                            log.warning("host_stalled host=%s — render slots never drained and not "
                                        "one page served; cooling off (queued URLs via C2/C3)", qhost)
                        # Force the terminal branch of _retry_or_log_render (it does +=1 then checks
                        # < cap), so it records the issue instead of requeuing. The page leaves the
                        # lane with a real cause and the crawl carries on.
                        item.render_attempts = _env_int("CRAWLER_RENDER_MAX_ATTEMPTS", 4)
                        self._retry_or_log_render(item)
                        continue
                await asyncio.wait_for(self._process(item, None, hold),
                                       timeout=_env_int("CRAWLER_ITEM_TIMEOUT_S", 240))
                processed = True
            except RenderDeferred:
                pass                    # moved to the render lane; still outstanding, NOT done
            except HostSlotBusy:
                # Not a failure and not an error: the host was busy, so give the worker back and
                # try this URL again shortly. Budget is returned or a merely-popular host would
                # quietly consume the job's max_pages allowance without fetching anything.
                try:
                    item.ctx.unreserve()
                except Exception:
                    pass
                _tick_stat("deflected_host")
                # Back to the lane it came from. A render item returned to the frontier would jump
                # ahead of cheap work again, which is the prioritisation the split exists to remove.
                if item.needs_render:
                    self._requeue_render(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
                else:
                    self._schedule_busy(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
            except RenderBusy:
                try:
                    item.ctx.unreserve()
                except Exception:
                    pass
                _tick_stat("deflected_render")
                self._requeue_render(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
            except asyncio.TimeoutError:
                # An item that blew the watchdog while rendering is the "endless render" case the
                # operator asked to be tracked: retried a bounded number of times, then logged with
                # a specific cause instead of vanishing into a warning.
                log.warning("item timeout — abandoning %s", getattr(item, "url", "?"))
                item.last_render_error = f"item watchdog ({_env_int('CRAWLER_ITEM_TIMEOUT_S', 240)}s) expired"
                self._retry_or_log_render(item)
            except Exception as exc:
                log.exception("worker page failed: %s", getattr(item, "url", "?"))
                if item.needs_render:
                    item.last_render_error = f"{type(exc).__name__}: {exc}"[:200]
                    self._retry_or_log_render(item)
            finally:
                if hold["tab"] is not None:
                    try:
                        await self._return_tab(hold["tab"])
                    except Exception:
                        pass
                if took_lane:
                    self._release_lane()
                # Durable frontier: done ONLY if the page was really worked. A STOP-dropped, a
                # deferred and a requeued item are all still outstanding, and recording any of them
                # done would make a resume skip a page that was never fetched.
                if processed:
                    self._frontier_complete(item.ctx, item.url)
                self._inflight_items -= 1
                _LANE_STATE.update(frontier=self.frontier.qsize(), render_q=self.render_q.qsize())
                q.task_done()

    def _will_render_first(self, item: WorkItem) -> bool:
        """True when this item goes to the BROWSER before httpx, so it really does hold a tab.
        Mirrors the try_httpx decision in _fetch_page; kept cheap (no I/O) because it runs per item."""
        try:
            job = item.ctx.job
            host = (urlsplit(item.url).hostname or "").lower()
            mode = _render_mode(job)
            if mode == "never":
                return False
            careful = (errors.is_careful_host(host) or host in self.host.force_careful) \
                and self.host.override_for(host) is None
            wants_shot = "screenshot" in (job.capture or [])
            return bool(careful or mode == "always" or wants_shot
                        or host in self.host_render_always)
        except Exception:
            return False

    def _host_queue_max(self, host: str) -> int:
        """How many renders may be in flight against ONE host. Global default, per-host override.

        `CRAWLER_HOST_QUEUE_MAX=2` is politeness applied uniformly, and uniformly is the problem: a
        CDN-fronted site serves six concurrent requests without noticing, while a small self-hosted
        one should not get two. With one number for everyone, the big sites set the pace for the
        whole fleet — measured, four hosts held a 45,825-deep render queue and 197 give-ups in five
        minutes, at a median wait of 42 minutes each.

        Raising it is safe HERE and would not be everywhere: across all twelve frontiers, the number
        of pages any host has ever rate-limited us on is ZERO — no 429, no Retry-After, no
        rate_limited row. The 2 was never a response to pushback, it was a guess. So this stays
        conservative by default and is lifted per host, deliberately, for hosts that have earned it.

        Format: `CRAWLER_HOST_QUEUE_MAX_HOSTS="zf.com:6,rtx.com:6"`. A listed domain covers its
        subdomains, so `zf.com:6` also lifts `www.zf.com` — the two are one server and splitting
        them would be an accident, not a policy.
        """
        spec = os.environ.get("CRAWLER_HOST_QUEUE_MAX_HOSTS", "").strip()
        default = _env_int("CRAWLER_HOST_QUEUE_MAX", 2)
        if not spec or not host:
            return default
        if spec != getattr(self, "_hqm_spec", None):        # parse once per distinct value
            table: dict[str, int] = {}
            for part in spec.split(","):
                name, _, val = part.partition(":")
                name = name.strip().lower().lstrip(".").removeprefix("www.")
                try:
                    if name and int(val) > 0:
                        table[name] = int(val)
                except ValueError:
                    continue                                 # a malformed entry must not raise here
            self._hqm_spec, self._hqm_table = spec, table
        h = host.lower().removeprefix("www.")
        if h in self._hqm_table:
            return self._hqm_table[h]
        parts = h.split(".")
        for i in range(1, len(parts) - 1):
            parent = ".".join(parts[i:])
            if parent in self._hqm_table:
                return self._hqm_table[parent]
        return default

    def _requeue_render(self, item: WorkItem, delay: float) -> None:
        """Put a render item back on the RENDER lane after *delay*. Deliberately not the frontier:
        a render item returned to the httpx lane would jump ahead of cheap work again, which is the
        prioritisation this split exists to remove."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()

        def _put() -> None:
            self._pending_retries = max(0, self._pending_retries - 1)
            try:
                self._lane_put(item)
            except Exception:
                pass
        loop.call_later(delay, _put)

    async def _recycle(self, tab):
        """A tab crashed — close it and hand back a fresh page, relaunching a browser if needed."""
        try:
            await tab.close()
        except Exception:
            pass
        for _b, ctx in self.browsers:
            try:
                return await ctx.new_page()
            except Exception:
                continue
        # EVERY CONTEXT DEAD -> open a fresh one on a SHARED browser.
        #
        # This used to close every browser and launch a replacement, which is how the first version
        # of this leak started. The browsers are process-wide now, so a dead context costs exactly
        # one context: drop the dead entries, take a browser from the shared pool, and re-open.
        # Nothing here may close or relaunch a shared browser - see _SHARED_POOL.
        dead, self.browsers = list(self.browsers), []
        each = _env_float("CRAWLER_CLOSE_TIMEOUT_S", 5.0)
        for _b, _ctx in dead:
            await _bounded_close(_ctx.close(), each, "dead context")
        if dead:
            log.warning("recycle: closed %d dead context(s); browsers are shared and stay up",
                        len(dead))

        # REUSE THE BROWSER WE ALREADY HAVE. The dead entries hold (browser, context) pairs whose
        # BROWSER is the shared, still-running one - only the context died. Calling _shared_browsers
        # here would ask the module for a pool it already gave us, and in doing so would start a
        # real driver inside any test that never installed one.
        b = dead[0][0] if dead else (await _shared_browsers(self.W))[0]
        ctx = await b.new_context(user_agent=self.seed.capture_defaults["user_agent"],
                                  viewport={"width": 1920, "height": 1080},
                                  ignore_https_errors=_ignore_https_errors(),
                                  proxy=_proxy_config())
        self.browsers.append((b, ctx))
        return await ctx.new_page()

    def _audit(self, url: str, host: str, status: int | None, reason: str | None,
               robots_decision: str) -> None:
        """Append one compliance-audit row — ONLY for careful (gov/mil) hosts, so the table
        stays small and focused on the hosts where provable politeness matters."""
        if self.history is None:
            return
        if not (errors.is_careful_host(host) or host in self.host.force_careful):
            return
        try:
            self.history.record_audit(
                url=url, host=host, fetched_at=_now_iso(),
                ua=self.seed.capture_defaults.get("user_agent", ""),
                robots_decision=robots_decision, status=status, reason=reason, careful=True)
        except Exception:
            pass

    def _mark_progress(self) -> None:
        try:
            self._last_progress = asyncio.get_running_loop().time()
        except Exception:
            pass

    async def shutdown(self) -> None:
        """Tear the pool down. EVERY await here is bounded.

        A close() on a wedged Chromium never returns, and this runs inside the batch thread that
        _dispatch is blocked on — so one stuck tab left the batch flag at running=True forever, with
        an empty frontier, 0 in-flight and the CPU idle. MEASURED on a 6.4k-page pixabay crawl: the
        operator pressed STOP, all work finished and the dashboard sat at "running · (stopping…)"
        with done 0/1 until the container was restarted, which also refuses every later batch with
        "a batch is already running". Cleanup that cannot finish must be abandoned, not waited on.

        BUT ABANDONING IS NOT REAPING, and this docstring used to claim it was ("the process is
        ending and tini reaps the browser children anyway"). The process is NOT ending: shutdown()
        runs at the end of every batch and the container stays up for the next one. Every timed-out
        close therefore left a live node driver and its browsers running forever, one set per
        batch — twelve of them on instance 11, 2,266 chrome processes fleet-wide, and the crawl
        down to 0.14 pages/s. So what the timeout abandons, we now kill outright."""
        try:
            self._asset_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            io_pool = getattr(self, "_io_pool", None)
            if io_pool is not None:
                io_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        each = _env_float("CRAWLER_CLOSE_TIMEOUT_S", 5.0)
        for t in list(self.tabs):
            await _bounded_close(t.close(), each, "tab")
        # ONLY THIS BATCH'S CONTEXTS. The browsers and the driver behind them are process-wide and
        # are deliberately left running - see _SHARED_POOL. Closing them here is what leaked: on
        # instance 7, six batches closed cleanly (0 timeouts, 0 kills) and still left four whole
        # driver trees alive, because a Playwright close returning success does not mean the OS
        # process exited. Nothing is closed here that can strand a process; a context that refuses
        # to close costs one context, not a browser tree.
        for _b, ctx in list(self.browsers):
            await _bounded_close(ctx.close(), each, "context")
        self.browsers, self.tabs = [], []
        # _pw / _pw_pids are BORROWED references to the shared pool. Drop our copies without
        # touching the pool itself; a later batch re-borrows them from _SHARED_POOL.
        self._pw, self._pw_pids = None, set()


# ── sync bridge (the FastAPI endpoint is sync) ───────────────────────────────

def _adaptive_host_concurrency(jobs: list[Job], total_tabs: int) -> int:
    """Per-host concurrent-tab cap, scaled to the batch so a SMALL batch still saturates
    the pool. The pool is always ``total_tabs`` tabs, but each host is capped so 96 tabs
    never hammer one site — with a fixed low cap (3), a 1-job single-domain crawl leaves
    ~93 tabs idle. So: spread the pool evenly across the batch's DISTINCT seed hosts, i.e.
    cap ≈ total_tabs / distinct_hosts, clamped to [floor, ceiling].

    Result: 1 host → cap ~= the whole pool (deep single-domain crawl fans out wide);
    32 hosts → cap back down to the polite floor. .gov/.mil careful hosts still override
    to concurrency 1 inside HostLimiter regardless of this cap — politeness there is
    non-negotiable. Overridable end-to-end with CRAWLER_HOST_CONCURRENCY (explicit set wins).
    ponytail: distinct SEED hosts, not live frontier hosts — a same_domain_only crawl stays
    on its seed host, and cross-host batches list their hosts up front, so this is exact
    for the common case and a safe over-estimate otherwise.
    """
    explicit = os.environ.get("CRAWLER_HOST_CONCURRENCY")
    if explicit:                        # operator pinned it → respect the pin
        return max(1, int(explicit))
    hosts = {
        (urlsplit(u).hostname or "").lower()
        for j in jobs for u in j.seed_urls if (urlsplit(u).hostname or "")
    }
    n = max(1, len(hosts))
    floor = _env_int("CRAWLER_HOST_CONCURRENCY_FLOOR", 3)
    ceil = _env_int("CRAWLER_HOST_CONCURRENCY_CEIL", 24)
    return max(floor, min(ceil, total_tabs // n))


# ── the process-wide engine loop ─────────────────────────────────────────────────────────────
#
# Every batch used to call asyncio.run() on a fresh thread, so each got a NEW event loop and every
# Playwright object died with it. That is the whole reason browsers could not be reused across
# batches, and reusing them is the only structural fix for the driver leak: a pool built once
# cannot leak once per batch.
#
# Kept on its own thread rather than FastAPI's: the engine assumes it owns its loop (see the module
# docstring — one event loop, one thread), and running on the request loop would put crawl work in
# the same loop as the API's health checks.
_LOOP: "asyncio.AbstractEventLoop | None" = None
_LOOP_LOCK = threading.Lock()


def _engine_loop() -> "asyncio.AbstractEventLoop":
    """The loop every batch runs on, started on first use and never stopped.

    Daemon thread: the process must still be able to exit. A crashed or closed loop is replaced
    rather than reused, so one bad batch cannot wedge every later one.
    """
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is not None and not _LOOP.is_closed():
            return _LOOP
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, name="crawl-engine-loop",
                         daemon=True).start()
        _LOOP = loop
        return loop


def reset_engine_loop() -> None:
    """Drop the process-wide loop. For tests only — a suite that leaves a live loop behind makes
    the next test's failure land in a thread nobody is watching."""
    global _LOOP
    with _LOOP_LOCK:
        loop, _LOOP = _LOOP, None
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)


def run_batch_async(jobs: list[Job], *, forward: bool,
                    seed: Seed | None = None, kp=None) -> list[dict]:
    """Run the whole batch through ONE shared browser pool. Returns per-job dicts matching
    crawler_api.app._run: {job_id, summary{...}, documents}. Runs on the process-wide engine loop
    (see _engine_loop), on its own thread, so it never touches FastAPI's anyio threadpool.

    This is the LIVE pool (C1 + C3). Archive jobs never reach it — a Job carrying archive_date
    routes to crawler.c2 instead, so C2 costs no browser and cannot run alongside C1/C3."""
    seed = seed or load_seed()
    kp = kp if kp is not None else get_corpus()   # global keep-gate corpus (built once)
    clear_stop()                                   # fresh batch — clear any stale stop (event + file)
    box: dict = {}

    async def _main() -> None:
        caps = seed.capture_defaults
        # Build the robots cache only if SOME job still respects robots; if every job bypasses
        # (env/flag), skip it entirely — robots_decision stays "off" and no host is ever blocked.
        robots = (RobotsCache(user_agent=caps["user_agent"])
                  if any(_job_respects_robots(j, caps) for j in jobs) else None)
        W = _env_int("CRAWLER_BROWSERS", 8)
        T = _env_int("CRAWLER_TABS_PER_BROWSER", 12)
        # Per-host caps spread the frontier-consumer budget across hosts. In pool mode that
        # budget is CRAWLER_WORKERS_N (tab-less httpx workers), not the tab count — otherwise a
        # small tab pool would throttle httpx per host back down.
        _budget = (_env_int("CRAWLER_WORKERS_N", W * T)
                   if _env_int("CRAWLER_TAB_POOL", 0) else W * T)
        host_conc = _adaptive_host_concurrency(jobs, _budget)
        host = HostLimiter(host_conc,
                           _env_float("CRAWLER_HOST_DELAY", 1.0), robots,
                           base_timeout_s=float(caps.get("timeout_seconds", 30)))
        eng = AsyncEngine(W, T, host, seed, kp, robots)
        # START INSIDE THE GUARD. It used to sit above the try, so a start() that raised
        # skipped shutdown() entirely and orphaned its driver tree — the leak that survived
        # the teardown fix. start() now cleans up after itself as well; this is the braces to
        # that belt, and it costs one line. shutdown() is idempotent (it clears _pw and the
        # browser list), so running it after start() already cleaned up is harmless.
        _progress_reset()                              # zero the live page counter for this run
        try:
            await eng.start()
            ctxs = await eng.run(jobs, forward)
        finally:
            _progress_done()
            await eng.shutdown()
        box["out"] = [{"job_id": c.job.job_id, "summary": c.summary(),
                       # None when forwarding (see CollectingIngestClient._retain_docs): the docs
                       # are already in ingest+Postgres and the caller keeps only counts.
                       "documents": [d["document"] for d in c.ingest.collected
                                     if d["document"] is not None]}
                      for c in ctxs]
        box["host_peak"] = dict(host.peak_inflight)

    # ONE LOOP FOR THE PROCESS, not one per batch. Submitting to a long-lived loop rather than
    # calling asyncio.run() here is what makes a process-wide browser pool possible at all — see
    # _engine_loop. Failure handling is deliberately unchanged: the old code let the exception kill
    # its thread, where threading.excepthook printed it and `box` stayed empty, so the caller got
    # []. Same outcome here, minus the silence.
    try:
        asyncio.run_coroutine_threadsafe(_main(), _engine_loop()).result()
    except BaseException:
        log.exception("batch failed")
    return box.get("out", [])


# ── multi-process sharding (a single Python process is GIL-capped to ~1 core) ─────────────────

def _shard_jobs(jobs: list[Job], n: int) -> list[list[Job]]:
    """Split jobs into n shards, keeping every seed HOST in ONE shard (two workers never crawl the
    same URLs or fight over dedup). Greedy-balance: assign the biggest host-groups to the lightest
    shard so per-worker load is even."""
    from collections import defaultdict
    by_host: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        host = ((urlsplit(j.seed_urls[0]).hostname or "") if j.seed_urls else "") or j.job_id
        by_host[host.lower()].append(j)
    shards: list[list[Job]] = [[] for _ in range(n)]
    for _host, hjobs in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        i = min(range(n), key=lambda k: len(shards[k]))
        shards[i].extend(hjobs)
    return [s for s in shards if s]


def _mp_worker(payload: dict) -> list[dict]:
    """Runs in a CHILD PROCESS (spawn): its OWN GIL, browser pool, event loop and crawl_history DB.
    Returns per-job summaries with documents dropped — they're forwarded to ingest already, and
    shipping full HTML back across the process boundary would be huge."""
    import os
    from . import config as _config
    _config.DB_PATH = _config.DATA_DIR / f"crawl_history_w{payload['worker_id']}.sqlite"
    if payload.get("browsers_per_worker"):
        os.environ["CRAWLER_BROWSERS"] = str(payload["browsers_per_worker"])
    try:
        jobs = [Job(**d) for d in payload["job_dicts"]]
        results = run_batch_async(jobs, forward=payload["forward"],
                                  seed=load_seed(), kp=get_corpus())
    except Exception as exc:
        log.error("mp worker %s failed: %s", payload.get("worker_id"), exc)
        return []
    for r in results:                       # drop documents (already forwarded) → small IPC payload
        r["documents"] = []
    return results


def run_batch_multiprocess(jobs: list[Job], *, forward: bool,
                           seed: Seed | None = None, kp=None) -> list[dict]:
    """Shard the batch across N worker PROCESSES (CRAWLER_WORKERS) so it uses ~N cores instead of the
    ~1 core a single Python process is GIL-capped to. Falls back to the single-process runner for
    1 worker / 1 job. Each worker owns its browser pool + crawl_history DB; all forward to the same
    ingest/MinIO. NOTE: worker results carry summaries only (documents are forwarded, not returned)."""
    workers = _env_int("CRAWLER_WORKERS", 1)
    if workers <= 1 or len(jobs) <= 1:
        return run_batch_async(jobs, forward=forward, seed=seed, kp=kp)
    shards = _shard_jobs(jobs, min(workers, len(jobs)))
    total_browsers = _env_int("CRAWLER_BROWSERS", 8)
    bpw = max(1, total_browsers // len(shards))     # keep total Chromium ≈ CRAWLER_BROWSERS
    payloads = [{"job_dicts": [j.model_dump() for j in shard], "forward": forward,
                 "worker_id": i, "browsers_per_worker": bpw}
                for i, shard in enumerate(shards)]
    log.info("multi-process batch: %d jobs → %d worker processes (%d browsers each)",
             len(jobs), len(shards), bpw)
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(shards)) as pool:
        shard_results = pool.map(_mp_worker, payloads)
    out: list[dict] = []
    for r in shard_results:
        out.extend(r or [])
    return out
