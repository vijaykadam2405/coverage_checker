"""Stop the dashboard from stampeding the corpus.

THE FAULT. `/api/live/sources?limit=5000` is a full aggregate over 1.38M documents - measured at
57s, and 130s under contention. FOUR separate endpoints call it independently:

    /api/data/categories   /api/logs/sites   /api/logs/sites/counts   /api/data/sources

Each has its own cache key, so the cache single-flights each one but cannot see that all four are
the same query. Four concurrent full scans, every refresh. Worse, their TTLs (25s) are far SHORTER
than the query takes, so a refresh is always in flight: the panel never stops scanning.

And the client walking away does not stop the query. Measured today: 31 orphaned scans, the oldest
running 25 minutes, each slowing the others down - a feedback loop that ends with the store
reporting itself unreachable on a database that is fine.

THE FIX, in two parts.

1. ONE shared cache entry for the ingest rollup itself, not four derived from it. Single-flight
   then means what it should: at most one such scan exists at a time, and the other three
   consumers wait on its result instead of starting their own.

2. TTLs longer than the query. A TTL shorter than the work it caches is not a cache, it is a
   scheduler for the work. 300s against a 57s query leaves the corpus idle between refreshes;
   these numbers move on the scale of a crawl, not a poll.

Not done here, and it needs a decision: a `statement_timeout` on the database role would make an
abandoned query die on its own rather than orphan. It would also apply to the crawler's writes,
so it is the operator's call, not mine.

Run:  python3 dc_fix2.py [--dry-run]
"""

import ast
import datetime as dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv
P = os.path.join(ROOT, "api.py")

src = open(P, encoding="utf-8").read()
before = src
notes = []

# -- 1. the shared rollup ---------------------------------------------------------------------
HELPER = '''

def _ingest_sources(db=None, limit: int = 5000):
    """The ingest per-source rollup, fetched AT MOST ONCE at a time for the whole process.

    This one HTTP call is a full aggregate over 1.38M documents (57s measured, 130s under
    contention). Four endpoints need it; before this they each fetched it separately, so a single
    dashboard refresh started four identical full scans and none of them could see the others.
    Routing every consumer through one cache key makes the cache's single-flight actually mean
    "one scan", which is what it was for.

    The TTL is deliberately much longer than the query: a TTL shorter than the work it caches is
    not a cache, it is a schedule for that work.
    """
    return cache.serve("ingest/sources:%d" % limit,
                       lambda _db: _ingest_get("/api/live/sources?limit=%d" % limit, timeout=180.0),
                       300.0)

'''

anchor = "def _ingest_get(path: str, timeout: float = 20.0):"
if "_ingest_sources(" in src:
    notes.append("shared rollup helper already present")
else:
    i = src.index(anchor)
    j = src.index("\n\n\n", i)
    src = src[:j] + "\n" + HELPER.rstrip("\n") + src[j:]
    notes.append("added _ingest_sources() shared helper")

# every bare call site now goes through the shared entry
for old, new in [
    ('_ingest_get("/api/live/sources?limit=5000", timeout=120.0)', '_ingest_sources()'),
    ('_ingest_get("/api/live/sources?limit=5000")', '_ingest_sources()'),
]:
    n = src.count(old)
    if n:
        src = src.replace(old, new)
        notes.append("routed %d call site(s) through the shared entry" % n)

# -- 2. TTLs longer than the query they cache ---------------------------------------------------
for old, new, what in [
    ('cache.serve("data/categories", _data_categories, 25.0', 'cache.serve("data/categories", _data_categories, 300.0', "data/categories"),
    ('lambda db: _logs_sites(db, limit), 25.0', 'lambda db: _logs_sites(db, limit), 300.0', "logs/sites"),
    ('lambda db: _logs_unresolved(host, limit), 80.0', 'lambda db: _logs_unresolved(host, limit), 300.0', "logs/unresolved"),
]:
    if old in src:
        src = src.replace(old, new, 1)
        notes.append("TTL raised: %s" % what)

ast.parse(src)
assert src.count("_ingest_sources()") >= 3, "call sites not routed"
assert '_ingest_get("/api/live/sources?limit=5000"' not in src, "a bare 5000-row call site remains"

print("\n".join("  " + s for s in notes))
if src == before:
    print("\nnothing to change")
    raise SystemExit(0)
if DRY:
    print("\ndry run - nothing written")
    raise SystemExit(0)

stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bdir = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "_patch_backup_" + stamp))
os.makedirs(bdir, exist_ok=True)
shutil.copy2(P, os.path.join(bdir, "api.py"))
print("\nbackup:", bdir)
open(P, "w", encoding="utf-8").write(src)
print("written: api.py")
