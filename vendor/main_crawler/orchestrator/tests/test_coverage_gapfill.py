"""Fetching the pages a site publishes but never links to — and the four guardrails that make it
safe to do at all.

The operator refused sitemap seeding the first time, for a good reason: lockheedmartin.com declares
28 sitemaps, one per locale (en-us, en-au, en-gb, en-il, he-il…). Feeding those to the frontier
would pour five extra languages of the same pages into a corpus that had just had 48,278 duplicate
URL records removed. "It can cause confusion" was exactly right.

What makes this version acceptable is that it is NOT sitemap seeding. The crawler engine still
never reads a sitemap — a test in the crawler suite asserts it never imports the module. The
ORCHESTRATOR hands the crawler a list of URLs, which is what the manual-batch box and the scheduler
have always done. The crawl still only follows links on pages it is given.

So the guardrails are the feature, and this file is mostly about them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mallory_orchestrator import api


class _DB:
    def scalars(self, *a, **k):
        class _R:
            def first(self_inner):
                return None
        return _R()

    def close(self):
        pass


@pytest.fixture()
def wired(monkeypatch):
    """Stub the coverage check and capture what would be dispatched."""
    sent: list = []

    def _go(verdict, missed=None, junk=0, master=True, free=(3,)):
        cov = {"ok": True, "verdict": verdict, "sitemap_total": 2263, "covered": 806,
               "crawled_total": 998, "coverage_pct": 36}
        monkeypatch.setattr(api, "data_coverage", lambda site, db: dict(cov, site=site))
        monkeypatch.setattr(api.crawler_client, "coverage",
                            lambda h, missed=25, base=None: dict(
                                cov, missed=list(missed_urls), missed_junk=junk,
                                missed_total=len(missed_urls)))
        missed_urls = missed if missed is not None else [
            "lockheedmartin.com/en-us/capabilities/electronic-warfare/airborne.html",
            "lockheedmartin.com/en-us/capabilities/cyber/intelligence-driven-defense.html"]
        monkeypatch.setattr(api.scheduler, "master_enabled", lambda db: master)
        # The dispatch policy (test_dispatch_policy.py) is read right after the master switch;
        # open here, because the fake db has no Setting store and these cases are about the seeds.
        monkeypatch.setattr(api.scheduler, "lane_open", lambda db, lane: True)
        monkeypatch.setattr(api.scheduler, "free_instances", lambda: list(free))
        monkeypatch.setattr(api.crawler_client, "pool", lambda: ["b"] * 12)
        import mallory_orchestrator.shard as _shard
        monkeypatch.setattr(_shard, "owner_of", lambda db, h, n: 3)
        monkeypatch.setattr(api, "Thread",
                            lambda target, args, daemon=None: type(
                                "T", (), {"start": lambda s: sent.append(args[0])})())
        return sent

    return _go


# ── the guardrails ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict", ["sitemap_partial", "different_urls", "covered", "no_sitemap"])
def test_only_a_real_gap_is_ever_filled(wired, verdict):
    """THE guardrail. `sitemap_partial` means we already hold MORE than the site lists (rtx.com:
    683 listed, 2,018 held) and `different_urls` means the same pages under two addresses
    (leonardo.com: 3,723 listed, 2,869 held, 563 in common). Seeding either fetches pages we
    already have, under a second URL — which is precisely how the duplicates happened."""
    wired(verdict)
    out = api.data_coverage_fill(site="x.example", apply=True, db=_DB())
    assert out["ok"] is False and out["seeds"] == 0
    assert "only 'gaps'" in out["summary"]


def test_a_dry_run_dispatches_nothing(wired):
    sent = wired("gaps")
    out = api.data_coverage_fill(site="lockheedmartin.com", apply=False, db=_DB())
    assert out["seeds"] == 2 and out["applied"] is False
    assert sent == [], "the dry run dispatched a batch"
    assert "nothing dispatched" in out["summary"]


def test_applying_dispatches_exactly_the_missing_urls(wired):
    sent = wired("gaps")
    out = api.data_coverage_fill(site="lockheedmartin.com", apply=True, db=_DB())
    assert len(sent) == 1
    job = sent[0][0]
    assert job["seed_urls"] == [
        "https://lockheedmartin.com/en-us/capabilities/electronic-warfare/airborne.html",
        "https://lockheedmartin.com/en-us/capabilities/cyber/intelligence-driven-defense.html"]
    assert out["applied"] is True


def test_it_follows_nothing(wired):
    """max_depth 0. The seeds ARE the work: their children are in the same sitemap and would be
    seeded too, so depth buys no coverage and only adds pages nobody asked for."""
    sent = wired("gaps")
    api.data_coverage_fill(site="lockheedmartin.com", apply=True, db=_DB())
    job = sent[0][0]
    assert job["max_depth"] == 0
    assert job["max_pages"] == len(job["seed_urls"])
    assert job["same_domain_only"] is True


def test_the_junk_filter_is_the_crawlers_own(wired):
    """A sitemap lists whatever its generator felt like listing, calendar exports included. The
    filtering happens crawler-side against the same `_is_action_link` the enqueue and fetch paths
    use — a second copy in this service would drift from the one that actually matters."""
    wired("gaps", junk=41)
    out = api.data_coverage_fill(site="x.example", apply=False, db=_DB())
    assert out["junk_skipped"] == 41
    src = Path(api.__file__).read_text(encoding="utf-8")
    body = src.split("def data_coverage_fill")[1].split("\n@router")[0]
    # Naming the crawler's predicate in a comment is fine and is the point. IMPLEMENTING one here
    # is not: a pattern in this service drifts from the one the enqueue path enforces, and then the
    # seed list and the frontier disagree about what junk is.
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    for smell in ("re.compile", "?ical", "outlook-ical", "startswith(\"http\") and"):
        assert smell not in code, f"a junk rule ({smell!r}) was copied into the orchestrator"


def test_the_master_switch_still_wins(wired):
    sent = wired("gaps", master=False)
    out = api.data_coverage_fill(site="x.example", apply=True, db=_DB())
    assert out["ok"] is False and sent == []
    assert "master switch" in out["summary"]


def test_an_empty_seed_list_dispatches_nothing(wired):
    sent = wired("gaps", missed=[])
    out = api.data_coverage_fill(site="x.example", apply=True, db=_DB())
    assert out["seeds"] == 0 and sent == []


def test_the_seed_list_is_capped_and_says_so(wired):
    many = ["x.example/p%d" % i for i in range(api.GAPFILL_MAX_SEEDS + 500)]
    wired("gaps", missed=many)
    out = api.data_coverage_fill(site="x.example", apply=False, db=_DB())
    assert out["seeds"] == api.GAPFILL_MAX_SEEDS
    assert out["truncated"] is True, "a silent truncation reads as 'we got everything'"


def test_a_busy_owner_is_reported_not_silently_dropped(wired):
    """The first LIVE run of this said "dispatched 1,457 missing URL(s)" and did nothing at all.
    run_manual_batch routes by domain shard, the crawler refuses a second concurrent batch, and it
    refuses WITHOUT raising — on a daemon thread nobody reads. No JobRun, no log line, no pages, and
    a success message on screen."""
    sent = wired("gaps", free=())            # the owning instance is mid-crawl
    out = api.data_coverage_fill(site="lockheedmartin.com", apply=True, db=_DB())
    assert sent == [], "it dispatched into a busy instance anyway"
    assert out["ok"] is False and out["blocked_by"] == 3
    assert "mid-crawl" in out["summary"]


def test_the_endpoint_is_registered():
    assert "/api/data/coverage/fill" in {r.path for r in api.router.routes}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
