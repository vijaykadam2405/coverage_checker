"""The Flush button deletes everything this machine collected — and nothing it was configured with.

Two properties matter more than the deleting, and both have bitten this system before:

1. **A partial flush must be legible.** Documents deleted while crawl history survives is the ONE
   combination that silently breaks the next crawl: `crawl_pages` still holds every URL's
   content_hash, so every page classifies "unchanged" and nothing is stored. A blanket "ok" after a
   mid-sequence failure hides exactly that, so every step reports its own outcome and one failure
   makes the whole call report not-ok.
2. **Config is not data.** Sources, scheduled jobs and keywords are what the operator BUILT. A test
   reset that destroyed those would mean re-importing a catalog before every run.

Runnable directly:  python tests/test_flush.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api  # noqa: E402


class _Q:
    """Minimal stand-in for db.scalars(...).all() / db.execute / db.commit."""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []
        self.deleted: list = []
        self.committed = False

    def scalars(self, stmt):
        return self

    def all(self):
        return list(self._rows)

    def execute(self, stmt):
        self.deleted.append(stmt)

    def commit(self):
        self.committed = True


class _Job:
    def __init__(self):
        self.last_status, self.last_run_at = "ok", "2026-07-27T00:00:00"


def _patch(*, running=False, fail=(), jobs=None):
    """Swap every outbound call the endpoint makes. `fail` names steps that should raise."""
    import httpx
    saved = {"batch_status": api.crawler_client.batch_status,
             "clear_resumable": api.crawler_client.clear_resumable,
             "httpx_post": httpx.post, "httpx_delete": httpx.delete}
    calls: list[str] = []

    class _R:
        status_code = 200

        def raise_for_status(self): pass

        def json(self): return {"ok": True}

    def _mk(name):
        def go(url, *a, **kw):
            calls.append(f"{name}:{url}")
            if name in fail:
                raise RuntimeError(f"{name} exploded")
            return _R()
        return go

    api.crawler_client.batch_status = lambda: {"running": running}

    def _clear():
        calls.append("resumable")
        if "resumable" in fail:
            raise RuntimeError("resumable exploded")
        return {"jobs": 2, "rows": 40}

    api.crawler_client.clear_resumable = _clear
    httpx.post = _mk("post")
    httpx.delete = _mk("delete")
    return calls, saved


def _restore(saved):
    import httpx
    api.crawler_client.batch_status = saved["batch_status"]
    api.crawler_client.clear_resumable = saved["clear_resumable"]
    httpx.post = saved["httpx_post"]
    httpx.delete = saved["httpx_delete"]


# ── the guard rails ──────────────────────────────────────────────────────────
def t_the_confirm_token_is_required() -> None:
    """Without it a stray POST to this path empties the machine."""
    calls, saved = _patch()
    try:
        for bad in ("", "flush", "DELETE-ALL", "yes"):
            try:
                api.flush_all(confirm=bad, db=_Q())
            except Exception as e:                     # HTTPException
                assert getattr(e, "status_code", None) == 400, e
            else:
                raise AssertionError(f"confirm={bad!r} was accepted")
        assert calls == [], f"something was deleted before the token check: {calls}"
    finally:
        _restore(saved)


def t_a_running_crawl_is_refused_not_raced() -> None:
    """Flushing mid-crawl races an engine that is still writing history rows and frontier
    positions, and leaves a machine cleared in halves nobody can identify."""
    calls, saved = _patch(running=True)
    try:
        try:
            api.flush_all(confirm="FLUSH", db=_Q())
        except Exception as e:
            assert getattr(e, "status_code", None) == 409, e
        else:
            raise AssertionError("flushed while a crawl was running")
        assert calls == [], f"deleted something despite the refusal: {calls}"
    finally:
        _restore(saved)


# ── the happy path ───────────────────────────────────────────────────────────
def t_every_target_is_actually_deleted() -> None:
    db = _Q(rows=[_Job(), _Job()])
    calls, saved = _patch(jobs=db)
    try:
        out = api.flush_all(confirm="FLUSH", db=db)
    finally:
        _restore(saved)
    assert out["ok"] is True, out
    keys = [s["key"] for s in out["steps"]]
    assert keys == ["store", "history", "resumable", "issues", "runlogs"], keys
    assert all(s["ok"] for s in out["steps"]), out["steps"]
    joined = " ".join(calls)
    assert "/sites" in joined, "documents + blobs were never deleted"
    assert "/v1/history/flush" in joined, "content hashes + fetched URLs survived the flush"
    assert "resumable" in joined, "resume state survived"
    assert "/v1/issues/clear" in joined, "issues log survived"
    assert db.committed, "the orchestrator's own log deletion was never committed"


def t_scheduled_jobs_survive_but_their_run_state_is_reset() -> None:
    """The jobs are config and must outlive a flush — but leaving last_status="ok, ran 2h ago" on a
    run whose log and documents are both gone is the dashboard lying about the store."""
    jobs = [_Job(), _Job()]
    db = _Q(rows=jobs)
    calls, saved = _patch()
    try:
        api.flush_all(confirm="FLUSH", db=db)
    finally:
        _restore(saved)
    for j in jobs:
        assert j.last_status == "pending", j.last_status
        assert j.last_run_at is None, j.last_run_at


def t_the_table_the_logs_tab_reads_is_actually_emptied() -> None:
    """The flush said "everything collected on this machine was deleted" and the Logs tab then
    listed every run it had just claimed to delete.

    Both panels there are built from SiteMeasurement — `/logs/runs` entirely, `/logs/sites` for its
    "history" source — and the wipe only ever deleted ScheduleLog and JobRun. Measured on the
    deployed stack right after a real flush: documents 0, blobs 0, job_runs 0, schedule_logs 0,
    site_measurements 2, both panels still populated.

    Deleting two of the three tables is the failure mode this whole file exists to catch: every
    outbound call succeeded, every step reported ok, and the summary was still false.
    """
    db = _Q(rows=[_Job()])
    calls, saved = _patch()
    try:
        out = api.flush_all(confirm="FLUSH", db=db)
    finally:
        _restore(saved)
    wiped = " ".join(str(s).lower() for s in db.deleted)
    for table in ("schedule_logs", "job_runs", "site_measurements"):
        assert table in wiped, (f"the flush never deletes {table}, so it survives a wipe that "
                                f"reports deleting everything. Deleted: {wiped or '(nothing)'}")
    # …and it has to be REPORTED, not just done: the per-step detail is what the operator reads.
    runlogs = next(s for s in out["steps"] if s["key"] == "runlogs")
    assert "site_measurements" in runlogs["detail"], runlogs


def t_the_preview_counts_every_table_the_step_deletes() -> None:
    """A dialog that promises less than the flush performs is the same class of lie as one that
    promises more — the operator approves a number, and the number has to be the real one."""
    import httpx
    saved_get, saved_bs = api._ingest_get, api.crawler_client.batch_status
    api._ingest_get = lambda p: {}
    api.crawler_client.batch_status = lambda: {"running": False}
    try:
        # Three rows come back for EVERY query the preview makes, one per model it must count.
        p = api.flush_preview(db=_Q(rows=[object()]))
    finally:
        api._ingest_get, api.crawler_client.batch_status = saved_get, saved_bs
    assert p["counts"]["runlogs"] == 3, (
        f"runlogs preview counted {p['counts']['runlogs']} of the 3 tables the step deletes — "
        f"ScheduleLog, JobRun and SiteMeasurement")


# ── the property that matters most ───────────────────────────────────────────
def t_a_failed_step_is_reported_not_swallowed() -> None:
    """Documents gone + history intact = the next crawl stores nothing, silently. A flush that
    reported a clean tick here would hand the operator exactly that with no way to see it."""
    db = _Q()
    calls, saved = _patch(fail=("post",))          # history + issues both go via httpx.post
    try:
        out = api.flush_all(confirm="FLUSH", db=db)
    finally:
        _restore(saved)
    assert out["ok"] is False, "a failed step reported success"
    bad = [s for s in out["steps"] if not s["ok"]]
    assert {s["key"] for s in bad} == {"history", "issues"}, out["steps"]
    assert "FAILED" in out["detail"], out["detail"]
    assert "half-cleared" in out["detail"], out["detail"]
    # …and the steps that CAN still run must have run: stopping at the first error would leave even
    # more behind than the failure itself accounts for.
    assert any(s["key"] == "runlogs" and s["ok"] for s in out["steps"]), out["steps"]


def t_the_preview_never_invents_a_count() -> None:
    """"unknown" and "0" mean opposite things to someone about to delete a corpus. A dead service
    must not render as an empty one — the same rule the Live dashboard already follows."""
    import httpx
    saved_get, saved_bs = api._ingest_get, api.crawler_client.batch_status
    saved_res = api.crawler_client.resumable

    def boom(*a, **kw):
        raise RuntimeError("ingest is down")

    api._ingest_get = boom
    api.crawler_client.resumable = boom
    api.crawler_client.batch_status = lambda: {"running": False}
    try:
        p = api.flush_preview(db=_Q())
    finally:
        api._ingest_get, api.crawler_client.batch_status = saved_get, saved_bs
        api.crawler_client.resumable = saved_res
    assert p["counts"].get("documents") is None, p["counts"]
    assert p["counts"].get("documents") != 0, "a dead store rendered as an empty one"
    assert "store" in p["errors"] and "resumable" in p["errors"], p["errors"]
    # The dialog builds its rows from targets[], so a target with no label would render blank.
    assert {t["key"] for t in p["targets"]} == {
        "documents", "blobs", "history", "resumable", "issues", "runlogs"}, p["targets"]
    assert all(t["label"] and t["cost"] for t in p["targets"]), p["targets"]


def t_the_button_is_wired_and_says_what_it_keeps() -> None:
    """A confirm dialog that does not name the blast radius is theatre."""
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnFlush"' in html, "the button is gone"
    assert "/api/control/flush/preview" in html, "the dialog stopped showing real counts"
    assert "/api/control/flush?confirm=FLUSH" in html, "the button no longer sends the token"
    assert 'typed !== "FLUSH"' in html, "the typed confirmation was replaced by a click"
    assert "not touched" in html, "the dialog no longer says the data centre is safe"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} FLUSH TESTS PASSED")


def test_flush_suite() -> None:
    main()


if __name__ == "__main__":
    main()
