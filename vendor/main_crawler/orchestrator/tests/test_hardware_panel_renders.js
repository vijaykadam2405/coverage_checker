// Does the hardware panel actually RENDER, against a real /api/host payload?
//
// WHY THIS EXISTS. `node --check` proves the dashboard PARSES; it says nothing about an undefined
// name at runtime. One `m.lanes` where the enclosing function's parameter is called `h` threw a
// ReferenceError mid-way through a single long string concatenation, so `#hw` and `#hwWatch` were
// never written and the operator saw an EMPTY hardware box on both the Watchfloor and the Control
// panel — with nothing on screen to say why, and the Control panel stuck on "loading…" behind it.
// Parsing was fine the whole time.
//
// So this extracts the real _renderHardware source out of index.html and runs it against a payload
// shaped like the live API, with the smallest possible stubs. It fails on any undefined reference
// the function touches.
//
//     node tests/test_hardware_panel_renders.js

import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = join(__dirname, '..', 'src', 'mallory_orchestrator', 'static', 'index.html');
const src = readFileSync(HTML, 'utf8');

// Pull out `function _renderHardware(h){ … }` by brace balance.
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} is missing from index.html`);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`could not find the end of ${name}`);
}

// A payload with the same SHAPE the fleet returns — the numbers are real ones taken from the live
// server so the arithmetic paths (percentages, ratios) are exercised rather than short-circuited.
const HOST = {
  reachable: true, cpu_percent: 54.5, cpu_count: 40,
  host_mem: { percent: 12.0, used_gb: 127.3, usable_gb: 1082.2, ballooned_gb: 0 },
  ram: { percent: 12.0, used_gb: 127.3, total_gb: 1082.2 },
  disk: { percent: 7, used_gb: 1479.8, total_gb: 21641.4 },
  container_ram: { percent: 47, used_gb: 121.9, limit_gb: 258, scope: '12 instance(s) combined' },
  pool: { tabs: 384, browsers: 48, host_concurrency: '3-48 adaptive' },
  adaptive: { enabled: true, target: 70, active: 115, render_limit: 301, cpu: 64.6 },
  lanes: { render_lane_used: 664, render_lane_cap: 1408, render_q: 22005, frontier: 100000 },
  progress: { pages_per_sec: 9.411 },
  instances: { up: 12, down: 0, total: 12, crawling: 10, idle: 2, down_detail: [] },
};

const sinks = {};
const ctx = {
  // The two sinks the panel writes to, captured instead of touching a DOM.
  _hwSink: (html, note) => { sinks.html = html; sinks.note = note; },
  tile: (label, value, sub, cls) =>
    `<TILE label="${label}" value="${value == null ? 'NULL' : value}">${sub || ''}</TILE>`,
  meter: (v, extra) => String(extra || ''),
  fmt: n => (n == null ? '—' : String(n)),
  esc: s => String(s == null ? '' : s),
  num: n => (n == null || n === '' || Number.isNaN(Number(n)) ? null : Number(n)),
  secs: s => `${s}s`,
};

const fn = new Function(
  ...Object.keys(ctx),
  `${extract('_renderHardware')}; return _renderHardware;`
)(...Object.values(ctx));

function run(name, payload, check) {
  sinks.html = sinks.note = undefined;
  fn(payload);
  check();
  console.log(`  ok  ${name}`);
}

run('a full payload renders every tile', HOST, () => {
  assert.ok(sinks.html, 'the panel wrote nothing at all — this is the blank-panel bug');
  const tiles = (sinks.html.match(/<TILE /g) || []).length;
  assert.ok(tiles >= 6, `expected the full tile set, got ${tiles}`);
  assert.ok(sinks.html.includes('Render slots'), 'the Render slots tile is missing');
  assert.ok(sinks.html.includes('664/1408'),
    'Render slots must show the LANE occupancy, not the CPU governor');
  assert.ok(sinks.html.includes('22005'), 'the queued-render count is not shown');
  assert.ok(sinks.html.includes('Crawler fleet'), 'the fleet tile is missing');
  assert.ok(sinks.html.includes('10/12'), 'the fleet tile must headline CRAWLING/total');
});

run('an unreachable crawler still renders a panel', { reachable: false, error: 'no answer' }, () => {
  assert.ok(sinks.html, 'an unreachable crawler must still paint tiles, not a blank box');
  assert.ok(String(sinks.note).includes('not answering'));
});

// The shapes a partly-reporting fleet actually returns. Each of these once had to be guarded, and a
// missing guard blanks the whole panel rather than one tile.
run('missing lanes does not blank the panel', { ...HOST, lanes: undefined }, () => {
  assert.ok(sinks.html && sinks.html.includes('<TILE '), 'no lanes must not blank the panel');
  assert.ok(sinks.html.includes('lane not reported'), 'it should say the lane is unknown');
});

run('missing adaptive does not blank the panel', { ...HOST, adaptive: undefined }, () => {
  assert.ok(sinks.html && sinks.html.includes('<TILE '));
});

run('missing instances does not blank the panel', { ...HOST, instances: undefined }, () => {
  assert.ok(sinks.html && sinks.html.includes('<TILE '));
});

run('a zero render_lane_cap does not divide by zero', {
  ...HOST, lanes: { render_lane_used: 0, render_lane_cap: 0, render_q: 0 },
}, () => {
  assert.ok(sinks.html.includes('<TILE '));
  assert.ok(!/NaN|Infinity/.test(sinks.html), 'a 0/0 lane produced NaN or Infinity on screen');
});

run('an older API without `crawling` says so rather than implying all is well', {
  ...HOST, instances: { up: 12, down: 0, total: 12, down_detail: [] },
}, () => {
  assert.ok(sinks.html.includes('work not measured'),
    'a missing crawling count must be stated, not smoothed into a green 12/12');
});

console.log('ALL HARDWARE-PANEL TESTS PASSED');
