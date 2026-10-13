"""A tiny, dependency-free HTML dashboard for the ingested page bundles.

Reads ``data/output/ingested.ndjson`` (the audit trail every run writes) and renders each raw
harvested page — clean text and viewable artifacts (screenshot / images / PDF).

Navigation is **site → depth → pages**: the landing lists each crawled site as a folder; click a
site to see its depth levels; click a depth (e.g. d2) to see every page scraped at that depth, with
all its data. Cards show only url + crawl timestamp + the captured data (no source/tier labels).
"""
from __future__ import annotations

import html
import json
import os
from collections import Counter, defaultdict, deque
from urllib.parse import quote, urlsplit

from crawler import config
from crawler.canonicalize import registered_domain

# Dashboard shows the most-recent N ingested pages. A whole-file read of ingested.ndjson (which can
# be hundreds of MB, each line carrying inline HTML) OOM'd 9090 under big multi-site crawls.
_DASH_MAX = int(os.environ.get("INGEST_DASH_MAX", "3000"))
# When Postgres is the store we show EVERY captured doc (all sites), not just the ndjson tail.
_PG_MAX = int(os.environ.get("INGEST_DASH_PG_MAX", "50000"))


def _pg_record(row) -> dict:
    """A `documents` card row (pgstore._CARD_SQL order) → the {ingested_at, document} record shape
    the renderer already speaks. No inline html (html_len only; /raw-html fetches the full html)."""
    (doc_id, url, title, main_text, fetched, depth, parent,
     served_by, served_from, atts, html_len, archived_at) = row
    return {"ingested_at": fetched, "document": {
        "document_id": doc_id, "url": url, "title": title, "main_text": main_text or "",
        "fetched_at": fetched, "depth": depth, "parent_url": parent,
        "served_by": served_by, "served_from": served_from, "archived_at": archived_at,
        "attachments": atts or [], "html": "", "html_len": html_len or 0}}


def load_records(limit: int = _DASH_MAX) -> list[dict]:
    """Ingested records with the heavy inline `html` stripped (its length is kept in `html_len` for
    the size stat + pill; /raw-html re-reads the full html on demand).

    Postgres `documents` is the durable store → when it's configured we return EVERY captured doc
    (newest-first, all sites). Otherwise (tests / ndjson-only) we stream the last `limit` ndjson
    lines via deque(maxlen) so neither the file size nor the inline HTML blows up RAM.

    ponytail: the PG branch loads all rows' card fields (no html) into RAM per request — fine
    ≤ ~50k docs. Past that, switch to per-view aggregate GROUP BY source_id/depth queries."""
    from . import pgstore
    if pgstore.has_db():
        return [_pg_record(r) for r in pgstore.fetch_documents(limit=_PG_MAX)]
    path = config.OUTPUT_DIR / "ingested.ndjson"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in deque(fh, maxlen=limit):        # only the last `limit` lines are retained
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            doc = rec.get("document")
            if isinstance(doc, dict) and doc.get("html"):
                doc["html_len"] = len(doc["html"])  # keep size for the stat + pill…
                doc["html"] = ""                    # …drop the heavy content (memory)
            out.append(rec)
    return out


def _html_len(doc: dict) -> int:
    return int(doc.get("html_len") or len(doc.get("html") or ""))


def crawled_urls_for_host(host: str) -> set[str]:
    """Every crawled URL for ``host`` (registered domain), normalized. Used by the coverage
    cross-verify so the % reflects the FULL crawl, not the bounded dashboard view. Reads Postgres
    `documents` when configured (source_id = registered domain), else scans the ndjson."""
    from crawler import sitemap

    from . import pgstore
    if pgstore.has_db():
        return {sitemap.norm_url(u) for u in pgstore.urls_for_source(host)}
    path = config.OUTPUT_DIR / "ingested.ndjson"
    out: set[str] = set()
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if '"url"' not in line:
                continue
            try:
                url = json.loads(line).get("document", {}).get("url", "")
            except json.JSONDecodeError:
                continue
            if url and _host(url) == host:
                out.add(sitemap.norm_url(url))
    return out


def _esc(v) -> str:
    return html.escape(str(v))


def _kb(n: int) -> str:
    return f"{n/1024:.0f} KB" if n >= 1024 else f"{n} B"


def _host(url: str) -> str:
    # Group by REGISTERED domain (collapses www.) so a site crawled under both www.x and apex x
    # shows as ONE folder. Display-only — stored fetch URLs keep their www (see canonicalize.py).
    return registered_domain(url) or (urlsplit(url or "").hostname or "?").lower()


def _dkey(depth) -> str:
    """Query-safe depth key ('none' for a record with no depth)."""
    return "none" if depth is None else str(depth)


def _depth_badge(depth) -> str:
    return (f'<span class="depth">d{_esc(depth)}</span>' if depth is not None
            else '<span class="depth none">d—</span>')


def _raw_metrics(doc: dict) -> str:
    """One row of raw-data-captured counts per page (L1 captures text + html + pdf only)."""
    parts = [
        f'📝 {len(doc.get("main_text") or ""):,} chars',
        f'🌐 {_kb(_html_len(doc))} html',
    ]
    pdfs = [a for a in doc.get("attachments", []) if a.get("storage_path")]
    if pdfs:
        parts.append(f'📄 {len(pdfs)} pdf')
    return '<div class="raw">' + "".join(f'<span class="m">{_esc(p)}</span>' for p in parts) + "</div>"


def _page_card(item: dict) -> str:
    """One harvested page: url + crawl timestamp + its captured data (text + html + pdf)."""
    doc = item["document"]

    arts = ""
    pdfs = ""
    for att in doc.get("attachments", []):
        if att.get("storage_path"):
            pdfs += (f'<a class="pill" href="/artifact?path={quote(att["storage_path"])}" '
                     f'target="_blank">📄 PDF ({len((att.get("extracted_text") or ""))} chars)</a>')
    if _html_len(doc) and doc.get("document_id"):
        pdfs += (f'<a class="pill html" href="/raw-html?doc_id={quote(doc["document_id"])}" '
                 f'target="_blank">🌐 raw HTML ({_kb(_html_len(doc))})</a>')

    url = doc.get("url", "")
    title = doc.get("title") or ""
    summary = (doc.get("main_text") or "")[:240]
    fetched = doc.get("fetched_at") or ""
    parent = doc.get("parent_url")
    backtrack = f'<div class="from">↑ from: {_esc(parent)}</div>' if parent else ''

    # Which tier served this page — badge shown only when it wasn't the normal live fetch.
    served = doc.get("served_by")
    tier = ""
    if served and served != "c1_live":
        label = {"c2_wayback": "C2 · wayback", "c2_memento": "C2 · memento",
                 "c3_camofox": "C3 · camofox", "feed_api": "feed"}.get(served, served)
        sf = doc.get("served_from")
        tier = (f'<a class="tier" href="{_esc(sf)}" target="_blank" title="{_esc(sf)}">{_esc(label)}</a>'
                if sf else f'<span class="tier">{_esc(label)}</span>')

    # For an ARCHIVE doc the snapshot date is the real date — it's what the operator asked for, and
    # what makes two versions of one url distinguishable. fetched_at is when we PULLED it, which for
    # a 2015 capture is a misleading "crawled today", so demote it to the hover.
    archived = doc.get("archived_at")
    if archived:
        date_chip = (f'<span class="date" title="pulled from the archive at {_esc(fetched)}">'
                     f'&#127963; archived {_esc(archived)}</span>')
    else:
        date_chip = f'<span class="date">&#128339; crawled {_esc(fetched)}</span>'

    return f"""
    <div class="card">
      <div class="top">{_depth_badge(doc.get("depth"))}{tier}{date_chip}</div>
      <a class="url" href="{_esc(url)}" target="_blank">{_esc(url)}</a>
      {f'<div class="subtitle">{_esc(title)}</div>' if title else ''}
      {backtrack}
      {_raw_metrics(doc)}
      <div class="summary">{_esc(summary)}</div>
      <div class="arts">{arts}{pdfs}</div>
    </div>"""


def _row(href: str, badge: str, label: str, count_txt: str) -> str:
    return (f'<a class="row" href="{href}">{badge}'
            f'<span class="rlabel">{_esc(label)}</span>'
            f'<span class="rcount">{_esc(count_txt)}</span></a>')


def _site_index(by_host: dict) -> str:
    """Landing: one folder per crawled site."""
    from . import pgstore
    can_dl = pgstore.has_db()          # the zip is a Postgres dump → only offer it when a DB is live
    rows = ""
    for host in sorted(by_host):
        recs = by_host[host]
        depths = [r["document"].get("depth") for r in recs if r["document"].get("depth") is not None]
        span = f"depth {min(depths)}–{max(depths)}" if depths else "depth —"
        dl = (f'<a class="dl" href="/download?host={quote(host)}" '
              f'title="download this site as a zip: html + text + pdf + a Postgres dump (documents.sql) '
              f'they can spin up in their own DB">⬇ zip</a>') if can_dl else ""
        # Per-site delete: drops this site's Postgres rows + MinIO blobs. Data you no longer need.
        # The host/count ride on data-* attributes and the handler is bound in JS — NOT an inline
        # onclick. json.dumps emits its OWN double quotes, so onclick="delSite(this,"abeking.com",…)"
        # closed the attribute at the first one: every button rendered as a no-op that looked fine.
        rm = (f'<button class="del" data-host="{_esc(host)}" data-n="{len(recs)}" '
              f'title="delete ALL captured data for this site (pages + PDFs). Cannot be undone.">'
              f'🗑 delete</button>') if can_dl else ""
        rows += (f'<div class="siterow">'
                 + _row(f"/?folder={quote(host)}", '<span class="folder">📁</span>',
                        host, f"{len(recs)} pages · {span}")
                 + f'<a class="cov" href="/coverage?host={quote(host)}" '
                   f'title="cross-verify crawled pages vs the site sitemap">🔍 vs sitemap</a>{dl}{rm}</div>')
    total_pages = sum(len(v) for v in by_host.values())
    dall = (f'<button class="delall" data-sites="{len(by_host)}" data-pages="{total_pages}" '
            f'title="delete EVERY site&#39;s captured data (pages + PDFs). Cannot be undone.">'
            f'🗑 delete all</button>') if (can_dl and by_host) else ""
    return (f'<div class="idx"><h2>📁 Sites ({len(by_host)}){dall}</h2>{rows or _EMPTY}</div>'
            + _DELETE_JS)


def render_coverage(host: str) -> str:
    """On-demand cross-verify page: crawled pages vs the site's sitemap (sitemap = check only,
    never used to seed links). Lists sitemap URLs we did NOT crawl."""
    from crawler import sitemap
    crawled = crawled_urls_for_host(host)
    cov = sitemap.coverage(host, crawled)
    if cov["sitemap_total"] == 0:
        body = ('<p class="empty">No usable sitemap found for this site — coverage can\'t be '
                'computed (this is normal for many gov/portal sites). Crawled '
                f'<b>{cov["crawled_total"]}</b> pages.</p>')
    else:
        missed = cov["missed"]
        cap = 300
        shown = missed[:cap]
        more = f' <span style="color:#64748b">(+{len(missed)-cap} more)</span>' if len(missed) > cap else ""
        miss_rows = "".join(
            f'<a class="row" href="https://{_esc(m)}" target="_blank">'
            f'<span class="rlabel">{_esc(m)}</span></a>' for m in shown) or _EMPTY
        pct = cov["coverage_pct"]
        color = "#34d399" if pct >= 80 else ("#fbbf24" if pct >= 40 else "#f87171")
        body = (f'<div class="idx"><h2 style="color:{color}">Coverage: {pct}% '
                f'&nbsp;·&nbsp; crawled {cov["covered"]}/{cov["sitemap_total"]} sitemap URLs '
                f'(crawled {cov["crawled_total"]} pages total)</h2>'
                f'<h2>Sitemap URLs NOT crawled ({len(missed)}){more}</h2>{miss_rows}</div>')
    crumb = (f'<div class="crumb"><a href="/">← sites</a> · <b>{_esc(host)}</b> · '
             f'<a href="/?folder={quote(host)}">open folder</a> · cross-verify vs sitemap</div>')
    return _page(crumb + body, _std_header())


def _depth_index(host: str, recs: list) -> str:
    """One folder open: the site's depth levels."""
    by_depth = Counter(r["document"].get("depth") for r in recs)
    rows = ""
    for d in sorted(by_depth, key=lambda x: (x is None, x)):
        rows += _row(f"/?folder={quote(host)}&depth={_dkey(d)}", _depth_badge(d),
                     f"depth {d if d is not None else '—'}", f"{by_depth[d]} pages")
    crumb = f'<div class="crumb"><a href="/">← sites</a> · <b>{_esc(host)}</b> · {len(recs)} pages</div>'
    return crumb + f'<div class="idx"><h2>Depth levels</h2>{rows}</div>'


_EMPTY = '<p class="empty">No pages yet. Run a crawl, then refresh.</p>'

# Delete. A plain <a> can't issue DELETE, so this is the one bit of JS on the page. Handlers are
# BOUND here from data-* attributes rather than written into inline onclick attributes — see the
# comment in _site_index for the quoting bug that made every inline handler a silent no-op.
_DELETE_JS = """<script>
async function delSite(btn, host, n){
  if(!confirm('Delete ALL '+n+' page(s) for '+host+'?\\n\\nRemoves its Postgres rows AND its PDF/image blobs.\\nThis cannot be undone.')) return;
  btn.disabled = true; btn.textContent = '… deleting';
  try{
    const r = await fetch('/site?host='+encodeURIComponent(host)+'&confirm=DELETE', {method:'DELETE'});
    const d = await r.json();
    if(!r.ok){ alert('Delete failed: '+(d.error||r.status)); btn.disabled=false; btn.textContent='🗑 delete'; return; }
    const row = btn.closest('.siterow'); if(row) row.remove();
  }catch(e){ alert('Delete failed: '+e); btn.disabled=false; btn.textContent='🗑 delete'; }
}

// Wiping the WHOLE corpus is a different kind of click from wiping one site, so it asks twice: an
// OK/Cancel that names the totals, then a typed confirmation. The server demands its own separate
// token (confirm=DELETE-ALL) on top of both.
async function delAll(btn, sites, pages){
  if(!confirm('Delete EVERYTHING?\\n\\nAll '+pages+' page(s) across all '+sites+' site(s), plus every PDF/image blob.\\nThis cannot be undone.')) return;
  if(prompt('Irreversible. Type  DELETE ALL  to confirm:') !== 'DELETE ALL'){
    alert('Cancelled — nothing was deleted.'); return;
  }
  btn.disabled = true; btn.textContent = '… deleting all';
  try{
    const r = await fetch('/sites?confirm=DELETE-ALL', {method:'DELETE'});
    const d = await r.json();
    if(!r.ok){ alert('Delete failed: '+(d.error||r.status)); btn.disabled=false; btn.textContent='🗑 delete all'; return; }
    alert('Deleted '+d.documents_deleted+' page(s) and '+d.files_deleted+' file(s).');
    location.reload();
  }catch(e){ alert('Delete failed: '+e); btn.disabled=false; btn.textContent='🗑 delete all'; }
}

document.querySelectorAll('button.del').forEach(function(b){
  b.addEventListener('click', function(){ delSite(b, b.dataset.host, b.dataset.n); });
});
document.querySelectorAll('button.delall').forEach(function(b){
  b.addEventListener('click', function(){ delAll(b, b.dataset.sites, b.dataset.pages); });
});
</script>"""

_STYLE = """<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin:0;
          background:#0f172a; color:#e2e8f0; }
  header { padding:18px 24px; background:#111827; border-bottom:1px solid #1f2937; position:sticky; top:0; }
  h1 { margin:0 0 6px; font-size:18px; }
  h1 a { text-decoration:none; color:inherit; }
  .crumb { padding:14px 24px 0; font-size:13px; color:#cbd5e1; }
  .crumb a { color:#60a5fa; text-decoration:none; }
  .idx { padding:8px 24px 4px; }
  .idx h2 { font-size:14px; color:#94a3b8; margin:14px 0 8px; font-weight:600; }
  /* Whole-corpus delete. Sits in the Sites heading, styled hotter than the per-site button so the
     blast radius is obvious before the click, not after it. */
  .delall { margin-left:14px; padding:3px 11px; font-size:11.5px; font-weight:700; cursor:pointer;
            background:#450a0a; border:1px solid #b91c1c; border-radius:8px; color:#fca5a5;
            vertical-align:middle; font-family:inherit; }
  .delall:hover { background:#7f1d1d; border-color:#ef4444; color:#fee2e2; }
  .delall:disabled { opacity:.55; cursor:not-allowed; }
  .siterow { display:flex; align-items:stretch; gap:8px; }
  .siterow .row { flex:1; margin:5px 0; }
  .cov { display:flex; align-items:center; padding:0 12px; margin:5px 0; font-size:12px;
         background:#0b1220; border:1px solid #334155; border-radius:8px; color:#7dd3fc;
         text-decoration:none; white-space:nowrap; }
  .cov:hover { border-color:#60a5fa; }
  .dl { display:flex; align-items:center; padding:0 12px; margin:5px 0; font-size:12px;
        background:#0b2a1a; border:1px solid #14532d; border-radius:8px; color:#4ade80;
        text-decoration:none; white-space:nowrap; font-weight:600; }
  .dl:hover { border-color:#22c55e; background:#0d3520; }
  .del { display:flex; align-items:center; padding:0 12px; margin:5px 0; font-size:12px;
         background:#2a1414; border:1px solid #5a2a2a; border-radius:8px; color:#ffb3b3;
         white-space:nowrap; font-weight:600; cursor:pointer; }
  .del:hover { border-color:#ef4444; background:#3a1818; }
  .row { display:flex; align-items:center; gap:10px; padding:9px 12px; margin:5px 0;
          background:#1e293b; border:1px solid #334155; border-radius:8px;
          text-decoration:none; color:#e2e8f0; }
  .row:hover { border-color:#60a5fa; }
  .folder { font-size:16px; }
  .rlabel { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#f1f5f9; }
  .rcount { font-size:12px; color:#7dd3fc; white-space:nowrap; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(400px,1fr)); gap:14px; padding:14px 24px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }
  .top { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
  .depth { color:#0f172a; background:#7dd3fc; padding:2px 7px; border-radius:6px; font-size:11px; font-weight:700; }
  .depth.none { background:#475569; color:#cbd5e1; }
  .tier { background:#7c2d12; color:#fed7aa; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; text-decoration:none; }
  .tier:hover { background:#9a3412; }
  .date { margin-left:auto; font-size:12px; color:#64748b; }
  .url { display:block; font-weight:600; color:#93c5fd; text-decoration:none; word-break:break-all; margin:2px 0; }
  .url:hover { color:#bfdbfe; text-decoration:underline; }
  .subtitle { font-size:12.5px; color:#94a3b8; margin:2px 0; }
  .from { font-size:11px; color:#64748b; word-break:break-all; margin:3px 0; }
  .raw { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 8px; }
  .m { background:#0b1220; color:#94a3b8; padding:1px 7px; border-radius:6px; font-size:11px; border:1px solid #1e293b; }
  .summary { font-size:12.5px; color:#cbd5e1; margin-bottom:8px; }
  .arts { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .thumb { height:70px; border-radius:6px; border:1px solid #475569; }
  .pill { background:#1d4ed8; color:#fff; padding:3px 9px; border-radius:8px; font-size:11.5px; text-decoration:none; }
  .pill.html { background:#334155; }
  .empty { padding:40px; color:#94a3b8; }
  a { color:#60a5fa; }
</style>"""


def _std_header(extra: str = "") -> str:
    return ('<h1><a href="/">🛰️ Mallory Crawler — harvested pages</a> '
            '<span style="color:#64748b">(Layer 1 → Ingest)</span></h1>' + extra)


def _page(body: str, header_inner: str) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Mallory Crawler — harvested pages</title>{_STYLE}</head><body>'
            f'<header>{header_inner}</header>{body}</body></html>')


def render(folder: str | None = None, depth: str | None = None) -> str:
    records = load_records()

    # raw-capture totals (text + html + pdf — the only things L1 captures). With Postgres these come
    # from a whole-table aggregate, NOT from `records`: the card query is capped at _PG_MAX, so
    # summing the slice made every header stat freeze once the corpus passed 50k.
    from . import pgstore
    agg = None
    if pgstore.has_db():
        try:
            agg = pgstore.totals()
        except Exception:
            agg = None                          # aggregate failed → fall back to the slice sum
    if agg:
        n_total, raw_bytes, raw_chars, n_pdf = agg
    else:
        n_total = len(records)
        raw_bytes = raw_chars = n_pdf = 0
        for r in records:
            doc = r["document"]
            raw_bytes += _html_len(doc)
            raw_chars += len(doc.get("main_text") or "")
            n_pdf += sum(1 for a in doc.get("attachments", []) if a.get("storage_path"))

    by_host: dict = defaultdict(list)
    for r in records:
        by_host[_host(r["document"].get("url", ""))].append(r)

    if folder == "__all__":
        cards = "".join(_page_card(r) for r in records) or _EMPTY
        body = (f'<div class="crumb"><a href="/">← sites</a> · All {len(records)} pages</div>'
                f'<div class="grid">{cards}</div>')
    elif folder and depth is not None:
        recs = [r for r in by_host.get(folder, []) if _dkey(r["document"].get("depth")) == depth]
        cards = "".join(_page_card(r) for r in recs) or _EMPTY
        dlabel = "—" if depth == "none" else depth
        body = (f'<div class="crumb"><a href="/">← sites</a> · '
                f'<a href="/?folder={quote(folder)}">{_esc(folder)}</a> · '
                f'<b>depth {_esc(dlabel)}</b> · {len(recs)} pages</div>'
                f'<div class="grid">{cards}</div>')
    elif folder:
        body = _depth_index(folder, by_host.get(folder, []))
    else:
        # Say "newest N of TOTAL" when the card query is capped — claiming "All" while showing a
        # 50k window is the bug that made this dashboard look stuck.
        _lbl = (f'All {len(records):,} pages' if len(records) >= n_total
                else f'Newest {len(records):,} of {n_total:,} pages')
        body = (f'<div class="crumb"><a href="/?folder=__all__">📄 {_lbl} (flat grid)</a></div>'
                + _site_index(by_host))

    from . import pgstore
    src = "Postgres · documents" if pgstore.has_db() else "data/output/ingested.ndjson"
    raw_total = (f'📦 raw captured: <b>{raw_bytes/1_048_576:.1f} MB</b> html · '
                 f'<b>{raw_chars:,}</b> text chars · <b>{n_pdf}</b> pdf')
    header_inner = _std_header(
        f'<div style="margin-top:8px;font-size:12.5px;color:#cbd5e1">{raw_total}</div>'
        f'<div style="margin-top:6px;font-size:12px;color:#64748b">'
        f'{n_total:,} pages · <a href="/">sites</a> · <a href="/stats">/stats</a> · '
        f'<a href="/v1/docs">API docs</a> · {_esc(src)}</div>')
    return _page(body, header_inner)
