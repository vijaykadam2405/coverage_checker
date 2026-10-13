"""BFS crawl driven entirely through the C3 CamoFox lane.

Deliberately NOT the main engine: no httpx tier, no Wayback tier, no escalation ladder, no adaptive
render pool. Every page is a stealth browser render. That is ~50x more expensive per page, so this
runs at concurrency 2 with a delay â€” it exists for sites where the cheap tiers get refused, not for
throughput. Reuses the main crawler's C3 client, link parser, canonicaliser and ingest client so a
C3 document lands in Postgres/MinIO identically to a C1 one.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import camofox_client, parse  # noqa: E402
from crawler.canonicalize import canonicalize_url, registered_domain  # noqa: E402
from crawler.dedup import CrawlHistory  # noqa: E402
from crawler.ingest_client import HttpIngestClient  # noqa: E402
from crawler.models import Document, version_id  # noqa: E402

log = logging.getLogger("special_request")

TARGETS_FILE = Path(__file__).with_name("targets.json")

# ONE user agent, used for the direct fetch AND for the robots.txt match â€” so what we tell the
# origin and what we check ourselves against can never disagree.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass
class CrawlStats:
    target: str
    fetched: int = 0
    sent: int = 0
    accepted: int = 0
    failed: int = 0
    skipped_seen: int = 0
    robots_skipped: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def bump_error(self, reason: str) -> None:
        self.errors[reason] = self.errors.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {"target": self.target, "fetched": self.fetched, "sent": self.sent,
                "accepted": self.accepted, "failed": self.failed,
                "skipped_seen": self.skipped_seen, "robots_skipped": self.robots_skipped,
                "errors": self.errors}


def load_targets(path: Path | None = None) -> list[dict]:
    data = json.loads((path or TARGETS_FILE).read_text(encoding="utf-8"))
    return [t for t in data.get("targets", []) if t.get("seed_urls")]


def _now_iso() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def _in_scope(url: str, seed_host: str, path_scope: str | None) -> bool:
    """Same registrable domain, and inside path_scope when one is set.

    registered_domain() is host-exact apart from stripping www., which is what we want: a
    special-request target is one specific site, not a corporate umbrella of subdomains.
    """
    if registered_domain(url) != seed_host:
        return False
    return not path_scope or urlsplit(url).path.startswith(path_scope)


async def _load_robots(seed_host: str, target: dict) -> tuple[RobotFileParser | None, float]:
    """Fetch + parse the origin's robots.txt once per run. Returns (parser, crawl_delay_seconds).

    Parsed with the stdlib RobotFileParser, but fetched with httpx rather than its own urlopen so
    the request carries our real UA and timeout. `respect_robots: false` on a target opts out.

    Fails OPEN (None) when robots.txt is missing/unreachable â€” that is what the standard says (no
    robots.txt = no restrictions), and it keeps a transient 500 on their side from silently halting
    a whole crawl. A robots.txt we DID read is always obeyed.
    """
    if not target.get("respect_robots", True):
        log.warning("robots.txt DISABLED for target %s (respect_robots=false)", target.get("name"))
        return None, 0.0
    import httpx
    url = f"https://{seed_host}/robots.txt"
    try:
        def _go():
            with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                return c.get(url, headers={"User-Agent": _UA})
        r = await asyncio.to_thread(_go)
    except Exception as exc:                           # noqa: BLE001
        log.warning("robots.txt unreachable (%s: %s) â€” proceeding without it", url, exc)
        return None, 0.0
    if r.status_code != 200 or not r.text.strip():
        log.info("no usable robots.txt at %s (status %s) â€” no restrictions", url, r.status_code)
        return None, 0.0
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    delay = rp.crawl_delay(_UA) or rp.crawl_delay("*") or 0.0
    log.info("robots.txt loaded for %s (crawl-delay=%ss)", seed_host, delay or "none")
    return rp, float(delay or 0.0)


def _document(target: dict, url: str, snap: dict, depth: int, parent: str | None) -> Document:
    html = snap.get("html") or ""
    text = snap.get("text") or ""
    body = html or text
    # title/main_text are REQUIRED str on Document â€” never pass None. Without CAMOFOX_API_KEY the
    # /evaluate route returns no HTML, so we fall back to the aria snapshot text and the URL path;
    # a C3 doc with no title is still worth ingesting, a validation error loses the page entirely.
    title = (parse.title_of(html) if html else None) or urlsplit(url).path.strip("/") or "(untitled)"
    # One hash, used for BOTH the stored content_hash and the deterministic id â€” computing it twice
    # would let them drift and silently break the upsert.
    chash = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
    final_url = snap.get("final_url") or url
    return Document(
        url=final_url,
        content_hash=chash,
        fetched_at=_now_iso(),
        source_id=target.get("source_id") or target["name"],
        title=title,
        main_text=text or (parse.visible_text(html) if html else ""),
        html=html,
        depth=depth,
        parent_url=parent,
        # documents.document_id is the NOT NULL primary key. Ingest returns 200 "accepted" even when
        # the Postgres insert then fails, so a missing id silently lands in the ndjson audit trail
        # and nowhere else. Same id shape as extract.build_document: deterministic on
        # (url, content) so an unchanged re-crawl upserts instead of duplicating.
        document_id=version_id(final_url, chash),
        served_by="c1_live" if snap.get("_via") == "direct" else "c3_camofox",
        served_from="live",
    )


async def _render(url: str, target: dict) -> dict | None:
    """One C3 render, off the event loop (camofox_client is synchronous httpx)."""
    timeout_s = float(target.get("timeout_s", 90.0))
    if target.get("solve_captchas"):
        snap, _info = await asyncio.to_thread(
            camofox_client.render_with_solver, url, timeout_s)
        return snap
    return await asyncio.to_thread(camofox_client.render, url, timeout_s, False)


async def _fetch_direct(url: str, target: dict) -> dict | None:
    """Plain HTTPS fallback, shaped like a C3 snapshot.

    C3 stays the primary transport â€” that is the point of this crawler. But measured against
    sipri.org, C3 returned content for ~8% of pages while plain HTTPS returned 200 for most of
    them: their origin is slow and flaky, and a refused render is indistinguishable from a dead
    page. Without this, an unattended overnight run captures almost nothing. Documents fetched
    this way are tagged served_by=c1_live so the two are never confused downstream.
    """
    import httpx

    try:
        def _go():
            with httpx.Client(timeout=float(target.get("timeout_s", 90.0)),
                              follow_redirects=True) as c:
                return c.get(url, headers={"User-Agent": _UA, "Accept": "text/html"})
        r = await asyncio.to_thread(_go)
    except Exception:                                  # noqa: BLE001 â€” fallback must never raise
        return None
    if r.status_code != 200 or not r.text:
        return None
    return {"html": r.text, "text": "", "final_url": str(r.url), "_via": "direct"}


async def crawl_target(target: dict, ingest: HttpIngestClient | None = None,
                       stop: asyncio.Event | None = None) -> CrawlStats:
    """BFS through one target. BFS on purpose: the shallow pages carry the real content, and a
    depth-first walk on a 90s-per-page lane would spend the whole budget in one branch."""
    stats = CrawlStats(target=target.get("name", "?"))
    if not camofox_client.enabled():
        stats.bump_error("camofox_disabled")
        return stats

    ingest = ingest or HttpIngestClient()
    seed_host = registered_domain(target["seed_urls"][0])
    path_scope = target.get("path_scope")
    max_pages = int(target.get("max_pages", 200))
    max_depth = int(target.get("max_depth", 3))
    delay_s = float(target.get("delay_s", 2.0))
    conc = max(1, int(target.get("concurrency", 2)))

    # robots.txt, read once per run. Disallowed URLs are skipped (and counted, never silently), and
    # the origin's crawl-delay RAISES our delay when theirs is slower â€” never lowers it, so a site
    # asking for 10s gets 10s while our own floor still applies to sites that ask for nothing.
    robots, robots_delay = await _load_robots(seed_host, target)
    if robots_delay > delay_s:
        log.info("robots.txt crawl-delay %.1fs exceeds configured %.1fs â€” honoring theirs",
                 robots_delay, delay_s)
        delay_s = robots_delay

    def _allowed(u: str) -> bool:
        if robots is None:
            return True
        try:
            return robots.can_fetch(_UA, u)
        except Exception:                              # noqa: BLE001 â€” a parser hiccup must not halt the crawl
            return True

    # Phase 1: seed from the published sitemap when the target names one, so coverage is complete by
    # construction rather than dependent on BFS reaching every page. Phase 2 is automatic â€” links
    # discovered from these pages catch anything the sitemap omits (orphans, new pages).
    seeds = list(target["seed_urls"])
    sm = target.get("sitemap")
    if sm:
        from . import sitemap
        published = await asyncio.to_thread(
            sitemap.fetch_urls, sm, timeout=float(target.get("timeout_s", 90.0)),
            delay=float(target.get("delay_s", 2.0)), cache=target.get("sitemap_cache"))
        # In scope only, and capped so a runaway sitemap can't blow past the page budget.
        published = [u for u in published if _in_scope(u, seed_host, path_scope)]
        log.info("sitemap seeded %d in-scope urls for %s", len(published), target.get("name"))
        seeds += published

    # Incremental resume. `seen` is in-memory, so without this every restart refetches the whole
    # site â€” on a host that refuses ~80% of connections that is hours of work to re-collect pages we
    # already have, AND it duplicates them: _document mints a random document_id, and pgstore's
    # ON CONFLICT (document_id) can never fire on a fresh uuid, so a refetch always writes a NEW row
    # (measured: 503 sipri.org rows for 431 distinct urls; the homepage stored 6x).
    # So: a URL already fetched in a previous run is skipped, and every success is recorded.
    # Set incremental=false on the target for a full re-collect (e.g. to refresh stale content).
    history = None
    if target.get("incremental", True):
        try:
            history = CrawlHistory()
        except Exception as exc:                       # noqa: BLE001 â€” never block a crawl on history
            log.warning("crawl history unavailable, running non-incremental: %s", exc)

    def _already_done(cu: str) -> bool:
        """True if a previous run fetched this URL successfully (has a content hash)."""
        if history is None:
            return False
        try:
            row = history.get(cu)
            return bool(row and getattr(row, "content_hash", None))
        except Exception:                              # noqa: BLE001
            return False

    seen: set[str] = set()
    frontier: deque[tuple[str, int, str | None]] = deque()
    resumed = 0
    for u in seeds:
        cu = canonicalize_url(u)
        if cu not in seen and _in_scope(cu, seed_host, path_scope):
            seen.add(cu)
            if not _allowed(cu):
                stats.robots_skipped += 1
                log.info("robots.txt disallows %s — not crawling it", cu)
                continue
            if _already_done(cu):
                resumed += 1
                continue                               # collected last run â€” don't pay for it again
            frontier.append((cu, 0, None))
    if resumed:
        log.info("incremental: skipping %d urls already collected; %d queued",
                 resumed, len(frontier))

    sem = asyncio.Semaphore(conc)
    discovered: list[tuple[str, int, str | None]] = []

    async def handle(url: str, depth: int, parent: str | None) -> None:
        async with sem:
            if stop is not None and stop.is_set():
                return
            # C3 refusals are intermittent, not deterministic â€” measured ~38% success on sipri, and
            # CamoFox itself resets connections when the main crawler is loading it. One cheap retry
            # recovers most of that; more than that just queues behind a genuinely blocked page.
            # Transport order is a per-target MEASUREMENT, not a preference. sipri.org serves raw
            # HTML in ~15.5s (example.com: 0.06s); a full C3 render of a page that slow exceeds
            # CamoFox's non-configurable 30s tab-create timeout, so C3-first there fails ~92% of
            # pages. direct_first gets the data and still escalates to C3 when direct is refused â€”
            # which is the case C3 actually exists for.
            primary = _fetch_direct if target.get("transport") == "direct_first" else _render
            secondary = _render if primary is _fetch_direct else _fetch_direct
            snap, last_err = None, "render_empty"
            for attempt in range(1 + max(0, int(target.get("retries", 1)))):
                try:
                    snap = await primary(url, target)
                except Exception as exc:                  # noqa: BLE001 â€” one bad page must not end the run
                    snap, last_err = None, f"render_exc:{type(exc).__name__}"
                await asyncio.sleep(delay_s)              # politeness, inside the permit
                if snap:
                    break
                if attempt == 0:
                    log.info("retrying %s (%s)", url, last_err)
            if not snap and target.get("direct_fallback", True):
                # Primary exhausted its attempts â€” try the other transport before writing the page
                # off. Counted, never silent: if the primary is quietly carrying nothing, the
                # fallback tally is the only thing that reveals it.
                snap = await secondary(url, target)
                if snap:
                    last_err = "fallback_" + ("c3" if secondary is _render else "direct")
                    stats.bump_error(last_err)
            if not snap:
                stats.failed += 1
                stats.bump_error(last_err)                # refused / challenge / dead page
                return
            # Re-check scope AFTER redirects. _in_scope gates links before they are enqueued, but a
            # 30x can carry the fetch off-site, and _document uses final_url â€” which is how a
            # meduza.io page ended up in sipri.documents from a sipri.org seed.
            final = snap.get("final_url") or url
            if not _in_scope(canonicalize_url(final), seed_host, path_scope):
                stats.bump_error("offsite_redirect")
                log.info("dropping off-site redirect %s -> %s", url, final)
                return

            stats.fetched += 1

            doc = _document(target, url, snap, depth, parent)
            stats.sent += 1
            outcome = await asyncio.to_thread(ingest.send, doc)
            # IngestOutcome.accepted â€” NOT .ok. A getattr(..., False) default here silently reported
            # every successful ingest as a failure while the documents landed fine.
            if outcome.accepted:
                stats.accepted += 1
                # Record it so the next run resumes past this page instead of refetching it.
                # Keyed on the REQUESTED url (what the frontier holds), not final_url, or a
                # redirecting page would never match its own history row on the next run.
                if history is not None:
                    try:
                        history.upsert(url, content_hash=doc.content_hash, etag=None,
                                       last_modified=None, status=200,
                                       fetched_at=doc.fetched_at)
                    except Exception as exc:           # noqa: BLE001 â€” history is an optimisation
                        log.debug("history upsert failed for %s: %s", url, exc)
            else:
                stats.bump_error(f"ingest:{outcome.failing_rule}")

            if depth < max_depth and snap.get("html"):
                base = snap.get("final_url") or url
                for link in parse.extract_links(snap["html"], base):
                    cu = canonicalize_url(link, base)
                    if cu in seen or not _in_scope(cu, seed_host, path_scope):
                        continue
                    seen.add(cu)
                    if not _allowed(cu):
                        stats.robots_skipped += 1
                        continue                       # robots.txt Disallow — never enqueue it
                    if _already_done(cu):
                        continue                       # discovered, but collected in an earlier run
                    discovered.append((cu, depth + 1, url))

    while frontier and stats.fetched < max_pages:
        if stop is not None and stop.is_set():
            break
        # One BFS level at a time, `conc` at a time within it.
        wave = [frontier.popleft() for _ in range(min(conc, len(frontier)))]
        discovered.clear()
        await asyncio.gather(*(handle(u, d, p) for u, d, p in wave))
        budget = max_pages - stats.fetched
        frontier.extend(discovered[:budget] if budget > 0 else [])

    stats.skipped_seen = max(0, len(seen) - stats.fetched - len(frontier))
    if history is not None:
        try:
            history.close()
        except Exception:                              # noqa: BLE001
            pass
    return stats


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    only = argv[0] if argv else os.environ.get("SPECIAL_TARGET")

    targets = load_targets()
    if only:
        targets = [t for t in targets if t.get("name") == only]
    if not targets:
        log.error("no matching target (have: %s)",
                  [t.get("name") for t in load_targets()])
        return 2

    if not camofox_client.health():
        log.error("CamoFox is not reachable at %s â€” this crawler is C3-only, nothing to fall back on",
                  camofox_client.base_url())
        return 3

    for t in targets:
        # Env caps so a run can be bounded (or smoke-tested) without editing targets.json.
        for key, env in (("max_pages", "SPECIAL_MAX_PAGES"), ("max_depth", "SPECIAL_MAX_DEPTH")):
            if os.environ.get(env, "").strip():
                t = {**t, key: int(os.environ[env])}
        log.info("special-request crawl: %s (max_pages=%s depth=%s conc=%s)",
                 t.get("name"), t.get("max_pages"), t.get("max_depth"), t.get("concurrency"))
        stats = await crawl_target(t)
        log.info("done: %s", json.dumps(stats.as_dict()))
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
