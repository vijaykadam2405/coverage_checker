"""Deleting a source's data — the only destructive route on the ingest service.

Written from the "what could this wrongly destroy?" side, because every failure here is
unrecoverable:
  * an empty/blank source_id must never become "delete everything"
  * a blob shared with a surviving document must never be removed
  * a failed DELETE must raise, not be swallowed into a cheerful success

Runnable directly:  python tests/test_delete_source.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest_api import pgstore  # noqa: E402


def t_empty_source_is_refused() -> None:
    """The catastrophic input. `DELETE FROM documents WHERE source_id = ''` would match nothing, but
    a future refactor that turned a blank into "no filter" would delete the entire store — so the
    refusal is asserted at the boundary rather than relied upon downstream."""
    for bad in ("", "   ", "\t", None):
        try:
            pgstore.delete_source(bad)                      # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"delete_source({bad!r}) was not refused")


def t_blob_deletion_refuses_paths_outside_the_bucket() -> None:
    """storage_path comes from the DB. If a row ever held an absolute path or a different bucket,
    a naive delete would reach outside the crawler's own storage."""
    refused = 0
    for bad in ("/etc/passwd", "s3://some-other-bucket/x.pdf", "file:///tmp/x", "x.pdf"):
        try:
            pgstore._delete_blob(bad)
        except ValueError as exc:
            assert "refusing" in str(exc)
            refused += 1
            continue
        except (AttributeError, TypeError) as exc:
            # NOT swallowed: a renamed function or a changed signature means the guard below was
            # never reached, and `except Exception: continue` turned that into a silent pass on
            # all four hostile inputs.
            raise AssertionError(
                f"_delete_blob is not callable as expected ({exc!r}) — the guard was never "
                f"exercised, so this test proves nothing") from exc
        except Exception:
            # A MinIO/connection failure is fine: it means the guard let the path through to the
            # client, which is what we are asserting must NOT happen for these inputs.
            raise AssertionError(f"_delete_blob({bad!r}) reached the storage client instead of "
                                 f"being refused")
        raise AssertionError(f"_delete_blob({bad!r}) was not refused")
    assert refused == 4, f"only {refused}/4 hostile paths were refused"


def t_orphan_query_keeps_shared_files() -> None:
    """The SQL that decides which blobs die. A path still referenced by ANY other source must be
    excluded — deleting it would leave that source pointing at a file that no longer exists."""
    import inspect
    src = inspect.getsource(pgstore.delete_source)
    assert "NOT IN (SELECT p FROM kept" in src, "the shared-blob exclusion is gone"
    assert "d.source_id <> %s" in src, "the survivor set no longer excludes the doomed source"
    # and the delete itself must be parameterised, never formatted
    assert "source_id = %s" in src and "source_id = '" not in src


def t_delete_uses_a_real_connection_not_the_swallowing_helper() -> None:
    """pgstore.query() returns [] on ANY exception. Routing a DELETE through it would make a failed
    delete look identical to a successful one — the operator would believe data was removed."""
    import inspect
    src = inspect.getsource(pgstore.delete_source)
    assert "psycopg.connect" in src, "delete no longer opens its own connection"
    assert "conn.commit()" in src, "delete is not committed explicitly"
    assert "pgstore.query(" not in src and "= query(" not in src, \
        "the destructive path went back through the exception-swallowing helper"


def t_preview_is_read_only() -> None:
    """The dialog's numbers must come from a function that cannot modify anything."""
    import inspect
    src = inspect.getsource(pgstore.preview_source).lower()
    for verb in ("delete", "drop", "update ", "insert", "truncate"):
        assert verb not in src, f"preview_source contains a {verb.strip()!r} statement"


def t_no_db_reports_rather_than_pretending() -> None:
    import os
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        assert pgstore.delete_source("example.com") == {"error": "no-db"}, \
            "with no DB configured the delete claimed to have done something"
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def t_history_forget_requires_a_host() -> None:
    """The crawler-side half. A blank host would clear the whole crawl history, so every URL in the
    store would be re-fetched from scratch on the next run."""
    import inspect

    from crawler_api import app as api
    src = inspect.getsource(api.history_forget)
    assert "refusing to forget everything" in src, "the blank-host guard is gone"
    # Host matching must parse the URL, not LIKE on it: 'bmw.com' as a pattern also matches
    # 'notbmw.com' and 'evil.com/?u=bmw.com'.
    assert "urlsplit" in src, "host matching no longer parses the URL"
    assert 'h == want or h.endswith("." + want)' in src, "exact-or-subdomain matching is gone"
    assert "apply" in src, "the dry-run mode is gone"


def t_history_forget_blank_host_returns_400_over_http() -> None:
    """The guard has to work through the ROUTE, not just exist in the source.

    This is here because the source-inspection test above passed while the live endpoint returned
    500: HTTPException was never imported, so the guard raised NameError instead of a 400. An
    assertion about code is not an assertion about behaviour.
    """
    from fastapi.testclient import TestClient

    from crawler_api.app import app
    c = TestClient(app)
    for blank in ("", "%20", "+"):
        r = c.post(f"/v1/history/forget?host={blank}&apply=true")
        assert r.status_code == 400, \
            f"blank host returned {r.status_code}, not 400 — the guard is not reachable"
    # ...and a real host still answers, in dry-run mode, without destroying anything.
    r = c.post("/v1/history/forget?host=example.invalid&apply=false")
    assert r.status_code == 200 and r.json()["applied"] is False


def main() -> None:
    for fn in (t_empty_source_is_refused,
               t_history_forget_blank_host_returns_400_over_http,
               t_blob_deletion_refuses_paths_outside_the_bucket,
               t_orphan_query_keeps_shared_files,
               t_delete_uses_a_real_connection_not_the_swallowing_helper,
               t_preview_is_read_only,
               t_no_db_reports_rather_than_pretending,
               t_history_forget_requires_a_host):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL DELETE-SOURCE GUARD TESTS PASSED")



def test_delete_source_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()
