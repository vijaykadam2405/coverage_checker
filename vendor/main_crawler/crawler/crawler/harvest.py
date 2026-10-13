"""Stage 1 — HARVEST. Fetch within the job's budget; capture requested assets.

A BFS frontier expands from ``seed_urls`` up to ``max_depth`` link hops, fetching
at most ``max_pages`` pages, staying on the seed domain when ``same_domain_only``
is set (except following a link OUT from a registry page, allowed within depth).

Conditional re-fetch (§7A): before fetching a URL we look up its stored
validators and send ``If-None-Match`` / ``If-Modified-Since``; a 304 short-
circuits the page (no body, no re-store). Harvest does NOT decide relevance —
that is the gate (Stage 2). It only bounds the crawl and brings back assets.

Opt-in seed-relevance pruning (``job.skip_irrelevant_seed_links``): when a
seed (depth-0) page has zero keyword hits, its links are not enqueued —
saves the crawl budget on dead seeds without changing what the gate itself
decides. Default off; existing jobs are unaffected.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from . import keywords as kwmod
from . import parse
from .canonicalize import canonicalize_url, same_site
from .dedup import CrawlHistory
from .fetcher import FetchResult, Fetcher
from .models import Job


class _AtomicInt:
    """Thread-safe integer for progress tracking across concurrent fetchers."""
    def __init__(self, value: int = 0):
        self._value = value
        self._lock = threading.Lock()

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def inc(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


@dataclass
class HarvestedPage:
    url: str                       # canonical
    depth: int
    fetch: FetchResult
    pdf_links: list[str] = field(default_factory=list)
    image_candidates: list[dict] = field(default_factory=list)
    media_candidates: list[dict] = field(default_factory=list)
    parent_url: str | None = None  # page this was discovered from (None = seed root)
    served_by: str | None = None   # c1_live | c2_wayback | c3_camofox | feed_api
    served_from: str | None = None # archive/source URL when not the live page
    # The snapshot's ORIGINAL capture datetime, for archive tiers only (None = live).
    # Document.archived_at (models.py) and the pgstore column already existed; extract.py's
    # getattr(page, "archived_at", None) already read it — this field is the missing producer.
    archived_at: str | None = None


@dataclass
class HarvestStats:
    fetched: int = 0
    not_modified: int = 0          # 304s — unchanged, skipped before download
    errors: int = 0
    enqueued: int = 0
    seeds_pruned: int = 0          # opt-in: irrelevant seed pages, links not enqueued


def harvest(job: Job, fetcher: Fetcher, history: CrawlHistory | None = None,
            on_fetch: Callable[[dict], None] | None = None
            ) -> tuple[list[HarvestedPage], HarvestStats]:
    """Run the frontier for one job. Returns harvested HTML/PDF pages + stats.

    *on_fetch* is an optional callback receiving a dict with:
      {fetched, errors, not_modified, enqueued, url, depth, status, max_pages}
    Called after each fetch attempt (success or failure).
    """
    stats = HarvestStats()
    pages: list[HarvestedPage] = []

    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for s in job.seed_urls:
        c = canonicalize_url(s)
        if c not in seen:
            seen.add(c)
            queue.append((c, 0))

    seed_domains = {canonicalize_url(s) for s in job.seed_urls}
    _fetched = _AtomicInt()
    _last_tick = time.perf_counter()

    def _notify(status: str) -> None:
        nonlocal _last_tick
        if on_fetch:
            now = time.perf_counter()
            elapsed = now - _last_tick
            _last_tick = now
            on_fetch({"fetched": _fetched.value, "errors": stats.errors,
                      "not_modified": stats.not_modified, "enqueued": stats.enqueued,
                      "url": url, "depth": depth, "status": status,
                      "max_pages": job.max_pages, "elapsed": elapsed})

    # Reuse ONE live browser for the whole job whenever rendering is on — a
    # fresh Chromium launch+teardown per page was the single biggest time sink.
    # The shared page is navigated cold for every URL (same as before, just no
    # relaunch); SPA click-through additionally clicks in-app on same-site links.
    shared = bool(job.render_js) and fetcher.open_shared_page()
    click_mode = bool(job.spa_click_through and shared)

    try:
        while queue and _fetched.value < job.max_pages:
            url, depth = queue.popleft()

            conditional = history.conditional_headers(url) if history else None
            use_click = click_mode and any(same_site(url, sd) for sd in seed_domains)
            res = fetcher.fetch(url, conditional=conditional, click_mode=use_click)

            if res.not_modified:
                stats.not_modified += 1
                _notify("304")
                if history:
                    history.upsert(url, content_hash=None, etag=res.etag,
                                  last_modified=res.last_modified, status=304,
                                  fetched_at=res.fetched_at)
                continue
            if res.error or res.status is None or res.status >= 400:
                stats.errors += 1
                _notify("err")
                continue

            stats.fetched += 1
            _fetched.inc()
            _notify("ok")

            pdf_links: list[str] = []
            image_candidates: list[dict] = []
            media_candidates: list[dict] = []
            if res.is_html() and res.text_html:
                base = res.final_url or url
                # Enqueue child links within depth/domain budget.
                if depth < job.max_depth and _prune_seed_links(job, depth, res):
                    stats.seeds_pruned += 1
                elif depth < job.max_depth:
                    if job.link_relevance_keywords:
                        links = [u for u, t in parse.extract_links_with_text(res.text_html, base)
                                 if _link_is_relevant(t, job.link_relevance_keywords)]
                    else:
                        links = parse.extract_links(res.text_html, base)
                    for link in links:
                        cl = canonicalize_url(link)
                        if cl in seen:
                            continue
                        if job.same_domain_only and not _allowed_offsite(cl, url, seed_domains):
                            continue
                        seen.add(cl)
                        queue.append((cl, depth + 1))
                        stats.enqueued += 1
                # Assets to consider (filtered/kept later by the extractor).
                if "pdf" in job.capture:
                    pdf_links = parse.extract_pdf_links(res.text_html, base)
                if "images" in job.capture:
                    image_candidates = parse.extract_images(res.text_html, base)
                if "media" in job.capture:
                    media_candidates = parse.extract_media_links(res.text_html, base)

            pages.append(HarvestedPage(url=url, depth=depth, fetch=res,
                                       pdf_links=pdf_links, image_candidates=image_candidates,
                                       media_candidates=media_candidates))
    finally:
        if shared:
            fetcher.close_shared_page()

    return pages, stats


def _allowed_offsite(child: str, parent: str, seed_domains: set[str]) -> bool:
    """same_domain_only: stay on the seed domain. The contract allows following
    a link OUT from a registry/seed page within max_depth, so a child of a seed
    URL may leave the domain once; deeper offsite hops are dropped.

    Set CRAWLER_STRICT_DOMAIN=1 to disable the one-hop exception — every link
    must stay on the seed domain (useful for company websites, not portals).

    Anchored to the SEED, never to the parent. Comparing child-vs-parent made scope transitive:
    once the crawl reached any offsite page — via the one-hop exception, or via a deep link that
    redirected cross-domain — every child of that page was "same site as its parent" and the whole
    new domain was in scope. Measured: an rtx.com crawl walked into 105 unrelated hosts / 4,412
    pages (about.google, policies.google.com, about.linkedin.com...). One hop now means one hop."""
    if any(same_site(child, s) for s in seed_domains):
        return True
    if _get_strict_domain():
        return False
    return parent in seed_domains   # one hop out from a seed page is allowed, but it is a LEAF


def _in_path_scope(url: str, scope: "list[str] | None") -> bool:
    """Keep a crawl inside the right SECTION of a host. same_domain_only stops you leaving the
    host; this stops you leaving the subtree you seeded.

    Needed because depth is the wrong lever on hub-shaped sites: seeding
    indianexpress.com/about/defence/ at depth 2 reached the whole paper (measured: 92% of 3,559
    pages were entertainment). A prefix matches on a path-segment boundary, so "/defence" admits
    /defence and /defence/x but NOT /defence-contractors-are-bad-actually.
    Empty/None scope = unrestricted (today's behaviour)."""
    if not scope:
        return True
    try:
        path = urlsplit(url).path or "/"
    except Exception:
        return True                       # unparseable → let the existing guards judge it
    for p in scope:
        p = "/" + (p or "").strip("/")
        if p == "/" or path == p or path.startswith(p + "/"):
            return True
    return False


def _get_strict_domain() -> bool:
    return os.environ.get("CRAWLER_STRICT_DOMAIN", "0") == "1"


def _prune_seed_links(job: Job, depth: int, res: FetchResult) -> bool:
    """Opt-in (job.skip_irrelevant_seed_links): True if this is a seed
    (depth-0) page with zero keyword hits — meaning its links should not
    be enqueued. Uses the same word-boundary FlashText matcher the gate uses,
    over this job's keyword list. Biased toward expanding: any hit, or no
    keywords set, or a non-HTML/no-flag job -> never prunes.

    Uses visible_text (not trafilatura main_text) deliberately — harvest
    must stay cheap, and visible_text is a superset of the article body, so
    a keyword the gate would find in main_text is effectively always
    present here too (the bias is toward expanding, the safe direction)."""
    if not (job.skip_irrelevant_seed_links and depth == 0 and job.keywords):
        return False
    title = parse.title_of(res.text_html) or ""
    text = parse.visible_text(res.text_html)
    return not kwmod.find(kwmod.from_list(job.keywords), title, text)


def _link_is_relevant(anchor_text: str, keywords: list[str]) -> bool:
    """Opt-in link-text relevance (job.link_relevance_keywords): True if any
    keyword appears in the anchor text (case-insensitive substring). This is
    a crawl-budget optimization, not a relevance judgment — the Stage-2 gate
    still evaluates every fetched page regardless of how it was discovered."""
    if not anchor_text:
        return False
    low = anchor_text.lower()
    return any(kw.lower().strip() in low for kw in keywords if kw.strip())
