"""`ingested_at` is TEXT and must be compared as TEXT, never cast per row.

MEASURED on the live stack. `ingested_at::timestamptz > now() - interval '5 minutes'` has to cast
every row before it can compare, so no index is usable and it is always a full pass over the corpus.
At 927k rows that query ran SIXTEEN MINUTES under load; the text form ran in 4.2s. It was one of the
scans that had Postgres at 7.16 of 40 cores — more than any crawler container — while crawl
throughput halved from 1.19 to 0.48 pages/s.

Worse, it stacked: a 39-minute dedup scan held a lock, ingest's start-up
`CREATE TABLE IF NOT EXISTS documents` queued behind it, and then every query queued behind THAT.
The whole database stalled for ~40 minutes.

ISO-8601 sorts lexicographically, so a string comparison against a pre-formatted bound answers the
identical question and can use documents_ingested_at_idx.
"""
from __future__ import annotations

import inspect
import re

import pytest

from ingest_api import pgstore


def _code_only(src: str) -> str:
    """Comments NAME the bad pattern on purpose — that is the explanation, and the first version of
    this test failed on its own prose. Only executable lines are the subject."""
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines() if not l.strip().startswith("#"))


def test_live_summary_does_not_cast_the_column():
    code = _code_only(inspect.getsource(pgstore.live_summary))
    assert "ingested_at::timestamptz" not in code, (
        "casting ingested_at per row forces a full corpus scan and defeats the index")
    assert "ingested_at >" in code, "the window filter went missing entirely"


def test_the_bound_is_built_from_UTC():
    """The column carries no timezone. Comparing it against a local-time string silently shifts the
    window by the server's offset — a wrong number that still looks like a number."""
    src = inspect.getsource(pgstore.live_summary)
    assert "at time zone 'UTC'" in src
    assert 'to_char(' in src


def test_the_bound_format_matches_how_rows_are_stored():
    """to_char must emit the same shape the writer stores, or the string comparison is meaningless.
    Pinned against the module's own timestamp format rather than a literal copy of it."""
    src = inspect.getsource(pgstore.live_summary)
    m = re.search(r"'YYYY-MM-DD\"T\"HH24:MI:SS'", src)
    assert m, "the to_char format is not the ISO shape the rows are written in"


def test_the_index_is_in_the_schema():
    """Created by hand on the live database once. It has to be in the schema too, or the next fresh
    deployment silently goes back to full scans."""
    assert "documents_ingested_at_idx" in pgstore.SCHEMA_SQL if hasattr(pgstore, "SCHEMA_SQL") \
        else "documents_ingested_at_idx" in inspect.getsource(pgstore)


def test_no_other_query_casts_ingested_at():
    """One re-introduction puts the full scan back. Cheap to check, and the symptom otherwise is
    not an error — it is the crawl slowing down."""
    bad = [l.strip() for l in _code_only(inspect.getsource(pgstore)).splitlines()
           if "ingested_at::" in l]
    assert not bad, "these cast ingested_at per row: %r" % bad


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
