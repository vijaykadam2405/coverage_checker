"""Contract data models — the JOB INPUT (§2) and the DOCUMENT (§3.2).

L1 sends one raw page bundle (the ``Document``) per kept page, carrying
detection tags (stream/competitor/countries/tech domains) as flat
informational fields. It does not construct separately-typed records —
deep record classification (tender/partnership/geo_footprint/innovation/
company_event/competitive_signal) is Layer 2's job, operating on the raw
text + tags handed over here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# --- interaction config (§2.1) -------------------------------------------

class ScrollConfig(BaseModel):
    enabled: bool = False
    mode: str = "viewport"           # "viewport" | "bottom" | "infinite"
    pause_ms: int = 800
    steps: int = 5
    wait_network_idle: bool = True
    network_idle_timeout_ms: int = 5000


class PaginateConfig(BaseModel):
    enabled: bool = False
    next_selector: str = ""          # CSS selector, e.g. "a.next", "[rel=next]"
    max_pages: int = 10
    pause_ms: int = 1000
    wait_network_idle: bool = True


class ClickConfig(BaseModel):
    enabled: bool = False
    selectors: list[str] = Field(default_factory=list)
    pause_ms: int = 500
    wait_network_idle: bool = False


class HoverConfig(BaseModel):
    enabled: bool = False
    selectors: list[str] = Field(default_factory=list)
    pause_ms: int = 300
    click_selectors: list[str] = Field(default_factory=list)


class SearchConfig(BaseModel):
    enabled: bool = False
    input_selector: str = ""
    submit_selector: str = ""
    keywords_to_search: list[str] = Field(default_factory=list)
    pause_ms: int = 1000


class InteractionConfig(BaseModel):
    scroll: ScrollConfig | None = None
    paginate: PaginateConfig | None = None
    click: ClickConfig | None = None
    hover: HoverConfig | None = None
    search: SearchConfig | None = None


# --- enums (kept as Literals so the Ingest API can validate) -------------
JobType = Literal["news", "tender", "profile", "spec", "patent_aux"]
CaptureType = Literal["html", "text", "images", "screenshot", "pdf", "media", "js"]


# --- JOB INPUT (§2) ------------------------------------------------------
class Job(BaseModel):
    job_id: str
    job_type: JobType
    seed_urls: list[str]
    keywords: list[str] = Field(default_factory=list)
    # Keyword gate switch (distinct from `keywords`, the list). True (default) = capture only
    # pages that hit >=1 corpus keyword (per-page filter). False = skip keyword matching entirely
    # and capture the WHOLE site (every fetched page, still bounded by max_pages/max_depth).
    keyword: bool = True
    # NOTE: max_pages <= 0 means UNLIMITED — crawl the WHOLE site (every page, bounded only by the
    # frontier draining + engine wall-clock). Use it with a high max_depth for "fetch all 15K pages
    # first, filter later" runs. Positive = a hard page budget (default below).
    target_entity: Optional[str] = None          # watchlist id, or null for tenders
    max_pages: int = 40
    # max_depth < 0 = UNLIMITED depth (follow links as deep as the site goes). 0 = seed pages only.
    max_depth: int = 2
    same_domain_only: bool = True
    # Restrict the crawl to URL paths under these prefixes (e.g. ["/defence", "/about/defence"]).
    # same_domain_only keeps you on the HOST; this keeps you in the right SECTION of it. Needed for
    # hub-shaped sites: seeding indianexpress.com/about/defence/ at depth 2 otherwise reaches the
    # whole paper's related-article graph (measured: 92% of 3,559 pages were entertainment).
    # Empty = no path restriction (today's behaviour). Seed URLs are always allowed.
    path_scope: list[str] = Field(default_factory=list)
    # RESUME: pick this job up from its durable frontier instead of re-seeding.
    #
    # A crawl that is interrupted — operator stop, container restart, network drop — otherwise
    # starts again from seed_urls and re-walks the entire site. Content-hash dedup keeps the
    # duplicates out of the corpus, so the DATA is never wrong; what is lost is TIME, which on a
    # large site is hours of re-fetching pages we already have. With resume=True the engine loads
    # the URLs that were discovered-but-not-yet-processed (crawl_frontier state='pending') and the
    # ones already processed (state='done', loaded into `seen` so they are not queued again).
    #
    # False (default) starts fresh AND clears any stale frontier for this job_id, so a normal
    # re-crawl cannot silently inherit a half-finished one.
    resume: bool = False
    render_js: bool = False
    # SPA click-through (§4): for same-site links, click the discovered <a href>
    # in-app on a shared live page instead of a cold page.goto(), for sites whose
    # server only serves working content via client-side routing. Requires render_js.
    spa_click_through: bool = False
    freshness_days: Optional[int] = None          # ignore content older than this
    capture: list[CaptureType] = Field(default_factory=lambda: ["html", "text", "screenshot"])
    # Opt-in link-text relevance (§harvest): when non-empty, only same-site
    # links whose anchor text overlaps one of these keywords get enqueued.
    # Empty (default) = crawl every same-site link, same as today.
    link_relevance_keywords: list[str] = Field(default_factory=list)
    # HARD topical requirement, AND-ed with the corpus gate: when non-empty, a page is kept only
    # if its title+text hits >=1 of these. Distinct from `keywords` (which the gate ignores — it
    # matches the global corpus) and from link_relevance_keywords (anchor text, budget only).
    # This is the one that cannot be satisfied by a listing page mentioning nothing relevant.
    # Auto-filled with the defence terms for job_type="tender" — see _apply_tender_profile.
    require_keywords: list[str] = Field(default_factory=list)
    # Registered-account crawling: log in (registering via a disposable inbox first, if there is
    # no stored account) before crawling, and replay the session cookies on every fetch.
    # None = auto: on for the gated tender portals, off everywhere else. See crawler.siteauth.
    auth: Optional[bool] = None
    # Opt-in seed-relevance pruning: when True, a seed (depth-0) page whose
    # title+visible text contains ZERO job keywords does not get its links
    # enqueued — saves the crawl budget on dead seeds. The page itself still
    # flows to the gate (which will drop it); this only stops BFS expansion.
    skip_irrelevant_seed_links: bool = False

    # Optional source identity stamped by the orchestrator's Source Catalog.
    # When present these are used VERBATIM (the crawler does not re-derive them).
    source_id: Optional[str] = None
    source_tier: Optional[int] = None
    source_type: Optional[str] = None
    source_region: Optional[str] = None

    # Hunt mode presets — fill only knobs left at default (see _apply_hunt_mode):
    #   "exhaustive" = deep dig (750 pages, depth 5) for meaty defense-caves;
    #   "focused"    = probe-then-crawl (small budget, shallow, relevance-pruned).
    hunt_mode: Optional[Literal["exhaustive", "focused"]] = None
    # Careful mode ("quiet quiet"): force per-host concurrency 1 + slow delay + no UA disguise
    # even for non-.gov hosts. None = auto by hostname suffix (CRAWLER_CAREFUL_HOSTS).
    careful: Optional[bool] = None
    # Robots policy override: None = env CRAWLER_RESPECT_ROBOTS / seed default; True/False forces
    # per-job. False bypasses robots (fetch every page directly, no robots→C3 detour) — robust but
    # not polite; use for sites where robots over-blocks and coverage matters more than etiquette.
    respect_robots: Optional[bool] = None
    # "Advanced -> Proceed anyway", the honest version. EMPTY (the default) = the crawler never
    # disables TLS verification, for anyone. A hostname listed here (bare host, lowercase, no
    # scheme, no port; a leading www. is ignored) may be re-fetched ONCE with verification off,
    # and ONLY after that host has already produced a real certificate error on a fully-verified
    # attempt. Per-HOST rather than per-job for the same reason a browser stores its exception
    # per-origin: a job that must reach one cert-broken supplier portal must not thereby trust
    # every other host it happens to crawl. Pages fetched this way are stamped
    # served_by="c1_live_tls_unverified", and the internal-address guard still refuses regardless
    # of what is listed here.
    tls_insecure_hosts: list[str] = Field(default_factory=list)
    # Click-discovery: after loading a page, CLICK every non-<a href> clickable (buttons, onclick,
    # SPA router links) to surface JS-driven navigation, follow same-domain targets, go back.
    # Requires render_js. OFF by default (it's slow — up to CRAWLER_MAX_CLICKS_PER_PAGE nav/page).
    click_discovery: bool = False
    # Per-page attachment cap override. None = env CRAWLER_MAX_PDFS_PER_PAGE (default 3); a positive
    # int caps at that many; 0 (or negative) = UNLIMITED — capture EVERY Download link (RFP PDF) on a
    # page. Set 0 for tender/archive listings so a whole table of RFPs is never truncated.
    max_pdfs_per_page: Optional[int] = None
    # Incremental recrawl: consult crawl_history and send conditional GETs
    # (If-None-Match/If-Modified-Since); unchanged pages return 304 and are SKIPPED without
    # re-rendering — only new/changed pages are fetched + re-ingested. OFF = full re-render each run.
    incremental: bool = False

    # Page interaction steps for JS rendering (§2.1).
    interaction: InteractionConfig | None = None

    # --- C2, the ARCHIVE tier -------------------------------------------------------------
    # The PRESENCE of archive_date makes this an ARCHIVE job: it routes to crawler.c2 and NO live
    # fetch happens (no C1, no C3). Exclusivity is STRUCTURAL, not a flag — a C2 job never reaches
    # AsyncEngine, so C1/C3 cannot run for it even by mistake.
    #   archive_date alone        -> "state as of that day": the nearest capture at or before its
    #                                END. One document per url.
    #   archive_date + archive_to -> the state when archive_date OPENED, plus every CHANGED version
    #                                through the end of archive_to.
    # Calendar days, "YYYY-MM-DD", ALWAYS UTC — every Wayback timestamp is UTC and an archive date is
    # a day, not an instant, so there is nothing to convert.
    # ponytail: no time-of-day. Add it when someone needs sub-day precision on a 2015 capture; the
    # window would just stop widening to 000000/235959 and nothing else changes.
    archive_date: Optional[str] = None
    archive_to: Optional[str] = None

    def is_c2(self) -> bool:
        """This job is served by the archive tier. The router dispatches on this, not on a flag."""
        return bool(self.archive_date)

    @model_validator(mode="after")
    def _check_archive(self) -> "Job":
        """Reject an incoherent archive job AT THE API BOUNDARY.

        This matters precisely BECAUSE CDX silently ignores params it cannot parse (a made-up param
        returns HTTP 200 and normal results), so a malformed `to=` would quietly widen a 3-week ask
        into the site's whole history with no signal anywhere.
        """
        if self.archive_to and not self.archive_date:
            raise ValueError("archive_to needs archive_date (it is the range start)")
        for f in ("archive_date", "archive_to"):
            v = getattr(self, f)
            if v is not None:
                # Round-trip, because BOTH obvious parsers are too lenient to trust on their own:
                # date.fromisoformat accepts "20240701" (3.11+), and strptime accepts un-padded
                # "2024-7-1". Either would sail through and then build a malformed CDX bound
                # ("202471"), which CDX ignores silently — widening the window with no signal.
                # strftime normalises, so anything not already exact YYYY-MM-DD fails here.
                try:
                    ok = datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d") == v
                except (ValueError, TypeError):
                    ok = False
                if not ok:
                    raise ValueError(f"{f} must be a UTC calendar day YYYY-MM-DD, got {v!r}")
        if self.archive_to and self.archive_to < self.archive_date:  # ISO days sort as strings
            raise ValueError(f"archive_to {self.archive_to} precedes archive_date {self.archive_date}")
        if not self.is_c2():
            return self

        # HARD REJECT, not a silent fix. gate.evaluate runs passes_freshness on the page's OWN
        # published_at BEFORE the keyword match (gate.py:43), against now()-N. EVERY generated job
        # carries freshness_days=120/180 (jobgen.py), so an archive read of July 2024 would drop
        # EVERY document as "stale_beyond_freshness_days" and report zero under a plausible-looking
        # reason. That is the default path, not an edge case. The archive window IS the temporal
        # filter; two temporal filters is incoherent.
        if self.freshness_days is not None:
            raise ValueError("freshness_days is meaningless in archive mode and would drop every "
                             "archived page (gate.py passes_freshness measures against now(), not "
                             "the snapshot date) — the archive_date window IS the filter")
        # Unlimited is UNLIMITED HOURS here. The archive index IS the frontier, and archive.org caps
        # us near 24 req/min: sipri.org is ~2,521 CDX index pages ≈ 21min just to ENUMERATE, before
        # one page body is fetched. Worse, a 429 ignored earns a 1-hour IP ban that DOUBLES per
        # violation — on a shared egress IP that is an outage for everyone. C1 may roam unbounded
        # (models.py above); C2 may not.
        if self.max_pages <= 0:
            raise ValueError("archive mode needs an explicit max_pages > 0: the archive index is the "
                             "frontier and archive.org rate-limits to ~24 req/min, so an unlimited "
                             "run takes hours and risks an IP ban")
        # capture defaults to ["html","text","screenshot"] (above) — NO 'pdf'. enrich_assets is gated
        # on `"pdf" in job.capture` (extract.py), so on a default job the whole archived-PDF path
        # would be dead code. The archive tier is specced for text+html+PDF, so default it right when
        # the caller didn't say. model_fields_set = the _apply_hunt_mode idiom: a default may fill a
        # gap, never override a caller.
        if "capture" not in self.model_fields_set:
            self.capture = ["html", "text", "pdf"]
        return self

    @model_validator(mode="after")
    def _apply_hunt_mode(self) -> "Job":
        """Expand hunt_mode into concrete knobs, but NEVER override a value the caller set
        explicitly (model_fields_set) — presets are defaults, not mandates."""
        if self.hunt_mode is None:
            return self
        s = self.model_fields_set
        if self.hunt_mode == "exhaustive":
            if "max_pages" not in s:
                self.max_pages = 750
            if "max_depth" not in s:
                self.max_depth = 5
        else:  # "focused" — probe-then-crawl: small, shallow, relevance-pruned
            if "max_pages" not in s:
                self.max_pages = 60
            if "max_depth" not in s:
                self.max_depth = 2
            if "skip_irrelevant_seed_links" not in s:
                self.skip_irrelevant_seed_links = True
            if "link_relevance_keywords" not in s and self.keywords:
                self.link_relevance_keywords = list(self.keywords)
        return self

    @model_validator(mode="after")
    def _apply_tender_profile(self) -> "Job":
        """A tender job means a DEFENCE tender job — wire the defence scoping automatically.

        The four Indian aggregators list every public tender in the country; the defence slice is
        a fraction of a percent. Requiring the operator to remember ~90 keywords, the login flag
        and the attachment cap on every job they generate is how a crawl quietly comes back full
        of road-works listings. So the profile is the DEFAULT for job_type="tender", and — same
        rule as _apply_hunt_mode — it only fills knobs the caller left alone. Set any of these
        explicitly (``require_keywords=[]``, ``auth=False``) to opt out per job.

        Deliberately NOT touched: max_pages/max_depth. How much of a portal to walk is a budget
        decision that has nothing to do with which slice of it is interesting.
        """
        if self.job_type != "tender":
            return self
        from . import tenderdefense
        s = self.model_fields_set
        if "require_keywords" not in s:
            self.require_keywords = list(tenderdefense.TERMS)
        if "link_relevance_keywords" not in s:
            # On these portals the tender's TITLE is the anchor text, so this decides relevance
            # before a fetch is spent. Pagination links ("2", "Next") are exempted at enqueue
            # time — see _enqueue_links — or the filter would sever the listing chain itself.
            self.link_relevance_keywords = list(tenderdefense.TERMS)
        if "max_pdfs_per_page" not in s:
            # A tender IS its attachment set (NIT, BoQ, corrigenda). The default cap of 3 silently
            # truncates a listing that links a whole table of documents. 0 = unlimited.
            self.max_pdfs_per_page = 0
        if "auth" not in s and any(tenderdefense.is_gated_portal(u) for u in self.seed_urls):
            self.auth = True
        return self


def version_id(url: str, content_hash: str | None) -> str:
    """Deterministic document_id: same URL + same content => same id, so a re-crawl that found
    nothing new UPSERTS in place instead of adding a row. Different content => different id => a
    NEW row, so real revisions are still kept as history.

    Replaces `"doc_" + uuid4()`, which was random per fetch: pgstore upserts
    ON CONFLICT (document_id), and a conflict on a fresh uuid can never fire, so EVERY re-crawl
    wrote a duplicate (measured 2026-07-21: 2,547 duplicate rows over 1,946 urls, one page 13x).
    Only 236 of those groups were byte-identical — the rest were genuine revisions, which is why
    the id keys on content too rather than on url alone."""
    import hashlib
    key = f"{url}\x00{content_hash or ''}".encode("utf-8", "replace")
    return "doc_" + hashlib.sha256(key).hexdigest()[:16]


# --- DOCUMENT sub-object (§3.2) — L1 captures TEXT + HTML + PDF only -------
class Attachment(BaseModel):
    url: str
    storage_path: Optional[str] = None
    type: str = "pdf"
    extracted_text: Optional[str] = None


class Document(BaseModel):
    """Layer-1 PURE CAPTURE dump — one bundle per kept page: raw text + html + asset refs, keyed by
    url + depth + timestamp + tier. NO analysis: source classification, entity resolution, summary,
    tables, translation, and language detection are all Layer 2's job, run on the raw main_text/html."""
    url: str                                  # canonical, dedup key, REQUIRED
    content_hash: str                         # sha256 of main_text (or file bytes), REQUIRED
    fetched_at: str                           # when WE captured it (ISO), REQUIRED
    source_id: str                            # bare registered domain (L2 re-resolves the real source)
    title: str                                # REQUIRED
    published_at: Optional[str] = None        # the page's own date if its meta declares one
    language: str = "en"                      # raw <html lang> hint (no detection); "und" if unknown
    main_text: str                            # cleaned body text dump, REQUIRED
    html: str = ""                            # raw source HTML dump
    attachments: list[Attachment] = Field(default_factory=list)   # pdf blobs (storage_path in MinIO)

    # Crawl provenance: which depth this page was scraped at (0 = seed) and the
    # page it was first discovered from (None = seed root).
    depth: Optional[int] = None
    parent_url: Optional[str] = None

    # Which tier served this page: c1_live (normal fetch/render), c2_wayback / c2_memento (archives),
    # c3_camofox (stealth render), feed_api (RSS/DVIDS). served_from = the archive/source URL when it
    # isn't the live page. archived_at = the snapshot's original capture datetime (memento) for archive
    # tiers only; null for live.
    served_by: Optional[str] = None
    served_from: Optional[str] = None
    archived_at: Optional[str] = None

    # This bundle's own id (idempotency/logging). One bundle per kept page.
    document_id: Optional[str] = None
    # Batch lineage: the crawl run that produced this row (one id per engine run / C2 batch). Lets L2
    # answer "what did run N add" without bucketing by ingested_at windows.
    crawl_run_id: Optional[str] = None

    # C3 / captcha solver provenance.
    captcha_type: Optional[str] = None          # e.g. recaptcha_v2, hcaptcha, turnstile, datadome
    captcha_solved: bool = False                # True when a captcha was solved to retrieve this page
    captcha_solver: Optional[str] = None        # buster, capsolver, 2captcha, anticaptcha, ...
    captcha_solver_cost_usd: Optional[float] = None  # reported commercial solver spend
    proxy_country: Optional[str] = None         # geo-targeted proxy country used for this page
