"""A refused resume must not report success.

THE BUG THIS PINS. `crawl_batch` refuses when a previous batch still owns the engine and RETURNS
{"started": False, ...} rather than raising. `resume_crawl` listed every job in `resumed` anyway, so
a refusal was indistinguishable from a restart.

The consequence was not a cosmetic wrong count. `_arm()` — the only thing that deletes the
`.crawl_stop` FILE — runs inside `crawl_batch` AFTER the single-flight check, so a refused resume
never clears it. The file outlives the batch, so when the wedged teardown finally ended, the instance
sat at running=false / stop_pending=true holding real pending URLs, while the fleet reported
"671 resumed, 0 errors". Observed on four separate stop/resume cycles.

Runnable directly:  python tests/test_resume_refusal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from crawler_api import app as appmod  # noqa: E402


class _FakeHistory:
    """Two resumable jobs, both with a stored config."""

    def job_state(self, jid):
        # job_type is required by the Job model — a stored config always carries it.
        return {"job_json": '{"job_id": "%s", "job_type": "profile", '
                            '"seed_urls": ["https://acme.com/"]}' % jid}

    def frontier_counts(self, jid):
        return {"pending": 42, "done": 7}

    def close(self):
        pass


def _patch(monkey_batch):
    import crawler.dedup as dedup
    dedup.CrawlHistory = _FakeHistory                    # type: ignore[assignment]
    appmod.crawl_batch = monkey_batch                    # type: ignore[assignment]


def test_a_refused_resume_raises_instead_of_claiming_success():
    _patch(lambda req: {"started": False, "error": "a batch is already running"})
    try:
        appmod.resume_crawl(job_ids=["j1"], background=True, forward_to_ingest=True)
    except HTTPException as e:
        assert e.status_code == 409, e.status_code
        assert "already running" in str(e.detail), e.detail
        return
    raise AssertionError("a refused resume returned success — this is the bug")


def test_a_successful_background_resume_still_reports_resumed():
    _patch(lambda req: {"started": True, "background": True, "total": len(req.jobs)})
    out = appmod.resume_crawl(job_ids=["j1", "j2"], background=True, forward_to_ingest=True)
    assert out["resumed"] == ["j1", "j2"], out
    assert out["started"] is True


def test_a_SYNCHRONOUS_resume_is_not_mistaken_for_a_refusal():
    """background=False returns {"jobs": n, "results": [...]} with NO `started` key. Treating a
    missing key as False would break every synchronous caller — including the orchestrator's manual
    batch, which sends background:false."""
    _patch(lambda req: {"jobs": 1, "results": [{"job_id": "j1", "summary": {"fetched": 3}}]})
    out = appmod.resume_crawl(job_ids=["j1"], background=False, forward_to_ingest=True)
    assert out["resumed"] == ["j1"], out


def test_an_unknown_job_still_raises_400_not_409():
    """A job with no stored config is a different failure and must stay distinguishable."""
    class _Empty(_FakeHistory):
        def job_state(self, jid):
            return None

    import crawler.dedup as dedup
    dedup.CrawlHistory = _Empty                          # type: ignore[assignment]
    appmod.crawl_batch = lambda req: {"started": True}    # type: ignore[assignment]
    try:
        appmod.resume_crawl(job_ids=["nope"], background=True, forward_to_ingest=True)
    except HTTPException as e:
        assert e.status_code == 400, f"expected 400 for a missing config, got {e.status_code}"
        return
    raise AssertionError("a resume with no resumable config should fail")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} RESUME-REFUSAL TESTS PASSED")
