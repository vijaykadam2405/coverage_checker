"""Coverage-complete job generation from the Source Catalog × the seed.

Fixes every gap in the crawler's first-cut jobgen: uses ALL news sources (not 6), ALL tracked
competitors (incl. P3), folds product names into the query, sweeps ALL target countries for
tenders, and stamps source_id/source_tier/source_type on every job. The matrix is a deterministic
function of (catalog × seed), so the coverage ledger can prove nothing is missed.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .models import Source
from .seed import Seed

# `general` (mainstream press) belongs here: it is crawled with the same freshness-gated news
# shape as the trade press. It is a separate BUCKET so the operator can stop it independently,
# not a separate crawl shape.
NEWS_CATEGORIES = {"trade_press", "business_press", "aggregator", "think_tank", "gov_primary",
                   "defence_org", "general"}


def job_type_for(category: str | None) -> str:
    """Catalogue CATEGORY -> the crawler's job_type. They are different vocabularies.

    The crawler validates job_type against a closed set (news/tender/profile/spec/patent_aux) and
    422s anything else, so passing a category straight through produces a job that is silently
    never crawled. That is not hypothetical: all 508 enrolled re-crawl jobs were written with
    job_type "manufacturer_ir" and every one would have failed on its first tick — they were all
    still `pending`, dated six days out, so nothing had surfaced it yet.

    One function because there are now three callers (job generation, schedule enrolment, and the
    idle-instance dispatch) and the rule was previously inline in only one of them.
    """
    if category == "tender_portal":
        return "tender"
    if category in NEWS_CATEGORIES:
        return "news"
    return "spec"          # manufacturer_ir and anything unrecognised

_MAXP = {"news": 30, "tender": 60, "spec": 10, "profile": 25}
_DEPTH = {"news": 2, "tender": 2, "spec": 1, "profile": 2}
# html + text + pdf is the floor for EVERY job type; the extras on top are per-type.
#
# `profile` used to be html+text only, and profile is what every manufacturer source generates —
# so a defence manufacturer's brochures, datasheets and spec sheets were the one thing the crawl
# did not collect. It only showed up because the live-sites panel put a PDF column next to a page
# count: 97 of 149 sites had PDFs and the seven being crawled at that moment had none. The ones
# with PDFs were dispatched as `manufacture` (which carried pdf through the UI's bucket default);
# the ones without were dispatched as `all`, which fell back to this table and lost it.
#
# A default that depends on WHICH NAME the operator picked for the same sites is not a default.
# PDFs are the highest-value artefact on a manufacturer site, so they are no longer optional.
_CAPTURE = {
    "news": ["html", "text", "pdf", "images", "screenshot"],
    "tender": ["html", "text", "pdf", "screenshot"],
    "spec": ["html", "text", "pdf", "images", "screenshot"],
    "profile": ["html", "text", "pdf"],
}
_EXPECT = {
    "news": ["competitive_signal", "company_event"],
    "tender": ["tender"],
    "spec": ["competitive_signal"],
    "profile": ["partnership", "geo_footprint"],
}


def _stamp(job: dict, s: Source) -> dict:
    job["source_id"] = s.source_id
    job["source_tier"] = s.tier
    job["source_type"] = s.category
    if s.region:
        job["source_region"] = s.region
    return job


def _base(job_type: str, jid: str, seed_urls: list[str], keywords: list[str], entity: str | None) -> dict:
    return {
        "job_id": jid,
        "job_type": job_type,
        "seed_urls": seed_urls,
        "keywords": keywords,
        "target_entity": entity,
        "max_pages": _MAXP[job_type],
        "max_depth": _DEPTH[job_type],
        "same_domain_only": True,
        "render_js": False,
        "freshness_days": 120,
        "capture": _CAPTURE[job_type],
        "expected_record_types": _EXPECT[job_type],
        "forward_to_ingest": True,
    }


def _news_keywords(seed: Seed, eid: str) -> list[str]:
    e = seed.entities[eid]
    kws = list(dict.fromkeys([e.name, *e.aliases]))[:4]
    kws += seed.products_by_owner.get(eid, [])[:4]  # fold product names in
    return kws[:8]


def generate(sources: list[Source], seed: Seed) -> list[dict]:
    jobs: list[dict] = []
    n = 0

    news_sources = [s for s in sources if s.category in NEWS_CATEGORIES and s.search_template]
    for s in news_sources:
        for eid, e in seed.entities.items():  # ALL competitors, incl. P3 — coverage-complete
            n += 1
            q = quote_plus(f"{(e.aliases[0] if e.aliases else e.name)} defence")
            jobs.append(_stamp(
                _base("news", f"job_news_{eid}_{s.source_id}_{n}",
                      [s.search_template.format(q=q)], _news_keywords(seed, eid), eid), s))

    tender_sources = [s for s in sources if s.category == "tender_portal"]
    for s in tender_sources:
        for country in (seed.countries or [None]):  # sweep ALL target countries
            n += 1
            kws = list(seed.tender_keywords[:12]) + ([country] if country else [])
            url = s.search_template.format(q=quote_plus(country or "tender")) if s.search_template else (s.seed_urls or [""])[0]
            jobs.append(_stamp(_base("tender", f"job_tender_{s.source_id}_{n}", [url], kws, None), s))

    # Direct-target sources (e.g. the offline end-to-end test) — one job per explicit URL.
    for s in sources:
        if not s.seed_urls or s.category == "tender_portal":
            continue
        jt = job_type_for(s.category)
        for url in s.seed_urls:
            n += 1
            jobs.append(_stamp(_base(jt, f"job_direct_{s.source_id}_{n}", [url], [], None), s))

    return jobs
