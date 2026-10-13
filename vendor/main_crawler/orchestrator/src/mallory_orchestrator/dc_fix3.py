"""The last call site, and the one that made "Data per site" say 'timed out' forever.

`_data_sources_live` builds its URL with an f-string:

    _ingest_get(f"/api/live/sources?limit={limit}")

so the previous pass, which replaced literal strings, walked straight past it. It therefore still
ran on the 20s default against a 57s query - which is the whole reason that panel has never once
loaded.

Fixing the timeout alone would have been wrong: it would have added a SECOND full scan alongside
the shared one, keyed on a different limit (the panel asks for 800, the rollups for 5000). So
`_ingest_sources` now always fetches ONE canonical size and slices down. The 5,000-row fetch is a
superset of every caller's need - the store holds ~836 sources with pages - so slicing returns
exactly what a smaller fetch would have, from a scan that is already running.

That leaves exactly one full-corpus scan in the entire process, shared by all five consumers.
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

# 1. one canonical fetch size, sliced per caller
old_helper = '''    return cache.serve("ingest/sources:%d" % limit,
                       lambda _db: _ingest_get("/api/live/sources?limit=%d" % limit, timeout=180.0),
                       300.0)'''
new_helper = '''    # ALWAYS fetch the canonical size and slice. Keying the cache on the caller's own limit would
    # give the 800-row panel and the 5000-row rollup separate entries, and therefore two concurrent
    # full scans of the same table - the exact stampede this helper exists to stop. 5,000 is a
    # superset of every caller (the store holds ~836 sources with pages), so a slice is byte-for-byte
    # what a smaller fetch would have returned.
    rows = cache.serve("ingest/sources",
                       lambda _db: _ingest_get("/api/live/sources?limit=5000", timeout=180.0),
                       300.0)
    if not isinstance(rows, list):
        return rows
    return rows[:limit] if limit and limit < len(rows) else rows'''
if old_helper in src:
    src = src.replace(old_helper, new_helper, 1)
    notes.append("one canonical fetch size, sliced per caller")
elif 'cache.serve("ingest/sources"' in src:
    notes.append("canonical fetch already in place")
else:
    raise SystemExit("ANCHOR MISSING: _ingest_sources body")

# 2. the f-string call site the previous pass could not see
old_fs = 'return {"reachable": True, "rows": _ingest_get(f"/api/live/sources?limit={limit}")}'
new_fs = 'return {"reachable": True, "rows": _ingest_sources(limit=limit)}'
if old_fs in src:
    src = src.replace(old_fs, new_fs, 1)
    notes.append("routed the f-string call site (data/sources) through the shared entry")
elif new_fs in src:
    notes.append("f-string call site already routed")
else:
    raise SystemExit("ANCHOR MISSING: _data_sources_live")

ast.parse(src)
# No caller may reach the store outside the shared entry any more.
assert "live/sources?limit={limit}" not in src, "an f-string call site remains"
assert src.count('_ingest_get("/api/live/sources?limit=5000", timeout=180.0)') == 1, \
    "the canonical fetch must appear exactly once"

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
