"""A category filter on "Data per site".

The store knows pages and bytes per domain; only the CATALOGUE knows whether a domain is a
manufacturer, a news site or a government portal. So the join happens server-side, on the rows the
panel already fetches - the alternative is making the browser pull the whole 546KB catalogue to
colour one column.

Two details that matter:

  * rows are COPIED before the category is attached. `_ingest_sources` returns the SHARED cache
    entry now, so mutating a row in place would quietly poison every other consumer of that scan.
  * a domain the catalogue has never heard of is labelled "uncatalogued", not folded into a bucket.
    Some sites in the store have no catalogue row at all (18 of them, holding ~16k pages), and
    filing them under a category would invent a fact about them.

Run:  python3 dc_catfilter.py [--dry-run]
"""

import ast
import datetime as dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv
API = os.path.join(ROOT, "api.py")
HTML = os.path.join(ROOT, "static", "index.html")

notes = []

# ── server: attach the catalogue category ────────────────────────────────────
src = open(API, encoding="utf-8").read()
before_api = src

HELPER = '''

def _with_category(rows):
    """Attach the operator category to each per-site store row.

    Rows are COPIED, never mutated: `_ingest_sources` hands back the SHARED cache entry, and
    writing into it would poison every other consumer of that one full-corpus scan.

    A domain with no catalogue row is labelled "uncatalogued" rather than filed under a bucket -
    the store holds sites the catalogue has never heard of, and guessing a category for them would
    invent a fact. Failing to reach the catalogue returns the rows unchanged, so the panel loses a
    filter rather than its data.
    """
    if not isinstance(rows, list):
        return rows
    try:
        from .db import SessionLocal
        with SessionLocal() as db:
            cat = {}
            for s in db.scalars(select(Source)).all():
                d = (s.domain or "").strip().lower().rstrip(".")
                if d.startswith("www."):
                    d = d[4:]
                if d:
                    cat[d] = (s.category or "", ui_category(s.category) or "")
    except Exception:  # noqa: BLE001 - the filter is a convenience; the numbers are the point
        return rows
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        d = str(r.get("source_id") or "").strip().lower().rstrip(".")
        if d.startswith("www."):
            d = d[4:]
        raw, lbl = cat.get(d, ("", ""))
        r2 = dict(r)
        r2["category"] = raw
        r2["category_label"] = lbl or "uncatalogued"
        out.append(r2)
    return out

'''

if "_with_category(" in src:
    notes.append("server-side category join already present")
else:
    anchor = "def _data_sources_live(limit: int) -> dict:"
    assert anchor in src, "ANCHOR MISSING: _data_sources_live"
    src = src.replace(anchor, HELPER.strip("\n") + "\n\n\n" + anchor, 1)
    old = 'return {"reachable": True, "rows": _ingest_sources(limit=limit)}'
    assert old in src, "ANCHOR MISSING: _data_sources_live body"
    src = src.replace(old, 'return {"reachable": True, "rows": _with_category(_ingest_sources(limit=limit))}', 1)
    notes.append("attached category + category_label to every per-site row")

ast.parse(src)

# ── client: the control and the filtering ────────────────────────────────────
html = open(HTML, encoding="utf-8").read()
before_html = html

if 'id="perSiteCat"' in html:
    notes.append("category filter control already present")
else:
    old = '''      <input id="perSiteFilter" placeholder="site…" style="width:180px">
      <span class="grow"></span>'''
    new = '''      <input id="perSiteFilter" placeholder="site…" style="width:180px">
      <!-- Options are built from the rows on screen, so a category with nothing in it is never
           offered - an empty filter that returns nothing reads as a broken panel. -->
      <select id="perSiteCat" title="filter by what kind of site it is"><option value="">all categories</option></select>
      <span class="grow"></span>'''
    assert old in html, "ANCHOR MISSING: perSiteFilter row"
    html = html.replace(old, new, 1)
    notes.append("added the category select")

    old2 = '''  const q = ($("#perSiteFilter") && $("#perSiteFilter").value || "").trim().toLowerCase();
  const doneOnly = false;   // filter removed with the Status column
  let rows = PERSITE.rows.filter(r => (r.pages || 0) > 0);
  if(q) rows = rows.filter(r => String(r.source_id||"").toLowerCase().includes(q));'''
    new2 = '''  const q = ($("#perSiteFilter") && $("#perSiteFilter").value || "").trim().toLowerCase();
  const doneOnly = false;   // filter removed with the Status column
  let rows = PERSITE.rows.filter(r => (r.pages || 0) > 0);

  /* Rebuild the category options from the rows that actually have pages, keeping whatever is
     selected. Offering a category with nothing behind it produces an empty table that reads as a
     broken panel rather than as an honest "none of these". */
  const sel = $("#perSiteCat");
  if(sel){
    const counts = {};
    rows.forEach(r => { const c = r.category_label || "uncatalogued"; counts[c] = (counts[c]||0)+1; });
    const names = Object.keys(counts).sort();
    const sig = names.map(n => n+":"+counts[n]).join("|");
    if(sel.dataset.sig !== sig){
      const keep = sel.value;
      sel.innerHTML = `<option value="">all categories (${fmt(rows.length)})</option>`
        + names.map(n => `<option value="${esc(n)}">${esc(n)} (${fmt(counts[n])})</option>`).join("");
      sel.value = names.indexOf(keep) >= 0 ? keep : "";
      sel.dataset.sig = sig;
    }
    if(sel.value) rows = rows.filter(r => (r.category_label || "uncatalogued") === sel.value);
  }
  if(q) rows = rows.filter(r => String(r.source_id||"").toLowerCase().includes(q));'''
    assert old2 in html, "ANCHOR MISSING: renderPerSite filter block"
    html = html.replace(old2, new2, 1)
    notes.append("filtering wired into renderPerSite")

    # the control has to re-render on change; follow however the text filter is bound
    bound = False
    for old3, new3 in [
        ('$("#perSiteFilter").oninput = renderPerSite;',
         '$("#perSiteFilter").oninput = renderPerSite;\n$("#perSiteCat").onchange = renderPerSite;'),
        ('$("#perSiteFilter").addEventListener("input", renderPerSite);',
         '$("#perSiteFilter").addEventListener("input", renderPerSite);\n'
         '$("#perSiteCat").addEventListener("change", renderPerSite);'),
    ]:
        if old3 in html:
            html = html.replace(old3, new3, 1)
            bound = True
            notes.append("change handler bound alongside the text filter")
            break
    if not bound:
        # No existing binding found - attach our own next to the canary, which runs last.
        html = html.replace('document.body.setAttribute("data-js", "ok");',
                            'var _psc = document.getElementById("perSiteCat");\n'
                            'if(_psc) _psc.addEventListener("change", renderPerSite);\n'
                            'document.body.setAttribute("data-js", "ok");', 1)
        notes.append("change handler attached at boot (no existing binding found)")

print("\n".join("  " + s for s in notes))
if src == before_api and html == before_html:
    print("\nnothing to change")
    raise SystemExit(0)
if DRY:
    print("\ndry run - nothing written")
    raise SystemExit(0)

stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
bdir = os.path.abspath(os.path.join(ROOT, "..", "..", "..", "_patch_backup_" + stamp))
os.makedirs(bdir, exist_ok=True)
shutil.copy2(API, os.path.join(bdir, "api.py"))
shutil.copy2(HTML, os.path.join(bdir, "index.html"))
print("\nbackup:", bdir)
open(API, "w", encoding="utf-8").write(src)
open(HTML, "w", encoding="utf-8").write(html)
print("written: api.py, static/index.html")
