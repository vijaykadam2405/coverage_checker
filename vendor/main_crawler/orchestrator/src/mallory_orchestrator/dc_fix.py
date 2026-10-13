"""Fix two faults on the data-centre orchestrator.

1. REGRESSION I INTRODUCED. Wrapping /logs/sites in the cache changed its signature from
   `logs_sites(limit, db=Depends(get_db))` to `logs_sites(limit, fresh=0)`. Two INTERNAL callers
   still pass `db=db`, so both now raise `logs_sites() got an unexpected keyword argument 'db'`:

     * `_logs_sites_counts_live` -> the Watchfloor "Sites complete" tile, which is why it reads
       "no crawl log yet";
     * `/api/schedule/enrol-completed`, which would have failed the moment it was pressed.

   The dev checkout has no such callers, which is exactly why checking it was not enough - the DC
   api.py is 1,400 lines longer. Both now call the uncached inner `_logs_sites(db, limit)`, which
   is what a route handler's body was always for.

2. PRE-EXISTING. "Data per site" has never loaded. `_ingest_get` defaults to a 20s timeout and
   `/api/live/sources?limit=5000` takes 57s against 1.38M documents - measured, returns HTTP 200
   and 119KB when allowed to finish. So the endpoint was not slow-and-flaky, it was deterministically
   abandoned 37 seconds before the answer arrived. The two call sites that feed the per-site panels
   get 120s, matching what the two /api/live/sources?limit=100000 call sites already use. The
   latency is absorbed by the slow-lane cache: the 57s is paid once per TTL, not per page load.

Idempotent and all-or-nothing, same as dc_patch.py.

Run:  python3 dc_fix.py [--dry-run]
"""

import ast
import datetime as dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv
P = os.path.join(ROOT, "api.py")

with open(P, encoding="utf-8") as f:
    src = f.read()

before = src
notes = []

# -- 1. the two internal callers -------------------------------------------------------------
old_a = "    rows = logs_sites(limit=5000, db=db).get(\"rows\") or []"
new_a = ("    # The uncached inner function, not the route handler: the handler is a cache wrapper\n"
         "    # now and no longer takes a session.\n"
         "    rows = _logs_sites(db, 5000).get(\"rows\") or []")
old_b = "        d = logs_sites(limit=5000, db=db)"
new_b = ("        # The uncached inner function — see the note on the other caller.\n"
         "        d = _logs_sites(db, 5000)")

if old_a in src:
    src = src.replace(old_a, new_a, 1)
    notes.append("enrol-completed caller fixed")
elif new_a.splitlines()[-1] in src:
    notes.append("enrol-completed caller already fixed")
else:
    raise SystemExit("ANCHOR MISSING: enrol-completed caller")

if old_b in src:
    src = src.replace(old_b, new_b, 1)
    notes.append("sites-counts caller fixed")
elif new_b.splitlines()[-1] in src:
    notes.append("sites-counts caller already fixed")
else:
    raise SystemExit("ANCHOR MISSING: sites-counts caller")

# -- 2. the 20s timeout on a 57s call ---------------------------------------------------------
# Only the bare-timeout call sites; the two that already pass timeout=120.0 are left alone.
old_c = '_ingest_get("/api/live/sources?limit=5000")'
new_c = ('_ingest_get("/api/live/sources?limit=5000", timeout=120.0)')
n = src.count(old_c)
if n:
    src = src.replace(old_c, new_c)
    notes.append("raised ingest timeout on %d call site(s)" % n)
else:
    notes.append("ingest timeout already raised")

# -- verify, back up, write --------------------------------------------------------------------
ast.parse(src)

# A guard against the very mistake this file exists to fix: no caller may pass db= to a handler
# that is now a cache wrapper.
for bad in ("logs_sites(limit=5000, db=", "data_categories(db=", "logs_sites(db="):
    assert bad not in src, "a caller still passes db= to a cache-wrapped handler: %s" % bad

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
with open(P, "w", encoding="utf-8") as f:
    f.write(src)
print("written: api.py")
