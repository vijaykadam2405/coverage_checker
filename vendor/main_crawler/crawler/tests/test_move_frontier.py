"""Moving a job's durable state between instances.

This tool writes to live crawl databases, so the tests that matter are the destructive ones: does
the seen-set travel (or does the target re-crawl everything), and can a half-finished copy ever be
followed by a delete.

Runnable directly:  python tests/test_move_frontier.py
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(data_root: str):
    """Import move_frontier with DATA_ROOT pointed at a throwaway tree (it is read at import)."""
    import os
    os.environ["CRAWLER_DATA_ROOT"] = data_root
    spec = importlib.util.spec_from_file_location("move_frontier", SCRIPTS / "move_frontier.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SCHEMA = """
CREATE TABLE crawl_frontier (job_id TEXT, url TEXT, depth INT, parent_url TEXT,
                             needs_render INT, state TEXT, queued_at TEXT);
CREATE TABLE crawl_job_state (job_id TEXT PRIMARY KEY, pages_done INT, status TEXT,
                              started_at TEXT, updated_at TEXT, job_json TEXT);
CREATE TABLE crawl_pages (canonical_url TEXT PRIMARY KEY, content_hash TEXT, etag TEXT,
                          last_modified TEXT, js_heavy INT, last_status INT, last_seen TEXT,
                          times_seen INT, error_category TEXT, fail_count INT, failed_at TEXT,
                          crawl_run_id TEXT, error_detail TEXT);
CREATE TABLE host_render (host TEXT PRIMARY KEY, always INT);
CREATE TABLE host_careful (host TEXT PRIMARY KEY, careful INT);
"""


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.commit()
    return c


def _fixture():
    """Instance 0 holds a job for acme.com with 3 pending URLs and 2 already-seen pages.
    Instance 7 (dir i8) is empty."""
    root = Path(tempfile.mkdtemp(prefix="mv_"))
    src = _make_db(root / "crawl_history.sqlite")
    job = json.dumps({"job_id": "j1", "seed_urls": ["https://www.acme.com/"]})
    src.execute("INSERT INTO crawl_job_state VALUES ('j1', 7, 'stopped', '', '', ?)", (job,))
    for i in range(3):
        src.execute("INSERT INTO crawl_frontier VALUES ('j1', ?, 1, '', 0, 'pending', '')",
                    (f"https://www.acme.com/p{i}",))
    for i in range(2):
        src.execute("INSERT INTO crawl_pages (canonical_url, content_hash, times_seen) VALUES (?,?,1)",
                    (f"https://www.acme.com/seen{i}", f"h{i}"))
    # a DIFFERENT job on the same instance, which must be left completely alone
    src.execute("INSERT INTO crawl_job_state VALUES ('j2', 1, 'stopped', '', '', ?)",
                (json.dumps({"job_id": "j2", "seed_urls": ["https://other.com/"]}),))
    src.execute("INSERT INTO crawl_frontier VALUES ('j2', 'https://other.com/x', 1, '', 0, 'pending', '')")
    src.execute("INSERT INTO host_render VALUES ('www.acme.com', 1)")
    src.execute("INSERT INTO host_careful VALUES ('www.acme.com', 1)")
    src.commit()
    src.close()
    _make_db(root / "i8" / "crawl_history.sqlite").close()
    return root


def test_the_pending_urls_move():
    root = _fixture()
    m = _load(str(root))
    r = m.move("j1", 0, 7, apply=True)
    assert r.get("ok"), r
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    assert dst.execute("select count(*) from crawl_frontier where job_id='j1'").fetchone()[0] == 3
    assert dst.execute("select count(*) from crawl_job_state where job_id='j1'").fetchone()[0] == 1
    src = sqlite3.connect(root / "crawl_history.sqlite")
    assert src.execute("select count(*) from crawl_frontier where job_id='j1'").fetchone()[0] == 0, \
        "the source kept the frontier — both instances would now crawl it"


def test_the_SEEN_SET_moves():
    """The expensive mistake. Without crawl_pages the target re-fetches every page the source already
    stored, so a 'move' silently doubles the work and the corpus."""
    root = _fixture()
    m = _load(str(root))
    m.move("j1", 0, 7, apply=True)
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    seen = dst.execute("select count(*) from crawl_pages where canonical_url like '%acme.com%'").fetchone()[0]
    assert seen == 2, f"the already-seen pages did not travel (got {seen}) — the target will re-crawl them"


def test_learned_host_hints_move():
    root = _fixture()
    m = _load(str(root))
    m.move("j1", 0, 7, apply=True)
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    assert dst.execute("select count(*) from host_render where host='www.acme.com'").fetchone()[0] == 1
    assert dst.execute("select count(*) from host_careful where host='www.acme.com'").fetchone()[0] == 1, \
        "a host learned to be fragile must stay fragile after the move"


def test_other_jobs_on_the_same_instance_are_untouched():
    root = _fixture()
    m = _load(str(root))
    m.move("j1", 0, 7, apply=True)
    src = sqlite3.connect(root / "crawl_history.sqlite")
    assert src.execute("select count(*) from crawl_job_state where job_id='j2'").fetchone()[0] == 1
    assert src.execute("select count(*) from crawl_frontier where job_id='j2'").fetchone()[0] == 1
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    assert dst.execute("select count(*) from crawl_job_state where job_id='j2'").fetchone()[0] == 0, \
        "an unrelated job was dragged along"


def test_a_failed_copy_never_deletes_the_source():
    """Verify-before-delete. If the copy is short and we delete anyway, the job is gone for good."""
    root = _fixture()
    m = _load(str(root))
    real_move = m.move

    # Make the target unwritable for crawl_frontier by dropping the table, so the INSERT fails.
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    dst.execute("DROP TABLE crawl_frontier")
    dst.commit()
    dst.close()

    try:
        real_move("j1", 0, 7, apply=True)
    except sqlite3.Error:
        pass                      # a hard failure is fine; losing the job is not
    src = sqlite3.connect(root / "crawl_history.sqlite")
    assert src.execute("select count(*) from crawl_frontier where job_id='j1'").fetchone()[0] == 3, \
        "the source frontier was deleted after a failed copy — the job is unrecoverable"
    assert src.execute("select count(*) from crawl_job_state where job_id='j1'").fetchone()[0] == 1


def test_a_move_is_idempotent():
    """Re-running must not duplicate pending URLs — INSERT OR REPLACE, and the source is already empty."""
    root = _fixture()
    m = _load(str(root))
    m.move("j1", 0, 7, apply=True)
    again = m.move("j1", 0, 7, apply=True)
    assert "error" in again, "a second move of an already-moved job should report no such job"
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    assert dst.execute("select count(*) from crawl_frontier where job_id='j1'").fetchone()[0] == 3


def test_plan_refuses_to_strip_a_source_bare():
    """The point is to fill idle instances, not to shuffle. A plan that empties a busy instance is
    just the same imbalance mirrored."""
    m = _load(tempfile.mkdtemp())
    state = {
        0: {"pending": 100_000, "jobs": {f"j{i}": 10_000 for i in range(10)}},
        1: {"pending": 0, "jobs": {}},
    }
    moves = m.plan_moves(state, reserve=2000)
    assert moves, "nothing planned despite a 100k-to-0 imbalance"
    # simulate and check we did not overshoot into the mirror image
    p = {0: 100_000, 1: 0}
    for jid, s, d in moves:
        p[s] -= 10_000
        p[d] += 10_000
    assert p[0] >= 2000, f"source stripped to {p[0]}, below the reserve"
    assert abs(p[0] - p[1]) < 100_000, "the imbalance was not reduced"


def test_plan_does_nothing_when_already_balanced():
    m = _load(tempfile.mkdtemp())
    state = {0: {"pending": 1000, "jobs": {"a": 1000}}, 1: {"pending": 900, "jobs": {"b": 900}}}
    assert m.plan_moves(state) == [], "moved work despite a 100-URL difference"


def test_job_hosts_covers_www_and_apex():
    m = _load(tempfile.mkdtemp())
    hosts = m.job_hosts(json.dumps({"seed_urls": ["https://www.acme.com/x"]}))
    assert "www.acme.com" in hosts and "acme.com" in hosts, \
        "the seen-set select must match both forms or half the rows stay behind"
    assert m.job_hosts(None) == set()
    assert m.job_hosts("not json") == set()


def test_a_stale_running_row_does_not_block_a_move():
    """The guard must key on FRESHNESS, not on the flag. Measured on the live fleet: 87 rows across
    two instances still said status='running' with updated_at twenty hours old, while every engine
    reported stopped. A guard that trusts the flag blocks the tool for ever and trains whoever runs
    it to pass --force by reflex."""
    import datetime as dt
    root = _fixture()
    m = _load(str(root))
    src = sqlite3.connect(root / "crawl_history.sqlite")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)).isoformat()
    src.execute("UPDATE crawl_job_state SET status='running', updated_at=? WHERE job_id='j1'", (old,))
    src.commit(); src.close()
    crawling, fresh, stale = m.is_crawling(0)
    assert crawling is False, "a 20-hour-old 'running' row was treated as a live crawl"
    assert (fresh, stale) == (0, 1), (fresh, stale)


def test_a_FRESH_running_row_does_block_a_move():
    """The other direction matters more: moving a frontier out from under a live engine races it."""
    import datetime as dt
    root = _fixture()
    m = _load(str(root))
    src = sqlite3.connect(root / "crawl_history.sqlite")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    src.execute("UPDATE crawl_job_state SET status='running', updated_at=? WHERE job_id='j1'", (now,))
    src.commit(); src.close()
    crawling, fresh, stale = m.is_crawling(0)
    assert crawling is True, "a live crawl was not detected — the tool would race the engine"
    assert fresh == 1


def test_an_unparseable_timestamp_is_not_evidence_of_a_live_crawl():
    root = _fixture()
    m = _load(str(root))
    src = sqlite3.connect(root / "crawl_history.sqlite")
    src.execute("UPDATE crawl_job_state SET status='running', updated_at='' WHERE job_id='j1'")
    src.commit(); src.close()
    crawling, fresh, stale = m.is_crawling(0)
    assert crawling is False and stale == 1


def test_stale_rows_on_the_TARGET_do_not_fail_the_verify():
    """Measured live: cat_all_drdo refused with "frontier 2260/981". The target already held rows for
    that job from an earlier life of it, so INSERT OR REPLACE left MORE rows than the source sent and
    the count check read a successful copy as a short one. The move is safe either way (the source is
    left intact) but it never completes, so the instance stays idle."""
    root = _fixture()
    m = _load(str(root))
    # the target already has 5 stale rows for j1, from before it was moved away
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    for i in range(5):
        dst.execute("INSERT INTO crawl_frontier VALUES ('j1', ?, 1, '', 0, 'pending', '')",
                    (f"https://www.acme.com/stale{i}",))
    dst.commit(); dst.close()

    r = m.move("j1", 0, 7, apply=True)
    assert r.get("ok"), f"the move was refused because of stale target rows: {r}"
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    n = dst.execute("select count(*) from crawl_frontier where job_id='j1'").fetchone()[0]
    assert n == 3, f"the target must end up with exactly the source's 3 URLs, got {n}"
    stale = dst.execute("select count(*) from crawl_frontier where url like '%stale%'").fetchone()[0]
    assert stale == 0, "the target's stale rows for this job must be cleared, not merged"


def test_the_targets_SEEN_SET_is_merged_not_replaced():
    """The opposite rule for crawl_pages: the target may already know URLs the source does not, and
    dropping those would re-fetch pages already stored."""
    root = _fixture()
    m = _load(str(root))
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    dst.execute("INSERT INTO crawl_pages (canonical_url, content_hash, times_seen) "
                "VALUES ('https://www.acme.com/known-only-here','h9',1)")
    dst.commit(); dst.close()

    m.move("j1", 0, 7, apply=True)
    dst = sqlite3.connect(root / "i8" / "crawl_history.sqlite")
    kept = dst.execute("select count(*) from crawl_pages where canonical_url like '%known-only-here%'").fetchone()[0]
    assert kept == 1, "the target's own seen-set entry was dropped — those pages will be re-fetched"
    total = dst.execute("select count(*) from crawl_pages where canonical_url like '%acme.com%'").fetchone()[0]
    assert total == 3, f"seen sets must MERGE (2 moved + 1 already there), got {total}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} MOVE-FRONTIER TESTS PASSED")
