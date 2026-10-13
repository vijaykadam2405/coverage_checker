"""Job hunt_mode presets fill only unset knobs (crawler/models.py)."""
from crawler.models import Job


def _job(**kw):
    base = dict(job_id="j", job_type="news", seed_urls=["https://x.com"], keywords=["artillery"])
    base.update(kw)
    return Job(**base)


def test_exhaustive_fills_defaults():
    j = _job(hunt_mode="exhaustive")
    assert j.max_pages == 750 and j.max_depth == 5


def test_focused_fills_defaults():
    j = _job(hunt_mode="focused")
    assert j.max_pages == 60 and j.max_depth == 2
    assert j.skip_irrelevant_seed_links is True
    assert j.link_relevance_keywords == ["artillery"]   # copied from keywords


def test_explicit_values_survive_preset():
    j = _job(hunt_mode="exhaustive", max_pages=100)
    assert j.max_pages == 100          # caller's explicit value wins
    assert j.max_depth == 5            # unset → preset fills


def test_no_hunt_mode_is_unchanged():
    j = _job()
    assert j.max_pages == 40 and j.max_depth == 2   # crawler defaults, untouched
    assert j.hunt_mode is None
