"""Inline HTML dashboard for the Crawler API.

Generate jobs from the seed, paste manual jobs, and run crawls — all from the browser.
The summary counters read the exact fields the crawl API returns
(fetched / kept / sent / accepted), so numbers are truthful.
"""
from __future__ import annotations

import html


def _esc(v) -> str:
    return html.escape(str(v))


def render() -> str:
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mallory Crawler API — Job Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin:0;
         background:#0f172a; color:#e2e8f0; }
  header { padding:18px 24px; background:#111827; border-bottom:1px solid #1f2937; }
  h1 { margin:0 0 4px; font-size:18px; }
  .sub { font-size:12px; color:#64748b; }
  .sub a { color:#60a5fa; }
  .stats-bar { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  .stat { background:#1e293b; padding:3px 9px; border-radius:12px; font-size:12px; }
  .toolbar { padding:16px 24px; display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
  button { background:#2563eb; color:#fff; border:none; padding:8px 18px;
           border-radius:8px; font-size:13px; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.green { background:#15803d; }
  button.purple { background:#7c3aed; }
  button.red { background:#b91c1c; }
  .llmwrap { display:flex; align-items:center; gap:4px; background:#1e293b;
             border:1px solid #334155; border-radius:8px; padding:3px 8px; }
  .llmlbl { font-size:12px; color:#94a3b8; margin-right:2px; }
  .seg { background:#0f172a; color:#94a3b8; border:1px solid #334155; padding:4px 12px;
         border-radius:6px; font-size:12px; }
  .seg.on { background:#2563eb; color:#fff; border-color:#2563eb; font-weight:600; }
  .llmdot { width:9px; height:9px; border-radius:50%; background:#64748b; margin-left:4px;
            cursor:pointer; }
  .llmdot.up   { background:#22c55e; box-shadow:0 0 6px #22c55e88; }
  .llmdot.down { background:#ef4444; box-shadow:0 0 6px #ef444488; }
  .llmdot.wait { background:#eab308; }
  .metrics { display:flex; gap:10px; flex-wrap:wrap; padding:12px 24px 0; }
  .met { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:8px 14px;
         min-width:130px; }
  .met .k { font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; }
  .met .v { font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; margin-top:2px; }
  .met .s2 { font-size:11px; color:#64748b; margin-top:1px; }
  .meter { height:6px; border-radius:4px; background:#0f172a; margin-top:6px; overflow:hidden; }
  .meter > i { display:block; height:100%; border-radius:4px; background:#22c55e; transition:width .4s ease; }
  label.chk { font-size:13px; color:#cbd5e1; cursor:pointer; user-select:none; }
  input[type=text] { background:#1e293b; color:#e2e8f0; border:1px solid #334155;
                     padding:7px 12px; border-radius:8px; font-size:13px; }
  .tabs { display:flex; gap:2px; padding:0 24px; margin-top:6px; }
  .tab { padding:8px 18px; background:#1e293b; border:1px solid #334155; border-bottom:none;
         border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; color:#94a3b8; }
  .tab.active { background:#0f172a; color:#e2e8f0; font-weight:600; }
  .pane { padding:16px 24px 32px; }
  textarea { width:100%; min-height:180px; background:#1e293b; color:#e2e8f0;
             border:1px solid #334155; border-radius:8px; padding:12px;
             font:13px/1.5 monospace; }
  select { background:#1e293b; color:#e2e8f0; border:1px solid #334155; padding:8px;
           border-radius:8px; font-size:13px; }
  .status { font-size:13px; margin-top:12px; padding:8px 12px; border-radius:8px; }
  .status.ok { background:#064e3b; color:#a7f3d0; }
  .status.err { background:#7c2d12; color:#fed7aa; }
  .status.wait { background:#1e3a5f; color:#93c5fd; }
  .resrow { margin-top:8px; padding:10px 12px; background:#1e293b; border:1px solid #334155;
            border-radius:8px; font-size:13px; display:flex; gap:16px; flex-wrap:wrap; align-items:center; }
  .resrow .jid { font-weight:600; min-width:120px; }
  .chip { padding:2px 9px; border-radius:10px; font-size:12px; background:#0f172a; }
  .chip.acc { background:#064e3b; color:#a7f3d0; }
  .chip.zero { background:#3f1d1d; color:#fecaca; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:12px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }
  .card h3 { margin:0 0 6px; font-size:14px; }
  .card .meta { font-size:11px; color:#94a3b8; margin-bottom:4px; }
  .card .urls { font-size:11px; color:#60a5fa; word-break:break-all; margin-bottom:6px; }
  .badge { display:inline-block; color:#fff; padding:2px 8px; border-radius:6px;
           font-size:11px; font-weight:600; margin-right:4px; background:#2563eb; }
  .hint { font-size:12px; color:#64748b; margin-bottom:8px; }
  code { background:#0f172a; padding:1px 6px; border-radius:4px; }
</style></head><body>
<header>
  <h1>&#128640; Mallory Crawler API <span style="color:#64748b">(Layer 1)</span></h1>
  <div class="sub">
    <a href="/health">/health</a> &middot; <a href="/v1/docs">/v1/docs</a> &middot;
    &rarr; <a href="http://localhost:9090" target="_blank">Ingest Dashboard :9090</a>
  </div>
  <div class="stats-bar" id="statsBar"></div>
</header>

<div class="metrics" id="metrics"></div>

<div class="toolbar">
  <button onclick="generateJobs()" id="btnGen">&#9889; Generate Jobs from Seed</button>
  <button onclick="runAllJobs()" id="btnRunAll" class="green" disabled>&#9654; Run All Jobs</button>
  <button onclick="stopCrawl()" id="btnStop" class="red" title="Halt the running crawl — stops both C1 (live render) and C3 (CamoFox). All crawling ceases." disabled>&#9209; Stop Crawling</button>
  <label class="chk"><input type="checkbox" id="freshnessToggle" onchange="updFresh()">
    <span id="freshnessLabel" style="color:#f87171">Freshness filter OFF</span></label>
  <label class="chk" title="C2 ARCHIVE tier — an EXCLUSIVE mode, not a fallback. ON = ONLY C2 runs: no live fetch, no browser, no C1, no C3. It reads the Wayback Machine's index for the whole domain and replays what the site looked like on your date. Give one date for 'the state as of that day', or a From+To range for 'that day's state + every version that CHANGED through the end date' (unchanged repeats are never re-sent). Captures text + html + PDFs, tagged c2_wayback with the real snapshot date. Wayback only: Memento's aggregator is gone (DNS-dead since 2025) and Common Crawl has ~1 capture/url/month with ~1% overlap, so neither can answer 'what changed between two dates'.">
    <input type="checkbox" id="waybackToggle" onchange="updWayback()">
    <span id="waybackLabel" style="color:#f87171">C2 archive OFF</span></label>
  <span id="c2Dates" style="display:none;">
    <input type="date" id="c2From" title="The date to read the archive at. Alone = the state as of the END of this day (the nearest capture at or before it)." style="width:140px;">
    <input type="date" id="c2To" title="Optional END of a range. With it you get the From-date state PLUS every CHANGED version through this day. Leave blank for a single-date read." style="width:140px;">
  </span>
  <span class="llmwrap" title="C3 CamoFox stealth engine — the WAF-block fallback (reactive)">
    <span class="llmlbl">C3 CamoFox:</span>
    <span id="camoTxt" style="font-size:12px;color:#94a3b8;">…</span>
    <span id="camoDot" class="llmdot" onclick="refreshCamo()"></span>
  </span>
  <span id="jobCount" style="font-size:13px;color:#64748b;"></span>
</div>

<div class="tabs">
  <div class="tab" onclick="showTab('batch')" id="tabBatch">Manual Batch</div>
  <div class="tab" onclick="showTab('jobs')" id="tabJobs">Generated Jobs</div>
  <div class="tab" onclick="showTab('resume')" id="tabResume">Resume</div>
</div>

<div id="paneResume" class="pane">
  <div class="hint">Crawls that <b>stopped before finishing</b> — an operator stop, a container
    restart, a dropped connection. Resuming reloads the URLs that were discovered but never
    processed, and puts the already-crawled ones straight into <i>seen</i> so they are
    <b>not fetched again</b>. (Content hashing already stopped duplicates reaching the corpus;
    what this saves is the hours spent re-fetching to get back to where you were.)</div>
  <div style="margin:10px 0;display:flex;gap:10px;align-items:center;">
    <button onclick="loadResumable()" id="btnResLoad">&#8635; Refresh</button>
    <button onclick="resumeSelected()" class="green" id="btnResume" disabled>&#9654; Resume Selected</button>
    <button onclick="clearResumable()" class="red" id="btnResClear" style="margin-left:auto;">&#128465; Delete all</button>
  </div>
  <div id="resumeResult"></div>
  <table id="resumeTable" style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="text-align:left;border-bottom:1px solid #cbd5e1;">
      <th style="padding:6px 4px;"><input type="checkbox" id="resAll" onclick="toggleAllRes(this)"></th>
      <th style="padding:6px 4px;">Job</th>
      <th style="padding:6px 4px;text-align:right;">Pages done</th>
      <th style="padding:6px 4px;text-align:right;">Still pending</th>
      <th style="padding:6px 4px;">Status</th>
      <th style="padding:6px 4px;">Last update</th>
    </tr></thead>
    <tbody id="resumeBody"><tr><td colspan="6" style="padding:10px;color:#64748b;">Click Refresh.</td></tr></tbody>
  </table>
</div>

<div id="paneBatch" class="pane">
  <div class="hint">Paste a JSON array of jobs (or a single job). Kept pages are pushed to
    the Ingest API on <b>9090</b> (Postgres + MinIO).</div>
  <textarea id="batchInput" spellcheck="false" placeholder='[{"job_id":"m1","job_type":"news","seed_urls":["https://..."],"keywords":["defence"],"max_pages":1,"max_depth":0,"render_js":false,"capture":["html","text"]}]'></textarea>
  <div style="margin-top:10px;display:flex;gap:10px;align-items:center;">
    <button onclick="runBatch()" class="green" id="btnBatch">&#9654; Run Batch</button>
    <select id="batchFwd">
      <option value="true" selected>Push to Ingest API too</option>
      <option value="false">Pull only (return records)</option>
    </select>
  </div>
  <div id="batchResult"></div>
  <div id="batchReport"></div>
</div>

<div id="paneJobs" class="pane" style="display:none">
  <div class="cards" id="joblist">
    <p style="color:#94a3b8;">Click &#9889; Generate Jobs from Seed to create crawl jobs.</p>
  </div>
</div>

<script>
window.jobs = [];

async function health() {
  try {
    const d = await (await fetch('/health')).json();
    document.getElementById('statsBar').innerHTML =
      `<span class="stat">Keywords: <b>${d.keywords}</b></span>` +
      `<span class="stat">Entities: <b>${d.entities}</b></span>` +
      `<span class="stat">Sources: <b>${d.sources}</b></span>`;
  } catch(e) {}
}

function updFresh() {
  const on = document.getElementById('freshnessToggle').checked;
  const l = document.getElementById('freshnessLabel');
  l.textContent = on ? 'Freshness filter ON' : 'Freshness filter OFF';
  l.style.color = on ? '#cbd5e1' : '#f87171';
}

function updWayback() {
  const on = document.getElementById('waybackToggle').checked;
  const l = document.getElementById('waybackLabel');
  l.textContent = on ? 'C2 archive ON — ONLY C2 runs' : 'C2 archive OFF';
  l.style.color = on ? '#a7f3d0' : '#f87171';
  document.getElementById('c2Dates').style.display = on ? 'inline' : 'none';
}
function _wayback() { return document.getElementById('waybackToggle').checked; }

// C2 is an EXCLUSIVE MODE and the DATE is what selects it: a job carrying archive_date routes to
// the archive runner server-side and never reaches the browser pool. So the toggle's whole job is
// to stamp the dates onto each job — there is no separate "mode" flag on the wire.
function _c2apply(job) {
  if (!_wayback()) return job;
  const from = document.getElementById('c2From').value;
  const to   = document.getElementById('c2To').value;
  const j = Object.assign({}, job, {archive_date: from});
  if (to) j.archive_to = to;
  // The API REJECTS freshness_days on an archive job (it measures staleness against now(), so it
  // would drop every archived page), and EVERY generated job carries it. Strip it here so a
  // generated job can be dropped straight into archive mode; a hand-rolled job still gets the 422.
  delete j.freshness_days;
  return j;
}

// Guard the one input the operator can get wrong before any request is sent.
function _c2ok() {
  if (!_wayback()) return true;
  if (!document.getElementById('c2From').value) {
    alert('C2 archive mode is ON — pick the archive date first.\\n\\n' +
          'One date = the state as of that day. Add the second for a range ' +
          '(that day + every version that changed through it).');
    return false;
  }
  return true;
}

function showTab(t) {
  document.getElementById('tabBatch').className  = t==='batch' ?'tab active':'tab';
  document.getElementById('tabJobs').className   = t==='jobs'  ?'tab active':'tab';
  document.getElementById('tabResume').className = t==='resume'?'tab active':'tab';
  document.getElementById('paneBatch').style.display  = t==='batch' ?'block':'none';
  document.getElementById('paneJobs').style.display   = t==='jobs'  ?'block':'none';
  document.getElementById('paneResume').style.display = t==='resume'?'block':'none';
  if (t==='resume') loadResumable();
}

// ── Resume ──────────────────────────────────────────────────────────────────
// job_id and status are operator-supplied and go into innerHTML below, so they are escaped.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
let RESUMABLE = [];
function toggleAllRes(cb) {
  document.querySelectorAll('.resChk').forEach(c => { c.checked = cb.checked; });
  syncResumeBtn();
}
function syncResumeBtn() {
  const n = document.querySelectorAll('.resChk:checked').length;
  const b = document.getElementById('btnResume');
  b.disabled = n === 0;
  b.textContent = n ? '▶ Resume Selected (' + n + ')' : '▶ Resume Selected';
  const c = document.getElementById('btnResClear');
  if (c) c.disabled = !RESUMABLE.length;   // nothing saved → nothing to delete
}
async function loadResumable() {
  const body = document.getElementById('resumeBody');
  body.innerHTML = '<tr><td colspan="6" style="padding:10px;color:#64748b;">Loading…</td></tr>';
  try {
    const r = await fetch('/v1/resumable');
    const j = await r.json();
    RESUMABLE = j.jobs || [];
    if (!RESUMABLE.length) {
      body.innerHTML = '<tr><td colspan="6" style="padding:10px;color:#64748b;">'
        + 'Nothing to resume — every crawl finished cleanly.</td></tr>';
      syncResumeBtn(); return;
    }
    body.innerHTML = RESUMABLE.map(x => {
      // A job with no stored config cannot be resumed by id alone; say so instead of offering a
      // button that would fail.
      const chk = x.has_config
        ? '<input type="checkbox" class="resChk" value="' + esc(x.job_id) + '" onclick="syncResumeBtn()">'
        : '<span title="no stored job config">&#8212;</span>';
      return '<tr style="border-bottom:1px solid #e2e8f0;">'
        + '<td style="padding:6px 4px;">' + chk + '</td>'
        + '<td style="padding:6px 4px;font-family:monospace;">' + esc(x.job_id) + '</td>'
        + '<td style="padding:6px 4px;text-align:right;">' + (x.pages_done||0) + '</td>'
        + '<td style="padding:6px 4px;text-align:right;font-weight:600;">' + (x.pending||0) + '</td>'
        + '<td style="padding:6px 4px;">' + esc(x.status||'') + '</td>'
        + '<td style="padding:6px 4px;color:#64748b;">' + esc(x.updated_at||'') + '</td></tr>';
    }).join('');
  } catch (e) {
    body.innerHTML = '<tr><td colspan="6" style="padding:10px;color:#b91c1c;">'
      + esc(e.message) + '</td></tr>';
  }
  syncResumeBtn();
}
async function resumeSelected() {
  const ids = [...document.querySelectorAll('.resChk:checked')].map(c => c.value);
  if (!ids.length) return;
  const out = document.getElementById('resumeResult');
  const btn = document.getElementById('btnResume');
  btn.disabled = true;
  out.innerHTML = '<div class="hint">Resuming ' + ids.length + ' job(s)…</div>';
  try {
    const r = await fetch('/v1/crawl/resume', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({job_ids: ids, background: true, forward_to_ingest: true})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    const tot = Object.values(j.pending || {}).reduce((a,b) => a + (b.pending||0), 0);
    out.innerHTML = '<div class="hint">Resumed <b>' + (j.resumed||[]).join(', ')
      + '</b> — ' + tot + ' pending URL(s) re-queued. Already-crawled pages are skipped.</div>';
    loadResumable();
  } catch (e) {
    out.innerHTML = '<div class="hint" style="color:#b91c1c;">' + esc(e.message) + '</div>';
  }
  syncResumeBtn();
}

async function clearResumable() {
  const n = RESUMABLE.length;
  if (!n) return;
  // Destructive and not undoable, so name the real consequence rather than "are you sure?" — the
  // pages are safe, the saved position is not.
  // Backslashes are DOUBLED below. This whole page is a plain (non-raw) Python triple-quoted
  // string, so a single backslash-n is consumed by Python and arrives here as a real newline —
  // which ends the JS string literal and takes the ENTIRE script block with it, leaving every tab
  // on this page dead. Doubling it hands JS the escape it expects.
  if (!confirm('Delete the saved resume position for all ' + n + ' job(s)?\\n\\n'
      + 'Crawled pages stay in the corpus. What is lost is the frontier — re-running these jobs '
      + 'will walk the sites from the start again.')) return;
  const out = document.getElementById('resumeResult');
  const btn = document.getElementById('btnResClear');
  btn.disabled = true;
  try {
    const r = await fetch('/v1/resumable/clear', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'});
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    out.innerHTML = '<div class="hint">Cleared <b>' + j.jobs + '</b> job(s), '
      + j.rows + ' frontier row(s).</div>';
    loadResumable();
  } catch (e) {
    out.innerHTML = '<div class="hint" style="color:#b91c1c;">' + esc(e.message) + '</div>';
  }
  btn.disabled = false;
}

// Render one truthful result row from a crawl summary.
function resRow(jobId, s) {
  const acc = s.accepted || 0;
  const cls = acc > 0 ? 'acc' : 'zero';
  const fwd = (s.forwarded_to || []).length
      ? `<span class="chip">&#128228; ${(s.forwarded_to).join(', ')}</span>` : '';
  const errN = s.errors || 0;
  const ebr = s.errors_by_reason
      ? Object.entries(s.errors_by_reason).map(([k,v]) => `${k}: ${v}`).join(', ') : '';
  const errChip = errN
      ? `<span class="chip zero" title="real fetch failures: ${ebr}">errors ${errN}</span>` : '';
  const skipChip = (s.skipped||0)
      ? `<span class="chip" title="queued URLs skipped WITHOUT a fetch (host cooldown / known-gone / robots) — not failures">skipped ${s.skipped}</span>` : '';
  const recChip = (s.recovered||0)
      ? `<span class="chip acc" title="blocked pages RECOVERED via C2 archive / C3 CamoFox / official API">recovered ${s.recovered}</span>` : '';
  const trap = (s.trap_skipped||0)
      ? `<span class="chip" title="URLs dropped by loop/calendar/size trap guards">traps ${s.trap_skipped}</span>` : '';
  // `{"summary": {}}` is what a dispatch that produced nothing returns, so `||0` printed four
  // confident zeros for a job that never reported anything at all. num() keeps "reported zero"
  // and "reported nothing" apart, the same rule the progress block above already follows.
  const num = v => (v === null || v === undefined) ? '—' : v;
  // The fetched -> kept gap, itemised. These three counters ARE the whole difference, and the API
  // has always returned them — they just were not on screen, so "fetched 178 / kept 98" read as 80
  // pages silently lost when in fact none were: they were off-topic, unchanged since the last
  // crawl, or the same content already captured under another URL this run.
  const gate = s.dropped_by_gate||0, unch = s.skipped_unchanged||0, dupe = s.skipped_duplicate||0;
  const gap = gate + unch + dupe;
  const gapChip = gap ? `<span class="chip" title="fetched but not stored, and why — off-topic (keep-gate): ${gate}; unchanged since the last crawl: ${unch}; duplicate of a page already captured this run: ${dupe}">not stored ${gap}</span>` : '';
  return `<div class="resrow">
      <span class="jid">${esc(jobId)}</span>
      <span class="chip" title="pages successfully retrieved — fetch EVENTS, so a URL re-fetched after an escalation counts each time">fetched ${num(s.fetched)}</span>
      <span class="chip" title="unique pages actually stored. fetched minus kept is the 'not stored' chip, never an unexplained loss">kept ${num(s.kept)}</span>
      <span class="chip" title="stored pages forwarded to the ingest API">sent ${num(s.sent)}</span>
      <span class="chip ${cls}" title="pages the ingest API confirmed it wrote">accepted ${acc}</span>
      ${gapChip}${errChip}${skipChip}${recChip}${trap}${fwd}
    </div>`;
}

// Per-site batch-completion report (kept / retryable / terminal), fetched from /v1/batch/report on
// batch finish. Reuses the .resrow/.chip classes above.
async function loadBatchReport(){
  const el = document.getElementById('batchReport'); if(!el) return;
  try{
    const rep = await (await fetch('/v1/batch/report')).json();
    const t = rep.totals||{};
    if(!(rep.sites||[]).length && !(t.retryable||t.terminal)){ el.innerHTML=''; return; }
    let h = `<div class="resrow"><span class="jid">outcomes</span>
       <span class="chip acc" title="pages successfully captured">kept ${t.kept||0}</span>
       <span class="chip" title="errored but resolvable — retried in-run / C3 / fresh IP / next run">retryable ${t.retryable||0}</span>
       <span class="chip zero" title="dropped, unresolvable even after retries (404/410/robots/ssl…)">terminal ${t.terminal||0}</span>
       <span class="chip" title="fetched but gate-dropped (off-keyword)">off-keyword ${t.dropped_by_gate||0}</span></div>`;
    for(const s of (rep.sites||[])){
      const sub = Object.entries(s.reasons||{}).map(([k,v])=>`${k}: ${v}`).join(', ');
      // Show the ATTEMPT COUNT and what was already tried. Reason alone read as "gave up on the
      // first try" even when the full C1 -> impersonate -> C3 ladder had run (fail_count proves it).
      // These are CRAWLED URLs and the failure text that came back with them, i.e. third-party
      // content. Unescaped they let any site we crawl put script into this dashboard, and the
      // href would honour a javascript: URL. esc() is defined above and is used correctly in the
      // Resume table; it just was not applied here.
      const safeHref = u => /^https?:\\/\\//i.test(String(u||'')) ? esc(u) : '#';
      const urls = (s.urls||[]).map(u=>{
        const tries = u.attempts>0 ? ` · ${u.attempts} attempt${u.attempts>1?'s':''}` : '';
        const det = u.detail && u.detail!==u.reason
          ? `<div style="opacity:.65;margin:1px 0 5px 12px">↳ ${esc(u.detail)}</div>` : '';
        return `<div><a href="${safeHref(u.url)}" target="_blank" style="color:#60a5fa">${esc(u.url)}</a>`
             + ` — <b>${esc(u.reason)}</b> (${esc(u.bucket)}${u.status?' '+esc(u.status):''}${tries})</div>${det}`;
      }).join('');
      h += `<div class="resrow"><span class="jid">${esc(s.host)}</span>
        <span class="chip acc">kept ${s.kept}</span>
        <span class="chip"${s.retryable?` title="${sub}"`:''}>retryable ${s.retryable}</span>
        <span class="chip zero"${s.terminal?` title="${sub}"`:''}>terminal ${s.terminal}</span>
        ${urls?`<a href="#" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='block'?'none':'block';return false">urls</a><div style="display:none;font-size:11px;opacity:.75;margin-top:4px">${urls}</div>`:''}</div>`;
    }
    el.innerHTML = h;
  }catch(e){}
}

async function runBatch() {
  if (!_c2ok()) return;
  const input = document.getElementById('batchInput').value.trim();
  const forward = document.getElementById('batchFwd').value === 'true';
  const freshnessOn = document.getElementById('freshnessToggle').checked;
  const el = document.getElementById('batchResult');
  const btn = document.getElementById('btnBatch');
  if (!input) { el.innerHTML = '<div class="status err">Paste job JSON first.</div>'; return; }
  let parsed;
  try { parsed = JSON.parse(input); }
  catch(e) { el.innerHTML = `<div class="status err">Invalid JSON: ${e.message}</div>`; return; }
  let jobs = Array.isArray(parsed) ? parsed
           : (parsed.jobs && Array.isArray(parsed.jobs)) ? parsed.jobs : [parsed];
  // Freshness OFF -> don't drop old articles before the keyword gate.
  if (!freshnessOn) jobs = jobs.map(j => ({...j, freshness_days: null}));

  const body = {jobs: jobs.map(_c2apply), forward_to_ingest: forward, background: true};

  btn.disabled = true;
  // The crawl runs in the BACKGROUND — it now runs to frontier-exhaustion (no 30-min chop), which
  // can take hours. This POST returns immediately; pollMetrics() watches /v1/metrics and flips the
  // banner to "finished" when the batch goes running -> idle. _batchWait stays true until then.
  window._batchWait = true; window._sawRunning = false;
  el.innerHTML = '<div class="status wait">&#9203; Starting crawl…</div>';
  try {
    const r = await fetch('/v1/crawl/batch', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!r.ok) {
      window._batchWait = false;
      const t = await r.text();
      el.innerHTML = `<div class="status err">HTTP ${r.status}: ${t.substring(0,300)}</div>`;
      btn.disabled = false; return;
    }
    const d = await r.json();
    if (d.started === false) {              // single-flight: a batch is already running
      window._batchWait = false;
      el.innerHTML = `<div class="status err">${d.error||'a batch is already running'}</div>`;
      btn.disabled = false; return;
    }
    // Started in the background — leave _batchWait true; pollMetrics reconciles on finish.
    el.innerHTML = `<div class="status wait">&#9203; Crawling ${d.total||''} job(s) in the background… runs to completion; progress in the strip above, Stop to halt.</div>`;
  } catch(e) {
    window._batchWait = false;
    el.innerHTML = `<div class="status err">${e.message}</div>`;
  }
  btn.disabled = false;
}

// ── live crawler + hardware metrics strip ──
function _meterColor(p){ return p>=90?'#ef4444':p>=70?'#eab308':'#22c55e'; }
function _tile(k,v,sub,pct){
  const bar = (pct==null) ? '' :
    `<div class="meter"><i style="width:${Math.min(100,pct)}%;background:${_meterColor(pct)}"></i></div>`;
  return `<div class="met"><div class="k">${k}</div><div class="v">${v}</div>`+
         (sub?`<div class="s2">${sub}</div>`:'')+bar+`</div>`;
}
// Speed tile: INSTANTANEOUS pages/s from the delta between successive polls — the server's
// cumulative average alone would hide stalls. window._spdLast = {pages, t} is the delta
// baseline; it resets when a new run starts (running flips false→true, or the page counter
// goes backwards), so a re-crawl shows a fresh live rate instead of a stale/huge delta.
function _speedTile(pg){
  if (!pg) return _tile('Speed', '—', 'idle', null);           // older backend — no progress block
  const now = Date.now(), running = !!pg.running;
  const pages = Number(pg.pages)||0, avg = Number(pg.pages_per_sec)||0;
  if (running && !window._spdWasRun) window._spdLast = null;   // idle→running: new run/recrawl
  if (window._spdLast && pages < window._spdLast.pages) window._spdLast = null;  // counter reset → new batch
  window._spdWasRun = running;
  if (!running){                                               // idle: last run's average, never a stale inst
    window._spdLast = null;
    return _tile('Speed', avg>0 ? 'avg '+avg.toFixed(1)+' pages/s' : 'idle', avg>0 ? 'idle' : '', null);
  }
  const last = window._spdLast;
  window._spdLast = {pages: pages, t: now};
  if (!last) return _tile('Speed', '…', 'measuring', null);    // first poll of this run — no delta yet
  const dt = (now - last.t)/1000;
  const inst = dt>0 ? (pages - last.pages)/dt : 0;
  const msp = inst>0 ? Math.round(1000/inst)+' ms/page' : '—';
  return _tile('Speed', inst.toFixed(1)+' pages/s', `${msp} · avg ${avg.toFixed(1)}/s`, null);
}
async function pollMetrics(){
  try{
    const m = await (await fetch('/v1/metrics')).json();
    const cpu = Math.round(m.cpu_percent), r = m.ram, p = m.pool, b = m.batch;
    // Mid-run the batch `totals` are ALL ZERO — they're only written when the whole batch ends, so
    // a 147-job crawl would show "0 fetched / 0 kept" for hours. Prefer the LIVE progress counters
    // while a crawl is running; fall back to the batch totals once it's finished.
    const _pg = m.progress || {};
    const t = (_pg.running && _pg.totals) ? _pg.totals : (m.totals || {});
    const bstat = b.running ? `&#9654; running${b.current?' · '+b.current:''}` : (b.total?`idle · last ${b.total} jobs`:'idle');
    document.getElementById('metrics').innerHTML =
      _tile('CPU', cpu+'%', `${m.cpu_count} cores`, cpu) +
      _tile('RAM', r.used_gb+' / '+r.total_gb+' GB', r.percent+'%', r.percent) +
      _tile('Pool', p.tabs+' tabs', `${p.browsers}&#215;${p.tabs_per_browser} · ${p.engine} · ${p.host_concurrency}/host`, null) +
      _tile('Batch', bstat, `${b.done||0}/${b.total||0} done`, null) +
      // MID-RUN `t` is progress.totals, whose keys are fixed by async_engine._STAT_KEYS — which
      // has neither `recovered` nor `skipped` (it has paid_recovered / skipped_unchanged). Both
      // chips were therefore structurally dead while a batch was running and only ever appeared
      // from the post-run summary, silently under-reporting the live view. Read both spellings.
      _tile('Crawled', (t.accepted||0)+' accepted', `${t.fetched||0} fetched · ${t.kept||0} kept`+(((t.recovered??t.paid_recovered)||0)?` · ${t.recovered??t.paid_recovered} recovered`:'')+(((t.skipped??t.skipped_unchanged)||0)?` · ${t.skipped??t.skipped_unchanged} skipped`:''), null) +
      _speedTile(m.progress);
    // Stop button follows batch state: enabled while running, shows "Stopping…" once signaled.
    const stopBtn = document.getElementById('btnStop');
    if (stopBtn) {
      stopBtn.disabled = !b.running || b.stopping;
      stopBtn.innerHTML = b.stopping ? '&#9203; Stopping…' : '&#9209; Stop Crawling';
    }
    // The batch runs in the background; flip the banner to "finished" once it goes running→idle.
    if (b.running) window._sawRunning = true;
    if (window._batchWait && window._sawRunning && !b.running) {
      window._batchWait = false;
      const bel = document.getElementById('batchResult');
      // fetched > accepted is normal: unchanged(304) + already-fetched-unchanged + dupes + off-keyword
      // pages are fetched but not re-sent. Spell it out so the gap doesn't read as lost pages.
      const gap = ((t.not_modified_304||0)?` · ${t.not_modified_304} unchanged(304)`:'')
                + ((t.skipped_unchanged||0)?` · ${t.skipped_unchanged} unchanged`:'')
                + ((t.skipped_duplicate||0)?` · ${t.skipped_duplicate} dupes`:'')
                + ((t.dropped_by_gate||0)?` · ${t.dropped_by_gate} off-keyword`:'');
      if (bel) bel.innerHTML = `<div class="status ok">&#10003; Batch finished &middot; ${(t.accepted||0)} accepted &middot; ${(t.fetched||0)} fetched${gap}. Full pages at <a href="http://127.0.0.1:9090" target="_blank">9090</a>.</div>`;
      const bb = document.getElementById('btnBatch'); if (bb) bb.disabled = false;
      loadBatchReport();          // per-site kept/retryable/terminal breakdown
    }
  }catch(e){}
}

async function stopCrawl() {
  const btn = document.getElementById('btnStop');
  btn.disabled = true; btn.innerHTML = '&#9203; Stopping…';
  window.stopAll = true;   // also halt the Run-All-Jobs JS loop (each job is a separate crawl)
  try {
    await fetch('/v1/crawl/stop', {method:'POST'});
    // The running batch tears down within a couple of drain ticks; metrics polling flips the UI.
  } catch(e) { btn.disabled = false; btn.innerHTML = '&#9209; Stop Crawling'; }
}

// ── C3 CamoFox stealth-engine status (WAF fallback) ──
async function refreshCamo() {
  const dot = document.getElementById('camoDot'), txt = document.getElementById('camoTxt');
  try {
    const d = await (await fetch('/v1/camofox')).json();
    if (!d.enabled) { dot.className='llmdot'; txt.textContent='off';
      dot.title='CamoFox disabled (CAMOFOX_ENABLED=0) — ladder falls through to Wayback/feed'; }
    else if (d.healthy) { dot.className='llmdot up'; txt.textContent='ready'; dot.title='CamoFox up · '+d.url; }
    else { dot.className='llmdot down'; txt.textContent='down'; dot.title='CamoFox enabled but unreachable · '+d.url; }
  } catch(e) { dot.className='llmdot down'; txt.textContent='?'; dot.title='camofox status check failed: '+e.message; }
}

// ── generated jobs (from seed) ──
async function generateJobs() {
  const btn = document.getElementById('btnGen'); btn.disabled = true; btn.textContent = '&#9203; Generating...';
  try {
    const d = await (await fetch('/v1/generate-jobs')).json();
    window.jobs = d.jobs; renderJobs();
    document.getElementById('btnRunAll').disabled = false;
    document.getElementById('jobCount').textContent = `${d.jobs.length} jobs · ${d.by_type_summary}`;
    showTab('jobs');
  } catch(e) {
    document.getElementById('joblist').innerHTML = `<p class="status err">${e.message}</p>`;
  }
  btn.disabled = false; btn.innerHTML = '&#9889; Generate Jobs from Seed';
}

function renderJobs() {
  document.getElementById('joblist').innerHTML = window.jobs.map((j,i) => `
    <div class="card">
      <h3><span class="badge">${j.job_type}</span> ${j.job_id}</h3>
      <div class="meta">entity: ${j.target_entity||'—'} · pages: ${j.max_pages} · depth: ${j.max_depth}</div>
      <div class="urls">${j.seed_urls.map(u=>`<a href="${u}" target="_blank">${u.substring(0,60)}…</a>`).join('<br>')}</div>
      <div class="meta">keywords: ${(j.keywords||[]).join(', ')||'—'}</div>
      <div id="res-${i}"></div>
      <button onclick="runOne(${i})" style="margin-top:8px;">&#9654; Run</button>
    </div>`).join('');
}

async function runOne(i) {
  if (!_c2ok()) return;
  const job = window.jobs[i];
  const freshnessOn = document.getElementById('freshnessToggle').checked;
  const el = document.getElementById('res-'+i);
  el.innerHTML = '<div class="status wait">&#9203; Running...</div>';
  try {
    const body = {..._c2apply(job), forward_to_ingest: true};
    if (!freshnessOn) body.freshness_days = null;
    const d = await (await fetch('/v1/crawl', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    })).json();
    el.innerHTML = resRow(job.job_id, d.summary||{});
  } catch(e) { el.innerHTML = `<div class="status err">${e.message}</div>`; }
}

async function serverStopPending() {
  // Unreachable API => false: a network blip must not silently abandon the operator's run.
  try { const r = await fetch('/v1/batch/status'); return !!(await r.json()).stop_pending; }
  catch(e) { return false; }
}

async function runAllJobs() {
  const btn = document.getElementById('btnRunAll'); btn.disabled = true; btn.innerHTML = '&#9203; Running...';
  window.stopAll = false;                      // reset the per-loop stop latch
  for (let i=0;i<window.jobs.length;i++) {
    if (window.stopAll) break;                 // Stop pressed in THIS tab → don't launch the next job
    // ...and a stop pressed anywhere else. The orchestrator's master switch posts straight to
    // /v1/crawl/stop, so window.stopAll never hears about it and this loop would carry on launching
    // jobs after the operator halted the pipeline. Skipped for i=0: a stale flag from an earlier
    // stop is still on disk until a crawl starts, and job 0 is the explicit start that clears it.
    if (i > 0 && await serverStopPending()) break;
    await runOne(i);
  }
  btn.disabled = false; btn.innerHTML = '&#9654; Run All Jobs';
}

showTab('batch');
health();
refreshCamo();
setInterval(refreshCamo, 30000);  // C3 CamoFox liveness (WAF fallback)
pollMetrics();
setInterval(pollMetrics, 2500);   // live CPU/RAM/pool — watch the box saturate during a batch
</script>
</body></html>"""
