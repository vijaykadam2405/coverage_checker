"""The archive date must actually reach a human.

`archived_at`'s wire was cut in TWO places: HarvestedPage never set it (write side), and
pgstore._CARD_SQL never SELECTed it (read side). The read-side cut is the dangerous one — it only
bites when DATABASE_URL is set, while the ndjson test path json.loads()es the whole document and
looks green. That is why the column/unpack arity is asserted here rather than trusted.
"""
import re

from ingest_api import dashboard, pgstore


def _card(**doc):
    base = {"url": "http://sipri.org/a", "title": "t", "main_text": "x", "fetched_at": "2026-07-16T00:00:00Z"}
    return dashboard._page_card({"document": {**base, **doc}})


# --- the read-side cut: SQL columns must match the tuple unpack ----------
def test_card_sql_selects_archived_at():
    # Without this, every C2 card renders a blank date in prod, forever, while tests pass.
    assert "archived_at" in pgstore._CARD_SQL


def test_card_sql_column_count_matches_the_unpack():
    cols = pgstore._CARD_SQL.split("SELECT", 1)[1].split("FROM", 1)[0]
    n = len(re.findall(r",(?![^()]*\))", cols)) + 1        # commas not inside coalesce(...)
    src = dashboard._pg_record.__code__
    unpacked = [v for v in src.co_varnames if v != "row"][:n]
    assert n == len(unpacked), f"_CARD_SQL selects {n} columns; _pg_record unpacks {len(unpacked)}"


def test_pg_record_carries_archived_at_through():
    row = ("id", "http://x/a", "t", "body", "2026-07-16", 0, None,
           "c2_wayback", "http://web.archive.org/web/1id_/http://x/a", [], 10, "2024-07-01T00:00:00Z")
    rec = dashboard._pg_record(row)
    assert rec["document"]["archived_at"] == "2024-07-01T00:00:00Z"
    assert rec["document"]["served_by"] == "c2_wayback"


# --- what the operator actually sees -------------------------------------
def test_archive_card_shows_the_snapshot_date_not_todays_date():
    html = _card(served_by="c2_wayback", archived_at="2015-04-04T07:25:17Z",
                 served_from="https://web.archive.org/web/20150404072517id_/http://sipri.org/")
    assert "archived 2015-04-04T07:25:17Z" in html
    assert "crawled 2026" not in html            # a 2015 capture must not shout "crawled today"
    assert "C2 &middot; wayback" in html or "C2 · wayback" in html


def test_archive_card_keeps_fetched_at_on_the_hover():
    html = _card(served_by="c2_wayback", archived_at="2015-04-04T07:25:17Z")
    assert "pulled from the archive at 2026-07-16T00:00:00Z" in html


def test_live_card_is_unchanged():
    html = _card(served_by="c1_live")
    assert "crawled 2026-07-16T00:00:00Z" in html
    assert "archived" not in html


def test_live_card_without_served_by_is_unchanged():
    assert "crawled 2026-07-16T00:00:00Z" in _card()
