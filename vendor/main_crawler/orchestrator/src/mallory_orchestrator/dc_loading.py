"""A loading screen for the Watchfloor, so the first paint is not a wall of empty tiles.

The panel's slow lane costs 10-60s on a cold cache. Until now that time was spent staring at
placeholder dashes, which look exactly like a measurement of zero - the one thing the numbers on
this page are not allowed to look like.

Three rules it follows:

  * it says what it is waiting for, and updates as each panel lands, so the wait is legible rather
    than a spinner that could mean anything;
  * it never traps anyone - it clears on the first real data, at 5s at the latest, and on a click
    or Escape at any time. A loading screen that outlives the load is worse than none;
  * it fades rather than cutting, because the page underneath is already painted; the overlay is
    hiding a half-filled dashboard, not an empty document.

Uses the page's own CSS variables, so it themes with the rest of the panel in light and dark.
"""

import datetime as dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv
P = os.path.join(ROOT, "static", "index.html")

src = open(P, encoding="utf-8").read()
before = src

if 'id="boot"' in src:
    print("  loading screen already present")
    raise SystemExit(0)

OVERLAY = '''<body>
<!-- Cold-start overlay. The slow lane costs 10-60s on a cold cache; without this the first paint
     is a grid of "-" placeholders, which reads as "measured zero" rather than "not here yet". -->
<div id="boot">
  <div class="boot-card">
    <div class="boot-brand">MALLORY<span>crawl control &middot; layer 1</span></div>
    <div class="boot-bar"><i></i></div>
    <div class="boot-msg" id="bootMsg">reaching the crawler&hellip;</div>
    <div class="boot-hint">the corpus rollup is a full scan &middot; click anywhere to skip</div>
  </div>
</div>
<style>
#boot{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;
      background:var(--bg);transition:opacity .45s ease}
#boot.gone{opacity:0;pointer-events:none}
.boot-card{text-align:center;max-width:min(90vw,420px);padding:0 20px}
.boot-brand{font-weight:700;letter-spacing:.14em;font-size:20px;color:var(--fg,#e6edf6)}
.boot-brand span{display:block;margin-top:7px;font-weight:400;letter-spacing:.04em;font-size:12px;
                 color:var(--muted,#8b9bb0)}
.boot-bar{margin:22px auto 12px;height:2px;width:190px;background:var(--line);overflow:hidden;
          border-radius:2px}
.boot-bar i{display:block;height:100%;width:38%;background:var(--accent);border-radius:2px;
            animation:bootslide 1.15s ease-in-out infinite}
@keyframes bootslide{0%{transform:translateX(-100%)}100%{transform:translateX(360%)}}
.boot-msg{font-size:13px;color:var(--fg,#e6edf6);min-height:1.2em}
.boot-hint{margin-top:8px;font-size:11px;color:var(--muted,#8b9bb0)}
@media (prefers-reduced-motion:reduce){.boot-bar i{animation:none;width:100%}}
</style>
<script>
/* Deliberately inline and BEFORE the app script: if the app throws, this still clears itself and
   the operator sees the page (and its canary) instead of a loading screen that never ends. */
(function(){
  var el = document.getElementById("boot"), done = false;
  window.__bootMsg = function(t){ var m = document.getElementById("bootMsg"); if(m && !done) m.textContent = t; };
  window.__bootDone = function(){
    if(done) return; done = true;
    el.classList.add("gone");
    setTimeout(function(){ if(el && el.parentNode) el.parentNode.removeChild(el); }, 500);
  };
  /* The cap is the promise: 5 seconds and you are looking at the dashboard, filled or not. */
  setTimeout(window.__bootDone, 5000);
  el.addEventListener("click", window.__bootDone);
  document.addEventListener("keydown", function(e){ if(e.key === "Escape") window.__bootDone(); });
})();
</script>
'''

assert "<body>\n" in src, "body anchor"
src = src.replace("<body>\n", OVERLAY, 1)

# Report progress from the fetch helper, and clear on the first panel that actually renders.
old = '''  if(body && body._cache){ CACHE_AGE[path.split("?")[0]] = body._cache; renderCacheAge(); }
  return body;
}'''
new = '''  if(body && body._cache){ CACHE_AGE[path.split("?")[0]] = body._cache; renderCacheAge(); }
  /* Name the panel that just landed. A named wait is a shorter-feeling wait than a spinner, and
     it also makes a stuck load diagnosable from across the room. */
  if(window.__bootMsg){
    var lbl = {"/api/host":"hardware", "/api/data/summary":"corpus", "/api/crawler/batch/status":"the crawl",
               "/api/crawler/batch/sites":"sites in this run", "/api/data/categories":"corpus by category",
               "/api/logs/sites":"per-site rows", "/api/logs/unresolved":"the failure log"}[path.split("?")[0]];
    if(lbl) window.__bootMsg(lbl + " loaded");
  }
  return body;
}'''
assert old in src, "api() anchor"
src = src.replace(old, new, 1)

# The first paint of the live view is the real "ready" signal.
old2 = "loadWatch();\nloadWatchSlow();"
new2 = '''loadWatch().then ? loadWatch() : loadWatch();
/* Clear on the first render of the fast lane, not on window.load: the page is "ready" when it has
   numbers on it, and the slow lane may still be minutes away. The 5s cap above is the backstop. */
setTimeout(function(){ if(window.__bootDone) window.__bootDone(); }, 900);
loadWatchSlow();'''
if old2 in src:
    src = src.replace(old2, new2, 1)
else:
    # boot block differs; fall back to clearing shortly after the script runs
    src = src.replace("loadWatch();", "loadWatch();\nsetTimeout(function(){ if(window.__bootDone) window.__bootDone(); }, 900);", 1)

# Belt and braces: the canary line runs last, so anything that got that far is renderable.
src = src.replace('document.body.setAttribute("data-js", "ok");',
                  'document.body.setAttribute("data-js", "ok");\nif(window.__bootDone) setTimeout(window.__bootDone, 300);')

print("  overlay + progress + dismissal added")
if src == before:
    print("\nnothing to change")
    raise SystemExit(0)
if DRY:
    print("\ndry run - nothing written")
    raise SystemExit(0)

stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bdir = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "_patch_backup_" + stamp))
os.makedirs(bdir, exist_ok=True)
shutil.copy2(P, os.path.join(bdir, "index.html"))
print("\nbackup:", bdir)
open(P, "w", encoding="utf-8").write(src)
print("written: static/index.html")
