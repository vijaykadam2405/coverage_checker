"""Pushing the local store up to the data centre.

The centre has no HTTP ingest API: it is the server's Postgres + MinIO, reached over the SSH tunnel
(db-tunnel:5432 / :9000). The crawler normally writes into that Postgres directly; when the tunnel
is down a run lands locally instead of being lost, and this is the re-sync — a store-to-store copy
of the same schema.

Two properties carry the whole feature:
  * the slice is ONE remote transaction, and `pushed_at` is stamped only after it commits. Stamping
    optimistically would mark documents delivered that the centre never got, and the watermark
    would then hide them forever.
  * the DSN holds a password; the label that reaches the dashboard must not.

Runnable directly:  python tests/test_push_centre.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest_api import live, pgstore  # noqa: E402

# Captured BEFORE any monkeypatching: the source-inspection tests must read the real functions, not
# whichever stub a previous test left in their place.
_REAL = {"mark_pushed": pgstore.mark_pushed, "unpushed": pgstore.unpushed,
         "has_db": pgstore.has_db, "push_backlog": pgstore.push_backlog,
         "DC_DSN": live.DC_DSN, "DC_MINIO": live.DC_MINIO, "copy": live._copy_blobs}

DSN = "postgresql://mallory:sup3rs3cr3t@db-tunnel:5432/mallory"


class _Cur:
    def __init__(self, fail): self._fail = fail; self.rows = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def executemany(self, sql, rows):
        if self._fail:
            raise ConnectionError("tunnel down")
        self.rows.extend(rows)
    def execute(self, sql, p=None): pass
    def fetchone(self): return (0,)


class _Conn:
    def __init__(self, fail, committed): self._fail = fail; self._c = committed; self.cur = _Cur(fail)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self.cur
    def commit(self): self._c.append(len(self.cur.rows))


def _patch(*, fail=False, batch=None, stamped=None, committed=None):
    batch = batch if batch is not None else _docs(3)
    live.DC_DSN, live.DC_MINIO = DSN, ""          # blobs off unless a test wants them
    live._copy_blobs = lambda docs: (0, [])
    pgstore.has_db = lambda: True
    pgstore.unpushed = lambda limit=200: list(batch)
    pgstore.push_backlog = lambda: {"pending": len(batch) - len(stamped)}

    def _mark(ids):
        stamped.extend(ids)
        return len(ids)
    pgstore.mark_pushed = _mark

    import psycopg
    psycopg.connect = lambda *a, **k: _Conn(fail, committed)
    return batch


def _docs(n):
    return [{"document_id": f"d{i}", "url": f"https://x.test/{i}", "title": "t",
             "source_id": "x.test", "served_by": "c1_live", "main_text": "m", "html": "<p>",
             "attachments": None, "ingested_at": "2026-07-22T00:00:00Z", "html_len": 3,
             "crawl_run_id": "r1"} for i in range(n)]


def t_a_failed_transaction_stamps_nothing() -> None:
    """THE test. If the remote insert throws, not one document may be marked pushed — otherwise the
    watermark says delivered and they are never re-sent."""
    stamped, committed = [], []
    _patch(fail=True, batch=_docs(5), stamped=stamped, committed=committed)
    out = live.push_to_centre(limit=10)
    assert stamped == [], f"stamped {stamped} after the remote transaction failed"
    assert committed == [], "commit ran despite the failure"
    assert out["accepted"] == 0 and out["failed"] == 5
    assert out["errors"], "the failure was not reported"


def t_a_committed_slice_is_stamped_exactly_once() -> None:
    stamped, committed = [], []
    _patch(batch=_docs(4), stamped=stamped, committed=committed)
    out = live.push_to_centre(limit=10)
    assert committed == [4], f"expected one commit of 4 rows, got {committed}"
    assert sorted(stamped) == ["d0", "d1", "d2", "d3"]
    assert out["accepted"] == 4 and out["sent"] == 4


def t_nothing_pending_is_not_an_error() -> None:
    stamped, committed = [], []
    _patch(batch=[], stamped=stamped, committed=committed)
    out = live.push_to_centre(limit=10)
    assert out["sent"] == 0 and committed == []
    assert "nothing pending" in out["message"]


def t_unconfigured_centre_is_refused_not_guessed() -> None:
    """With no DSN a push would either do nothing silently or hit whatever default was compiled in."""
    from fastapi import HTTPException
    stamped, committed = [], []
    _patch(batch=_docs(1), stamped=stamped, committed=committed)
    live.DC_DSN = ""
    try:
        live.push_to_centre(limit=10)
    except HTTPException as e:
        assert e.status_code == 400 and "void" in e.detail
        assert committed == [] and stamped == []
        return
    raise AssertionError("an unconfigured data centre was not refused")


def t_the_dashboard_label_never_leaks_the_password() -> None:
    """_dc_label() is rendered into the page and into the strip tooltip. The DSN it is built from
    carries the production Postgres password."""
    live.DC_DSN = DSN
    label = live._dc_label()
    assert "sup3rs3cr3t" not in label, f"password leaked into the dashboard: {label}"
    assert "mallory:" not in label, f"credentials leaked into the dashboard: {label}"
    assert "db-tunnel:5432" in label, f"label lost the host it is meant to show: {label}"
    live.DC_DSN = ""
    assert live._dc_label() == "", "an unconfigured centre produced a label"


def t_blobs_go_before_documents() -> None:
    """A document row whose PDF upload failed leaves the centre with a reference to a file only this
    machine holds — worse than a missing attachment, because nothing downstream can tell."""
    import inspect
    src = inspect.getsource(live.push_to_centre)
    i_blob, i_insert = src.index("_copy_blobs"), src.index("INSERT INTO documents")
    assert i_blob < i_insert, "documents are inserted before their blobs are copied"


def t_blob_copy_skips_what_the_centre_already_has() -> None:
    """A re-push after a partial run must not re-upload gigabytes over an SSH tunnel."""
    import inspect
    src = inspect.getsource(_REAL["copy"])
    assert "stat_object" in src and "continue" in src, "the already-present check is gone"


def t_insert_is_parameterised_and_column_explicit() -> None:
    import inspect
    src = inspect.getsource(live.push_to_centre)
    assert "ON CONFLICT (document_id) DO NOTHING" in src, "a re-push would duplicate rows"
    assert "executemany(sql, rows)" in src and "% d" not in src
    # Explicit column list, not SELECT * / *: a new local column must not silently break every
    # insert against a server that has not been migrated yet.
    assert "_PUSH_COLS" in src and "SELECT *" not in src
    assert live._PUSH_COLS[0] == "document_id" and "attachments" in live._PUSH_COLS


def main() -> None:
    try:
        for fn in (t_a_failed_transaction_stamps_nothing,
                   t_a_committed_slice_is_stamped_exactly_once,
                   t_nothing_pending_is_not_an_error,
                   t_unconfigured_centre_is_refused_not_guessed,
                   t_the_dashboard_label_never_leaks_the_password,
                   t_blobs_go_before_documents,
                   t_blob_copy_skips_what_the_centre_already_has,
                   t_insert_is_parameterised_and_column_explicit):
            fn()
            print(f"  ok  {fn.__name__}")
    finally:
        pgstore.unpushed, pgstore.has_db = _REAL["unpushed"], _REAL["has_db"]
        pgstore.push_backlog, pgstore.mark_pushed = _REAL["push_backlog"], _REAL["mark_pushed"]
        live.DC_DSN, live.DC_MINIO, live._copy_blobs = _REAL["DC_DSN"], _REAL["DC_MINIO"], _REAL["copy"]
    print("ALL PUSH-TO-CENTRE TESTS PASSED")



def test_push_centre_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
