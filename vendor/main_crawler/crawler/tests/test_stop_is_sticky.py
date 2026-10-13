"""A stop must survive the gap BETWEEN two crawls, not just interrupt the one in flight.

Every runner in this system clears the stop flag on its way in (`run_batch_async` calls
`clear_stop()`, deliberately — a fresh batch must not inherit a stale halt from last week). That is
correct for a batch an operator just asked for, and wrong for every sequential loop built on top of
it, because each iteration of the loop looks like a fresh batch:

  * `_dispatch` runs archive (C2) jobs and live jobs as two SEPARATE phases;
  * the orchestrator's scheduler tick dispatches up to 25 jobs one after another;
  * the dashboard's Run-All-Jobs button loops one `/v1/crawl` per job.

Press stop during any of those and the halt landed on the current job, then the next iteration wiped
the flag and crawling resumed. The operator watched a "stopped" pipeline keep going. This file pins
the two crawler-side halves of that fix; the scheduler half lives in the orchestrator's
tests/test_master_switch.py.

Runnable directly:  python tests/test_stop_is_sticky.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine  # noqa: E402
from crawler_api import app as api  # noqa: E402


def _job(job_id: str, archive: bool = False):
    from crawler.models import Job
    kw = dict(job_id=job_id, job_type="profile", seed_urls=["https://example.com/"])
    if archive:
        kw["archive_date"] = "2020-01-01"
    return Job(**kw)


def _patched(**overrides):
    """Swap out the two runners _dispatch calls, recording which phases actually ran."""
    ran: list[str] = []
    saved = {"run_batch_multiprocess": api.run_batch_multiprocess}
    api.run_batch_multiprocess = lambda jobs, **kw: (ran.append("live"), [])[1]
    for k, v in overrides.items():
        saved[k] = getattr(api, k, None)
        setattr(api, k, v)
    return ran, saved


def _restore(saved):
    for k, v in saved.items():
        setattr(api, k, v)


# ── the archive phase must not hand a running batch to the live phase ────────
def t_a_stop_during_the_archive_phase_cancels_the_live_phase() -> None:
    """The C2 phase blocks for as long as archive.org's rate limit takes. A stop pressed in that
    window used to be erased the moment the live runner started, so a browser pool spun up on a
    crawl the operator had already halted."""
    import crawler.c2 as c2
    real_c2 = c2.run_c2_batch
    ran, saved = _patched()
    c2.run_c2_batch = lambda jobs, **kw: (ran.append("archive"),
                                          async_engine.request_stop(), [])[2]
    try:
        api._dispatch([_job("arch", archive=True), _job("live")], forward=False)
    finally:
        c2.run_c2_batch = real_c2
        _restore(saved)
        async_engine.clear_stop()
    assert ran == ["archive"], f"the live phase ran after a stop: {ran}"


def t_a_stale_stop_file_does_not_silently_swallow_the_next_batch() -> None:
    """Caught live, on a freshly restarted container: /v1/batch/status reported stop_pending=true
    with nothing running and no crawl since boot.

    The stop signal is a FILE, so that child worker processes can see it — which means it outlives
    the process. `_dispatch` refuses to start the live phase while a stop is pending, so a batch of
    ordinary live jobs submitted after a restart would have been skipped IN SILENCE: no error, no
    pages, just "0 jobs". The entry points clear it first; this pins that they do."""
    ran, saved = _patched()
    try:
        async_engine.request_stop()                 # the stale flag, as if left by last week's stop
        assert async_engine.stop_requested(), "premise broken: the flag did not set"
        api.crawl_batch(api.BatchRequest(jobs=[_job("live")], forward_to_ingest=False))
    finally:
        _restore(saved)
        async_engine.clear_stop()
    assert ran == ["live"], "a stale stop file swallowed a brand-new batch"


def t_arming_never_disarms_a_crawl_that_is_already_running() -> None:
    """The single-flight rejection path must not touch the flag. Clearing it there would let a
    second, rejected request cancel the STOP an operator had just pressed on the first."""
    api._batch_status.update(running=True)
    try:
        async_engine.request_stop()
        out = api.crawl_batch(api.BatchRequest(jobs=[_job("x")], background=True))
        assert out.get("started") is False, out
        assert async_engine.stop_requested(), "a rejected request disarmed a live crawl's stop"
    finally:
        api._batch_status.update(running=False)
        async_engine.clear_stop()


def t_without_a_stop_both_phases_still_run() -> None:
    """The negative control. A guard that always fired would pass the test above and quietly break
    every mixed batch in production."""
    import crawler.c2 as c2
    real_c2 = c2.run_c2_batch
    ran, saved = _patched()
    c2.run_c2_batch = lambda jobs, **kw: (ran.append("archive"), [])[1]
    try:
        async_engine.clear_stop()
        api._dispatch([_job("arch", archive=True), _job("live")], forward=False)
    finally:
        c2.run_c2_batch = real_c2
        _restore(saved)
    assert ran == ["archive", "live"], f"a clean batch lost a phase: {ran}"


# ── the flag has to be readable between jobs, not only during one ────────────
def t_batch_status_reports_a_pending_stop_with_nothing_running() -> None:
    """What the Run-All loop reads. /v1/metrics' `stopping` is AND-ed with `running`, so between
    two jobs of the loop it reads false — exactly when the loop needs the answer. This field is the
    raw flag, so a stop from outside the dashboard (the orchestrator's master switch posts straight
    to /v1/crawl/stop) is visible to a tab that never saw the button press."""
    async_engine.clear_stop()
    assert api.batch_status()["stop_pending"] is False
    try:
        async_engine.request_stop()
        st = api.batch_status()
        assert st["stop_pending"] is True, st
        assert not st.get("running"), "premise broken: this asserts on the gap BETWEEN jobs"
    finally:
        async_engine.clear_stop()
    assert api.batch_status()["stop_pending"] is False, "clear_stop did not clear the flag"


def t_the_dashboard_loop_actually_consults_it() -> None:
    """The field is useless if nothing reads it. Pin the wiring: the Run-All loop must call the
    check and must break on it, and the check must be gated to i>0 (a stale flag from an earlier
    stop is still on disk until a crawl starts, and job 0 is the explicit start that clears it)."""
    import re

    from crawler_api import dashboard
    # The RENDERED page, not the .py source. This dashboard's JS lives inside a Python string, so
    # source that looks right can still reach the browser mangled — a single stray backslash gets
    # eaten by Python and takes the whole <script> block with it while the page still returns 200.
    page = dashboard.render()
    blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert blocks, "the dashboard rendered with no <script> block at all"
    js = "\n".join(blocks)
    assert "serverStopPending" in js, "the helper is gone"
    assert "i > 0 && await serverStopPending()" in js, \
        "the Run-All loop no longer consults the server's stop flag between jobs"
    assert "stop_pending" in js, "the helper stopped reading the field"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} STICKY-STOP TESTS PASSED")


def test_stop_is_sticky_suite() -> None:
    """pytest collects `test_*` only and this file uses the repo's `t_*` + main() convention."""
    main()


if __name__ == "__main__":
    main()
