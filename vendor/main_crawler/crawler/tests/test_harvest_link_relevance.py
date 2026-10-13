"""link_relevance_keywords (opt-in): default behavior is unchanged; when set,
irrelevant same-site links are never enqueued."""
from crawler.dedup import CrawlHistory
from crawler.fetcher import Fetcher
from crawler.harvest import harvest
from crawler.seed import load_seed
from crawler.testing_batch import build as build_batch

SEED = load_seed()


def _job(job_id):
    return next(j for j in build_batch() if job_id in j.job_id)


def _run(job):
    fetcher = Fetcher(user_agent=SEED.capture_defaults["user_agent"],
                      delay_s=0, render_js=job.render_js)
    return harvest(job, fetcher, CrawlHistory(":memory:"))


def test_harvest_default_ignores_link_text():
    base_job = _job("LT_news")
    assert base_job.link_relevance_keywords == []
    _, stats = _run(base_job)
    # lt_k9.html has 6 discoverable same-site links; none should be filtered
    # out when link_relevance_keywords is unset (default = crawl everything).
    assert stats.enqueued > 0


def test_harvest_opt_in_filters_irrelevant_links():
    base_job = _job("LT_news")
    unfiltered_job = base_job
    filtered_job = base_job.model_copy(
        update={"link_relevance_keywords": ["k9 thunder"]})

    _, stats_unfiltered = _run(unfiltered_job)
    _, stats_filtered = _run(filtered_job)

    # Only the "Hanwha K9 Thunder exports" anchor overlaps the keyword — far
    # fewer links should be enqueued than the unfiltered crawl.
    assert stats_filtered.enqueued < stats_unfiltered.enqueued
    assert stats_filtered.enqueued > 0
