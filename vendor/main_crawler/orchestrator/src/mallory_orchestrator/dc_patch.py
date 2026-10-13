"""Apply TWO things to the data-centre orchestrator, in place, and nothing else.

  1. the General news source category (a sixth switchable bucket, news-shaped)
  2. the slow-lane cache (cache.py + four wrapped endpoints + the panel's age readout)

The DC tree has diverged from the dev checkout — its api.py is 3,800 lines to the dev copy's 2,400
and it has a `seedprobe` module that does not exist there — so nothing is COPIED over it. Every
change below is an anchored string replacement against the DC's own file.

Two safety properties, both deliberate:

  * ALL-OR-NOTHING. Every file is patched in memory and every anchor asserted before a single byte
    is written. A moved anchor aborts the run with the tree untouched, rather than leaving the
    orchestrator half-patched and unimportable.
  * IDEMPOTENT. Each step checks for its own result first, so a re-run is a no-op and a partial
    previous run completes instead of doubling up.

Backups go to ../_patch_backup_<stamp>/ before anything is written.

Run:  python3 dc_patch.py [--dry-run]
"""

import datetime as dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv

changes = {}     # path -> new text
notes = []
skipped = []


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def stage(rel, pairs, *, done_marker):
    """Apply (old, new) pairs to `rel`. Skips entirely if done_marker is already present."""
    cur = changes.get(rel) or read(rel)
    if done_marker in cur:
        skipped.append(rel + " (already applied)")
        return
    for old, new in pairs:
        assert old in cur, "ANCHOR MISSING in %s:\n%s" % (rel, old[:200])
        cur = cur.replace(old, new, 1)
    changes[rel] = cur
    notes.append(rel)


# ── 1. General news — the bucket ──────────────────────────────────────────────

stage("categories.py", [
('''    "think_tank":  ({"think_tank", "defence_org"}, "news"),
}''',
 '''    "think_tank":  ({"think_tank", "defence_org"}, "news"),
    # General news: mainstream, non-defence press - BBC, Reuters, AP, CNN, Al Jazeera.
    #
    # Its OWN bucket rather than more rows inside News, because the two need separate switches.
    # News is the defence trade press, a few hundred sites that are the point of the corpus;
    # General news is ~700 mainstream outlets that mention defence occasionally. Stopping the
    # second must not stop the first, and one bucket cannot do that.
    #
    # It generates `news`, the same shape as the trade press: freshness-gated, page-capped,
    # keyword-filtered. This is load-bearing. A `profile` job is a whole-site walk with no
    # freshness gate, and bbc.com is millions of pages - that crawl would never finish and would
    # hold an instance for ever. Whatever else changes here, general news is never a profile crawl.
    #
    # NOT the same as `unknown`, which stays out of every bucket on purpose: unknown means nobody
    # has classified the source, so ui_category() shows it under its own raw name rather than
    # inventing a claim about it.
    "general":     ({"general"}, "news"),
}'''),
('''BUCKET_LABEL = {"news": "News", "manufacture": "Manufacture", "tender": "Tender",
                "gov_portal": "Gov portal", "think_tank": "Think tank"}''',
 '''BUCKET_LABEL = {"news": "News", "manufacture": "Manufacture", "tender": "Tender",
                "gov_portal": "Gov portal", "think_tank": "Think tank",
                "general": "General news"}'''),
('''BUCKETS = ("manufacture", "news", "tender", "think_tank", "gov_portal")''',
 '''# General news sorts LAST: it is the broadest and least specific bucket, and listing it first
# invites it to be picked by default, which would quietly stop sites from being classified as the
# defence trade press they actually are.
BUCKETS = ("manufacture", "news", "tender", "think_tank", "gov_portal", "general")'''),
], done_marker='"general":     ({"general"}, "news")')

stage("jobgen.py", [
('NEWS_CATEGORIES = {"trade_press", "business_press", "aggregator", "think_tank", "gov_primary", "defence_org"}',
 '# `general` (mainstream press) belongs here: it is crawled with the same freshness-gated news\n'
 '# shape as the trade press. It is a separate BUCKET so the operator can stop it independently,\n'
 '# not a separate crawl shape.\n'
 'NEWS_CATEGORIES = {"trade_press", "business_press", "aggregator", "think_tank", "gov_primary",\n'
 '                   "defence_org", "general"}'),
], done_marker='"defence_org", "general"}')

stage("sources.py", [
('    "aggregator": 3,',
 '    "aggregator": 3,\n'
 '    # Mainstream press (BBC, Reuters, AP). Tier 2, the same as the defence trade press: reliable\n'
 '    # reporting, but a secondary source - it reports what a primary said rather than saying it.\n'
 '    "general": 2,'),
], done_marker='"general": 2,')

stage("importer.py", [
('''    "think_tank", "business_press", "aggregator", "blog_forum_social", "unknown",''',
 '''    "think_tank", "business_press", "aggregator", "blog_forum_social", "general", "unknown",'''),
('''    "blog_forum_social": ("blog", "forum", "social", "socialmedia", "community", "twitter",''',
 '''    # Mainstream press. "generalnews" is listed as well as "general" so the intent is readable,
    # though longest-alias-wins already gets "General News" right ("general" is the longer match,
    # so it beats "news" -> trade_press). Verified: "General News" -> general, and "Defence News"
    # still -> trade_press.
    #
    # Nothing vague maps here on purpose. "other" and "misc" stay on `unknown`: those are what a
    # spreadsheet says when the author did not know, and a row nobody classified must not arrive
    # pre-classified.
    "general": ("general", "generalnews", "generalpress", "mainstream", "mainstreamnews"),
    "blog_forum_social": ("blog", "forum", "social", "socialmedia", "community", "twitter",'''),
], done_marker='"general": ("general", "generalnews"')

stage("tiering.py", [
('    "blog_forum_social": "weekly", "unknown": "weekly",',
 '    "blog_forum_social": "weekly", "unknown": "weekly",\n'
 '    # Mainstream news publishes continuously, so daily - same as the rest of the press. The\n'
 '    # tiering engine re-tiers from the measured change rate afterwards.\n'
 '    "general": "daily",'),
('    "aggregator": 4, "blog_forum_social": 4, "unknown": 4,',
 '    "aggregator": 4, "blog_forum_social": 4, "unknown": 4,\n'
 '    # Below the defence trade press (2) on purpose: mainstream outlets mention defence\n'
 '    # occasionally, so when the queue is contended the specialists go first.\n'
 '    "general": 3,'),
], done_marker='"general": "daily",')

# ── 2. the panel: General news in both dropdowns ─────────────────────────────

stage("static/index.html", [
('''          <option value="gov_primary">Gov portal</option>
          <option value="think_tank">Think tank</option>''',
 '''          <option value="gov_primary">Gov portal</option>
          <option value="think_tank">Think tank</option>
          <option value="general">General news</option>'''),
('''        <option value="gov_portal">Gov portal</option>
        <option value="think_tank">Think tank</option>''',
 '''        <option value="gov_portal">Gov portal</option>
        <option value="think_tank">Think tank</option>
        <option value="general">General news</option>'''),
# ── the cache age readout ────────────────────────────────────────────────────
('''    <div class="svc" id="svcStrip"></div>
    <button id="btnTheme" class="themeBtn" style="margin-left:auto"''',
 '''    <div class="svc" id="svcStrip"></div>
    <!-- The age of the SLOW panels. Those endpoints cost 10-38s here, so they are served from the
         last computed answer while a refresh runs behind (see cache.py). A stale count that is not
         labelled stale reads as a live one, which is the one thing the numbers on this page are
         not allowed to do. -->
    <span class="note" id="cacheAge" style="margin-left:14px" title=""></span>
    <button id="btnTheme" class="themeBtn" style="margin-left:auto"'''),
('''  if(!r.ok){
    const d = body && body.detail ? body.detail : (typeof body === "string" ? body : r.statusText);
    throw new Error(String(d).slice(0,300));
  }
  return body;
}''',
 '''  if(!r.ok){
    const d = body && body.detail ? body.detail : (typeof body === "string" ? body : r.statusText);
    throw new Error(String(d).slice(0,300));
  }
  if(body && body._cache){ CACHE_AGE[path.split("?")[0]] = body._cache; renderCacheAge(); }
  return body;
}

/* Cache provenance, collected in one place. The slow endpoints answer with a `_cache` stamp
   ({as_of, age_s, stale, refreshing, error}); recording it in api() means every panel contributes
   to the age readout without any renderer having to know the cache exists. */
const CACHE_AGE = {};
function ageWords(sec){
  if(sec == null) return "";
  if(sec < 60) return Math.round(sec) + "s";
  if(sec < 3600) return Math.round(sec/60) + "m";
  return (sec/3600).toFixed(1) + "h";
}
/* Report the OLDEST panel, not an average: the reader needs to know the worst thing on screen is
   four minutes old, and an average would hide exactly that behind three fresh ones. */
function renderCacheAge(){
  const el = document.getElementById("cacheAge");
  if(!el) return;
  const rows = Object.entries(CACHE_AGE);
  if(!rows.length){ el.textContent = ""; return; }
  let worst = null, refreshing = 0, errs = [];
  for(const [pth, c] of rows){
    if(c.refreshing) refreshing++;
    if(c.error) errs.push(pth.replace("/api/","") + ": " + c.error);
    if(worst === null || c.age_s > worst.age_s) worst = c;
  }
  const parts = ["slow panels: " + ageWords(worst.age_s) + " old"];
  if(refreshing) parts.push("refreshing " + refreshing);
  if(errs.length) parts.push("refresh FAILED");
  el.textContent = parts.join(" \\u00b7 ");
  el.style.color = errs.length ? "var(--bad, #d66)" : "";
  el.title = rows.map(([pth,c]) =>
    pth + " - as of " + String(c.as_of).replace("T"," ").slice(0,19) +
    (c.error ? " (refresh failed: " + c.error + ")" : "")).join(String.fromCharCode(10));
}
/* The stamp ages on the clock, not only when a request lands: without this the readout would
   freeze at "2s old" on a page nobody is polling. */
setInterval(() => {
  for(const c of Object.values(CACHE_AGE)) c.age_s += 5;
  renderCacheAge();
}, 5000);'''),
], done_marker='id="cacheAge"')

# ── 3. the cache: import, four wrapped endpoints, two diagnostic routes ──────

stage("api.py", [
('from . import (categories, crawler_client, errorclass, importer, jobgen, orchestrate, scheduler,\n'
 '               seedprobe, tiering)',
 'from . import (cache, categories, crawler_client, errorclass, importer, jobgen, orchestrate,\n'
 '               scheduler, seedprobe, tiering)'),

('@router.get("/data/summary")\ndef data_summary() -> dict:\n',
 '# -- the slow lane, cached -----------------------------------------------------------------\n'
 '# Measured on this stack: /logs/sites 38.4s, /logs/unresolved 33.7s, /data/categories 20.5s,\n'
 '# /data/summary 10.6s. Those are real costs (seven instances\' batch reports, a 5,000-row ingest\n'
 '# rollup), not something a faster query fixes - so the reader gets the last computed answer\n'
 '# immediately, stamped with its age, while a background refresh runs. `?fresh=1` forces a\n'
 '# synchronous recompute for an operator who has just changed something and wants to see it.\n'
 '#\n'
 '# TTLs sit just under each panel\'s own poll interval, so what is on screen is at most one poll\n'
 '# behind: the panel already polls a 30s lane and a 90s lane.\n'
 '\n'
 '@router.get("/data/summary")\n'
 'def data_summary(fresh: int = 0) -> dict:\n'
 '    """Real document/blob counts, from the last-known-good cache (see cache.py)."""\n'
 '    return cache.serve("data/summary", lambda db: _data_summary(), 120.0, force=bool(fresh))\n'
 '\n\n'
 'def _data_summary() -> dict:\n'),

('@router.get("/data/categories")\ndef data_categories(db: Session = Depends(get_db)) -> dict:\n',
 '@router.get("/data/categories")\n'
 'def data_categories(fresh: int = 0) -> dict:\n'
 '    """Stored documents by catalog category, from the cache (see cache.py)."""\n'
 '    return cache.serve("data/categories", _data_categories, 25.0, force=bool(fresh))\n'
 '\n\n'
 'def _data_categories(db: Session) -> dict:\n'),

('@router.get("/logs/sites")\ndef logs_sites(limit: int = 300, db: Session = Depends(get_db)) -> dict:\n',
 '@router.get("/logs/sites")\n'
 'def logs_sites(limit: int = 300, fresh: int = 0) -> dict:\n'
 '    """Per-site crawl rows, from the cache (see cache.py). Keyed on `limit`: a 500-row and a\n'
 '    50-row answer are different values, and sharing one entry would serve whichever the last\n'
 '    caller happened to ask for."""\n'
 '    return cache.serve(f"logs/sites:{limit}", lambda db: _logs_sites(db, limit), 25.0,\n'
 '                       force=bool(fresh))\n'
 '\n\n'
 'def _logs_sites(db: Session, limit: int = 300) -> dict:\n'),

('@router.get("/logs/unresolved")\ndef logs_unresolved(host: str = "", limit: int = 500) -> dict:\n',
 '@router.get("/logs/unresolved")\n'
 'def logs_unresolved(host: str = "", limit: int = 500, fresh: int = 0) -> dict:\n'
 '    """Unresolved URLs with the crawler\'s reason codes, from the cache (see cache.py)."""\n'
 '    return cache.serve(f"logs/unresolved:{host}:{limit}",\n'
 '                       lambda db: _logs_unresolved(host, limit), 80.0, force=bool(fresh))\n'
 '\n\n'
 'def _logs_unresolved(host: str = "", limit: int = 500) -> dict:\n'),
], done_marker="cache.serve(")

# api.py also gets the two diagnostic routes, appended.
if "api.py" in changes and "/cache/clear" not in changes["api.py"]:
    changes["api.py"] = changes["api.py"].rstrip("\n") + '''


@router.get("/cache")
def cache_state() -> list:
    """What the slow-lane cache is holding and how old each entry is.

    Exists so "the dashboard is showing an old number" is answerable without reading logs: it says
    which key is stale, whether a refresh is running, and what the last refresh error was.
    """
    return cache.peek()


@router.post("/cache/clear")
def cache_clear(key: str = "") -> dict:
    """Drop cached values so the next read recomputes."""
    return {"cleared": cache.clear(key or None), "key": key or "all"}
'''

# ── verify, back up, write ───────────────────────────────────────────────────

for rel, text in changes.items():
    if rel.endswith(".py"):
        import ast
        ast.parse(text)          # a syntax error must never reach the container

print("would patch:" if DRY else "patching:")
for rel in sorted(changes):
    print("   ", rel)
for s in skipped:
    print("    skip:", s)

if DRY:
    print("\ndry run - nothing written")
    raise SystemExit(0)

if not changes:
    print("\nnothing to do (everything already applied)")
    raise SystemExit(0)

stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
backup = os.path.join(ROOT, "..", "..", "..", "_patch_backup_" + stamp)
backup = os.path.abspath(backup)
os.makedirs(backup, exist_ok=True)
for rel in changes:
    dst = os.path.join(backup, rel.replace("/", "__"))
    shutil.copy2(os.path.join(ROOT, rel), dst)
print("\nbackup:", backup)

for rel, text in changes.items():
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
        f.write(text)
print("written:", len(changes), "file(s)")
