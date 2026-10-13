"""Smoke test: spa_click_through must be a no-op under fixture-mode (the default
test environment), not an accidental behavior change."""
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
                      delay_s=0, render_js=job.render_js,
                      interaction_cfg=job.interaction)
    pages, stats = harvest(job, fetcher, CrawlHistory(":memory:"))
    return fetcher, pages, stats


def test_harvest_click_mode_noop_under_fixtures():
    base_job = _job("LT_news")
    click_job = base_job.model_copy(update={"spa_click_through": True, "render_js": True})

    fetcher_a, pages_a, stats_a = _run(base_job)
    fetcher_b, pages_b, stats_b = _run(click_job)

    assert stats_a.fetched == stats_b.fetched
    assert [p.url for p in pages_a] == [p.url for p in pages_b]
    # close_shared_page() always resets _shared_ctx, whether or not it was ever opened
    assert fetcher_b._shared_ctx is None
