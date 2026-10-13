"""A weak-under-load mark must EXPIRE. One bad minute must not cap a host forever.

MEASURED on the live fleet. `_note_weak_host` promotes a host to careful (concurrency 1) after
CRAWLER_HOST_WEAK_FAILS=3 conn_refused/timeouts and persists it, and `make_careful` returns early
for a host already careful — so nothing ever re-tested one. The result:

    365 hosts marked careful fleet-wide, against only 76 hosts with pending work

Essentially everything being crawled was capped at a single connection while each instance ran 48
workers, so the workers had nowhere to go and spun: i9 and i10 were logging 157 and 231
host-deflections PER FETCH. That is a livelock, not politeness.

The trigger is three timeouts, which a load spike produces on perfectly healthy hosts — the
40-minute database stall immediately before this measurement timed out crawler POSTs fleet-wide,
which is very likely how the set got so large.

This is the same failure shape as the always-render escalation (test_render_escalation_evidence):
a permanent decision made from transient evidence, never revisited. Expiry IS the re-test — a
genuinely weak host earns the mark back within three failures, at the cost of one window at normal
concurrency; a healthy host recovers without an operator noticing.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crawler.dedup import CrawlHistory


def _h(tmp):
    return CrawlHistory(Path(tmp) / "h.sqlite")


def _stamp(h, host, when):
    h._conn.execute("UPDATE host_careful SET at=? WHERE host=?",
                    (when.strftime("%Y-%m-%dT%H:%M:%SZ"), host))
    h._conn.commit()


def test_a_fresh_mark_is_honoured():
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h.set_careful_host("slow.test", True)
        assert "slow.test" in h.get_careful_hosts()
        h.close()


def test_a_stale_mark_expires():
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h.set_careful_host("wasslow.test", True)
        _stamp(h, "wasslow.test", datetime.now(timezone.utc) - timedelta(hours=48))
        assert "wasslow.test" not in h.get_careful_hosts(), (
            "a 48h-old mark still caps the host — nothing re-tests it")
        h.close()


def test_an_UNSTAMPED_row_is_treated_as_expired():
    """Rows written before the `at` column existed. Those are exactly the 365 poisoned marks, so
    treating a NULL as permanent would preserve the bug on every existing database."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h._conn.execute("INSERT INTO host_careful (host, careful, at) VALUES ('legacy.test', 1, NULL)")
        h._conn.commit()
        assert "legacy.test" not in h.get_careful_hosts()
        h.close()


def test_the_ttl_is_configurable(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h.set_careful_host("x.test", True)
        _stamp(h, "x.test", datetime.now(timezone.utc) - timedelta(hours=5))
        monkeypatch.setenv("CRAWLER_CAREFUL_TTL_H", "1")
        assert "x.test" not in h.get_careful_hosts()
        monkeypatch.setenv("CRAWLER_CAREFUL_TTL_H", "24")
        assert "x.test" in h.get_careful_hosts()
        h.close()


def test_clearing_a_host_still_works():
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h.set_careful_host("y.test", True)
        assert "y.test" in h.get_careful_hosts()
        h.set_careful_host("y.test", False)
        assert "y.test" not in h.get_careful_hosts()
        h.close()


def test_re_marking_refreshes_the_stamp():
    """A host that is STILL weak must stay careful — expiry is a re-test, not an amnesty."""
    with tempfile.TemporaryDirectory() as d:
        h = _h(d)
        h.set_careful_host("z.test", True)
        _stamp(h, "z.test", datetime.now(timezone.utc) - timedelta(hours=48))
        assert "z.test" not in h.get_careful_hosts()
        h.set_careful_host("z.test", True)                  # failed again after the re-test
        assert "z.test" in h.get_careful_hosts()
        h.close()


def test_an_existing_database_gains_the_column():
    """Every live crawl_history.sqlite predates `at`, and CREATE TABLE IF NOT EXISTS will not add a
    column to a table that already exists. Without the ALTER, opening one raises on every write."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.sqlite"
        old = sqlite3.connect(p)
        old.executescript("CREATE TABLE host_careful (host TEXT PRIMARY KEY, careful INTEGER DEFAULT 0);")
        old.execute("INSERT INTO host_careful (host, careful) VALUES ('old.test', 1)")
        old.commit()
        old.close()

        h = CrawlHistory(p)                                  # must migrate, not explode
        cols = {r[1] for r in h._conn.execute("PRAGMA table_info(host_careful)")}
        assert "at" in cols
        assert "old.test" not in h.get_careful_hosts(), "the legacy mark should read as expired"
        h.set_careful_host("old.test", True)
        assert "old.test" in h.get_careful_hosts()
        h.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
