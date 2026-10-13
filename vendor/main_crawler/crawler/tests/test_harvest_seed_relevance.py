"""skip_irrelevant_seed_links (opt-in): default behavior is unchanged; when
set, a seed (depth-0) page with zero keyword hits does not get its links
enqueued, saving the crawl budget on dead seeds."""
from crawler.dedup import CrawlHistory
from crawler.fetcher import Fetcher
from crawler.harvest import harvest
from crawler.seed import load_seed
from crawler.testing_batch import build as build_batch

SEED = load_seed()

NON_MATCHING_KEYWORDS = ["submarine", "frigate"]


def _job(job_id):
    return next(j for j in build_batch() if job_id in j.job_id)


def _run(job):
    fetcher = Fetcher(user_agent=SEED.capture_defaults["user_agent"],
                      delay_s=0, render_js=job.render_js)
    return harvest(job, fetcher, CrawlHistory(":memory:"))


def test_default_expands_links_even_when_seed_irrelevant():
    base_job = _job("LT_news")
    assert base_job.skip_irrelevant_seed_links is False
    job = base_job.model_copy(update={"keywords": NON_MATCHING_KEYWORDS})
    _, stats = _run(job)
    # flag off -> links still enqueued regardless of seed relevance
    assert stats.enqueued > 0
    assert stats.seeds_pruned == 0


def test_opt_in_prunes_links_when_seed_irrelevant():
    base_job = _job("LT_news")
    job = base_job.model_copy(update={"keywords": NON_MATCHING_KEYWORDS,
                                      "skip_irrelevant_seed_links": True})
    _, stats = _run(job)
    assert stats.enqueued == 0
    assert stats.seeds_pruned == 1
    assert stats.fetched == 1          # only the seed page itself was fetched


def test_opt_in_keeps_links_when_seed_relevant():
    base_job = _job("LT_news")          # real keywords: K9 Vajra, artillery, ...
    default_job = base_job.model_copy(update={"skip_irrelevant_seed_links": False})
    pruning_job = base_job.model_copy(update={"skip_irrelevant_seed_links": True})

    _, stats_default = _run(default_job)
    _, stats_pruning = _run(pruning_job)

    assert stats_pruning.seeds_pruned == 0
    assert stats_pruning.enqueued == stats_default.enqueued
