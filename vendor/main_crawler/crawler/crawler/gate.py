"""Stage 2 — FILTER (the mechanical keyword-relevance gate, §3).

Keep a page if its title+main_text matches >=1 keyword from the global corpus
(``keywords.load_corpus`` — a FlashText trie over the user's CSV). That's it: the
keyword match is what says "this page is relevant to the crawl". This is
mechanical, not strategic — "does this page mention something we're watching?",
never "is this a threat?" (that is Layer 2).

Freshness is also enforced here: content published older than ``freshness_days``
is dropped (when a published date is known; unknown dates are kept, never
fabricated). An empty/absent corpus fails OPEN (keep-all) so a misconfigured
keyword file can't silently zero the output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import keywords
from .models import Job


@dataclass
class GateResult:
    keep: bool
    reason: str
    matched_keywords: list[str]


def passes_freshness(published_iso: str | None, freshness_days: int | None) -> bool:
    if not freshness_days or not published_iso:
        return True   # no limit, or unknown date -> keep (never fabricate a drop)
    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
        return pub >= cutoff
    except Exception:
        return True


def evaluate(job: Job, title: str, main_text: str,
             published_iso: str | None, kp) -> GateResult:
    if not passes_freshness(published_iso, job.freshness_days):
        return GateResult(False, "stale_beyond_freshness_days", [])

    # HARD topical requirement (job.require_keywords), checked BEFORE the keyword=False escape
    # hatch and before the empty-corpus fail-open — both of those are "keep everything" paths, and
    # a defence-only tender crawl must not keep everything. On the aggregator portals the listing
    # and pagination pages we are obliged to walk mention no defence term at all; this is what
    # stops them entering the corpus. Empty list (the default) = no requirement, unchanged.
    if job.require_keywords:
        req = keywords.find(keywords.from_list(job.require_keywords), title, main_text)
        if not req:
            return GateResult(False, "no_required_keyword", [])
        # A match here REPLACES the corpus gate rather than adding to it. The corpus is the
        # watchlist — companies we track — and a genuine Ordnance Factory ammunition tender names
        # none of them, so AND-ing the two would drop exactly the pages this mode exists to find.
        # Corpus hits are still recorded when present, so a tender that DOES name a watchlist
        # company stays identifiable downstream.
        corpus_hits = keywords.find(kp, title, main_text) if kp is not None and len(kp) else []
        return GateResult(True, "required_keyword_match", list(dict.fromkeys(req + corpus_hits)))

    # Per-job switch: keyword=False disables keyword matching entirely -> keep every page
    # (crawl the whole site). Freshness above still applies. getattr keeps old callers/tests
    # that build a Job without the field working (defaults to filtering on).
    if not getattr(job, "keyword", True):
        return GateResult(True, "keyword_filter_off", [])

    # Empty corpus -> fail open (keep-all) so a bad CRAWLER_KEYWORDS_FILE path
    # doesn't silently drop every page. len() on a KeywordProcessor is the trie size.
    if kp is None or len(kp) == 0:
        return GateResult(True, "no_corpus_keep_all", [])

    hits = keywords.find(kp, title, main_text)
    if not hits:
        return GateResult(False, "no_keyword_match", hits)
    return GateResult(True, "keyword_match", hits)
