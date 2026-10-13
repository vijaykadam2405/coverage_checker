"""The five buckets the operator works in, and the one function that decides which one a job is in.

The internal taxonomy has ten names (`trade_press`, `gov_primary`, `defence_org`, …) and the
crawler has five JOB TYPES (`news`, `tender`, `profile`, `spec`, `patent_aux`). Neither is what an
operator thinks in. Every screen that offers a choice offers Manufacture / News / Tender / Gov
portal / Think tank, and until now the Control panel's stop switches were the exception — they were
labelled with the crawler's job types, so the panel asked you to stop "profile" when what you
wanted to stop was Manufacture.

WHY THIS CANNOT JUST BE RENAMED: the mapping is many-to-one. News, Gov portal AND Think tank all
generate `job_type: "news"`, so a switch keyed on job_type that is labelled "Gov portal" would also
silently stop News and Think tank. A bucket switch has to key on the SOURCE's category, which the
job carries as `source_type` (stamped by jobgen._stamp) and which the catalog knows by `source_id`.

Lives in its own module because api, scheduler and orchestrate all need it, and api already imports
the other two — putting it in any of them makes an import cycle.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Source

# bucket key -> (raw catalog categories that file into it, the crawler job_type it generates)
UI_CATEGORIES: dict[str, tuple[set[str] | None, str]] = {
    # "all" carries no job_type of its own: a None category set routes through CAT_TO_JOBTYPE so
    # each site gets the type ITS category implies. The second element is unread for this key.
    "all":         (None, ""),
    "news":        ({"trade_press", "business_press", "aggregator", "blog_forum_social"}, "news"),
    "manufacture": ({"manufacturer_ir"}, "profile"),
    "tender":      ({"tender_portal"}, "tender"),
    "gov_portal":  ({"gov_primary"}, "news"),
    "think_tank":  ({"think_tank", "defence_org"}, "news"),
    # General news: mainstream, non-defence press - BBC, Reuters, AP, CNN, Al Jazeera.
    #
    # Its OWN bucket rather than more rows inside News, because the two need separate switches.
    # News is the defence trade press, a few hundred sites that are the point of the corpus;
    # General news is ~700 mainstream outlets that mention defence occasionally. Stopping the
    # second must not stop the first, and one bucket cannot do that.
    #
    # It generates `news`, the same shape as the trade press: freshness-gated, page-capped,
    # keyword-filtered. This is load-bearing. A `profile` job is a whole-site walk with no
    # freshness gate, and bbc.com is millions of pages - that crawl would never finish and would
    # hold an instance for ever. Whatever else changes here, general news is never a profile crawl.
    #
    # NOT the same as `unknown`, which stays out of every bucket on purpose: unknown means nobody
    # has classified the source, so ui_category() shows it under its own raw name rather than
    # inventing a claim about it.
    "general":     ({"general"}, "news"),
}

BUCKET_LABEL = {"news": "News", "manufacture": "Manufacture", "tender": "Tender",
                "gov_portal": "Gov portal", "think_tank": "Think tank",
                "general": "General news"}

# The switchable vocabulary, in the order every other screen lists it.
# General news sorts LAST: it is the broadest and least specific bucket, and listing it first
# invites it to be picked by default, which would quietly stop sites from being classified as the
# defence trade press they actually are.
BUCKETS = ("manufacture", "news", "tender", "think_tank", "gov_portal", "general")

# A job we cannot place. NOT a sixth bucket to hide things in: it exists so an unplaceable job is
# VISIBLE and switchable rather than quietly running under a label that does not describe it.
UNCATALOGUED = "uncatalogued"

CAT_TO_BUCKET = {c: k for k, (cats, _) in UI_CATEGORIES.items() if cats for c in cats}
CAT_TO_JOBTYPE = {c: jt for cats, jt in UI_CATEGORIES.values() if cats for c in cats}


def label(bucket: str) -> str:
    return BUCKET_LABEL.get(bucket, bucket)


def ui_category(category: str | None) -> str:
    """Raw catalog category → the bucket LABEL the dashboard shows.

    A category outside the taxonomy keeps its own name instead of being forced into a bucket:
    "unknown" means nobody has classified this source, and rendering that as "News" would be
    inventing a fact about it — the same rule the Live tiles follow for a number nobody measured.
    """
    if not category:
        return ""
    return BUCKET_LABEL.get(CAT_TO_BUCKET.get(category, ""), category)


def catalog_buckets(db: Session) -> dict[str, str]:
    """source_id/domain (lowercased, both forms) → bucket key. The fallback lookup for a job that
    was generated before source_type was stamped on it."""
    out: dict[str, str] = {}
    for s in db.scalars(select(Source)).all():
        b = CAT_TO_BUCKET.get(s.category or "")
        if not b:
            continue
        for k in (s.domain, s.source_id):
            if k:
                out[str(k).lower()] = b
    return out


def bucket_of_job(job: dict | None, by_source: dict[str, str] | None = None) -> str:
    """Which switch governs this job. Never guesses: a job whose source category is unknown comes
    back as UNCATALOGUED so it shows up on the panel under its own row, instead of being folded
    into whichever bucket happens to share its job_type."""
    job = job or {}
    b = CAT_TO_BUCKET.get(str(job.get("source_type") or ""))
    if b:
        return b
    sid = str(job.get("source_id") or "").lower()
    if sid and by_source:
        b = by_source.get(sid) or by_source.get(sid.removeprefix("www."))
        if b:
            return b
    return UNCATALOGUED
