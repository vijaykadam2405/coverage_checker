"""The C2 (archive tier) Job contract.

Each rejection here guards a way the run would otherwise SUCCEED while producing wrong/no data:
  freshness_days -> every archived doc dropped as "stale" under a plausible reason (gate.py
                    measures against now(), not the snapshot date)
  max_pages<=0   -> an hours-long scan that can earn a 1-hour archive.org IP ban (doubling)
  bad date       -> CDX silently ignores params it can't parse, quietly widening the window
"""
import pytest
from pydantic import ValidationError

from crawler.extract import pdf_budget
from crawler.harvest import HarvestedPage
from crawler.models import Job


def _job(**kw):
    base = dict(job_id="a", job_type="profile", seed_urls=["https://sipri.org/"])
    return Job(**{**base, **kw})


# --- the mode switch is the DATE, not a flag ------------------------------
def test_archive_date_is_the_mode_switch():
    assert not _job().is_c2()                                  # a normal C1/C3 job
    assert _job(archive_date="2024-07-01").is_c2()
    assert _job(archive_date="2024-07-01", archive_to="2024-07-23").is_c2()


def test_a_plain_job_is_untouched_by_the_archive_validator():
    # C1's defaults must not drift: _check_archive returns at the first guard for a non-C2 job.
    j = _job(freshness_days=120, max_pages=0)                  # both illegal for C2, fine for C1
    assert j.capture == ["html", "text", "screenshot"]         # NOT defaulted to pdf
    assert j.max_pages == 0 and j.freshness_days == 120        # 0 = unlimited stays legal for C1


# --- date validation ------------------------------------------------------
@pytest.mark.parametrize("bad", ["20240701", "2024-7-1", "01-07-2024", "July 1 2024", "", "2024-13-01"])
def test_malformed_date_rejected(bad):
    # "20240701" is the important one: date.fromisoformat accepts it on 3.11+, strptime does not.
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        _job(archive_date=bad)


def test_archive_to_needs_a_start():
    with pytest.raises(ValidationError, match="needs archive_date"):
        _job(archive_to="2024-07-23")


def test_backwards_range_rejected():
    with pytest.raises(ValidationError, match="precedes"):
        _job(archive_date="2024-07-23", archive_to="2024-07-01")


def test_single_day_range_is_legal():
    assert _job(archive_date="2024-07-01", archive_to="2024-07-01").is_c2()


# --- the two rejections that stop a silent-wrong-data run -----------------
def test_freshness_days_rejected_in_archive_mode():
    # jobgen stamps freshness_days=120/180 on EVERY generated job, so this is the DEFAULT path:
    # without the reject, an archive read of 2024 drops every doc as stale_beyond_freshness_days.
    with pytest.raises(ValidationError, match="freshness_days is meaningless"):
        _job(archive_date="2024-07-01", freshness_days=120)


@pytest.mark.parametrize("n", [0, -1])
def test_unlimited_max_pages_rejected_in_archive_mode(n):
    with pytest.raises(ValidationError, match="max_pages > 0"):
        _job(archive_date="2024-07-01", max_pages=n)


def test_explicit_max_pages_is_honoured():
    assert _job(archive_date="2024-07-01", max_pages=50).max_pages == 50


# --- capture defaulting ---------------------------------------------------
def test_capture_defaults_to_pdf_for_archive_jobs():
    # The default ["html","text","screenshot"] carries no "pdf", and enrich_assets gates on it —
    # so without this the whole archived-PDF path would be dead code on a default C2 job.
    assert _job(archive_date="2024-07-01").capture == ["html", "text", "pdf"]


def test_capture_default_never_overrides_the_caller():
    j = _job(archive_date="2024-07-01", capture=["html", "text"])
    assert j.capture == ["html", "text"]                       # model_fields_set idiom respected


def test_hunt_mode_still_applies_after_the_archive_validator():
    # Validator ORDER: _check_archive runs before _apply_hunt_mode (definition order). max_pages
    # defaults to 40 (>0) so the archive guard passes, then hunt_mode may still widen it.
    j = _job(archive_date="2024-07-01", hunt_mode="exhaustive")
    assert j.max_pages == 750 and j.is_c2()


# --- the previously-dead archived_at wire ---------------------------------
def test_harvested_page_carries_archived_at():
    # extract.py's getattr(page, "archived_at", None) has always read this; nothing ever set it.
    assert HarvestedPage(url="u", depth=0, fetch=None).archived_at is None
    assert HarvestedPage(url="u", depth=0, fetch=None,
                         archived_at="2024-07-01T00:00:00Z").archived_at == "2024-07-01T00:00:00Z"


# --- pdf_budget: <=0 means UNLIMITED (the [:n or None] trap) --------------
@pytest.mark.parametrize("n,expect", [(3, 3), (0, 0), (-1, -1), (None, 3)])
def test_pdf_budget_passes_through(n, expect, monkeypatch):
    monkeypatch.delenv("CRAWLER_MAX_PDFS_PER_PAGE", raising=False)
    assert pdf_budget(_job(max_pdfs_per_page=n)) == expect


def test_pdf_budget_negative_must_not_be_sliced_naively():
    # Guards the real bug: links[:n or None] silently DROPS THE LAST LINK for any negative n
    # (only 0 is caught by `or None`). Callers must use the `if n <= 0` form instead.
    links = ["a.pdf", "b.pdf", "c.pdf"]
    n = pdf_budget(_job(archive_date="2024-07-01", max_pages=5, max_pdfs_per_page=-1))
    assert links[:n or None] == ["a.pdf", "b.pdf"]             # the trap, demonstrated
    assert (links if n <= 0 else links[:n]) == links           # the correct form
