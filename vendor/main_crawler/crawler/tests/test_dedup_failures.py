"""Failure persistence + gone-skip + column back-fill (crawler/dedup.py)."""
import sqlite3

from crawler.dedup import CrawlHistory


def test_record_failure_and_is_gone_404(tmp_path):
    h = CrawlHistory(tmp_path / "h.sqlite")
    u = "https://x.com/missing"
    h.record_failure(u, status=404, category="http_404", failed_at="2026-07-09T00:00:00Z")
    assert not h.is_gone(u)                       # one 404 → not yet gone (golden rule)
    h.record_failure(u, status=404, category="http_404", failed_at="2026-07-09T00:01:00Z")
    assert h.is_gone(u)                           # two 404s → gone
    h.close()


def test_record_failure_410_immediate(tmp_path):
    h = CrawlHistory(tmp_path / "h.sqlite")
    u = "https://x.com/dead"
    h.record_failure(u, status=410, category="http_410", failed_at="2026-07-09T00:00:00Z")
    assert h.is_gone(u)                           # 410 → gone at once
    h.close()


def test_success_resets_fail_count(tmp_path):
    h = CrawlHistory(tmp_path / "h.sqlite")
    u = "https://x.com/flaky"
    h.record_failure(u, status=404, category="http_404", failed_at="t1")
    h.record_failure(u, status=404, category="http_404", failed_at="t2")
    assert h.is_gone(u)
    h.upsert(u, content_hash="abc", etag=None, last_modified=None, status=200, fetched_at="t3")
    assert not h.is_gone(u)                        # a successful fetch clears the gone state
    h.close()


def test_is_gone_unknown_url(tmp_path):
    h = CrawlHistory(tmp_path / "h.sqlite")
    assert not h.is_gone("https://x.com/never-seen")
    h.close()


def test_column_backfill_on_old_db(tmp_path):
    """A crawl_history.sqlite created before the failure columns existed must open clean."""
    p = tmp_path / "old.sqlite"
    old = sqlite3.connect(p)
    old.executescript(
        "CREATE TABLE crawl_pages (canonical_url TEXT PRIMARY KEY, content_hash TEXT, "
        "etag TEXT, last_modified TEXT, js_heavy INTEGER DEFAULT 0, last_status INTEGER, "
        "last_seen TEXT, times_seen INTEGER DEFAULT 0);"
    )
    old.execute("INSERT INTO crawl_pages(canonical_url) VALUES ('https://x.com/a')")
    old.commit()
    old.close()

    h = CrawlHistory(p)                            # must ALTER-add the new columns, no crash
    cols = {r[1] for r in h._conn.execute("PRAGMA table_info(crawl_pages)")}
    assert {"error_category", "fail_count", "failed_at"} <= cols
    h.record_failure("https://x.com/a", status=410, category="http_410", failed_at="t")
    assert h.is_gone("https://x.com/a")
    h.close()
