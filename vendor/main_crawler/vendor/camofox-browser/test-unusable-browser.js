// The live-but-unusable browser: does a dead-context error from newContext trigger a relaunch?
//
// Standalone (no test framework — this vendored service has none). Run:
//     node test-unusable-browser.js
//
// It does NOT boot the service. It re-implements the exact control flow added to getSession around
// `b.newContext(...)` against fake browsers, because what broke was the CONTROL FLOW: the real
// server correctly detected the error (isDeadContextError existed and matched) and correctly
// destroyed the session, and still served errors for 20 minutes, because nothing on that path ever
// concluded that the BROWSER was the thing to replace.

// ESM: this package sets "type": "module", so require() is not available here.
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Verbatim from server.js — if it drifts, the fix stops firing and this test still passes, so it is
// asserted against the real file below.
function isDeadContextError(err) {
  const msg = (err && err.message) || '';
  return msg.includes('Target page, context or browser has been closed') ||
         msg.includes('browser has been closed') ||
         msg.includes('Context closed') ||
         msg.includes('Browser closed');
}

const DEAD = () => new Error(
  'browserContext.newPage: Target page, context or browser has been closed');

// The patched flow, in the shape it has in getSession.
async function createContext({ browser, relaunch, log = () => {} }) {
  let context;
  try {
    context = await browser.newContext();
  } catch (err) {
    if (!isDeadContextError(err)) throw err;
    log('relaunching');
    const fresh = await relaunch();
    context = await fresh.newContext();
  }
  return context;
}

async function run() {
  // 1. The measured failure: a browser that reports connected but cannot make a context.
  {
    let relaunches = 0;
    const dead = { isConnected: () => true, newContext: async () => { throw DEAD(); } };
    const live = { isConnected: () => true, newContext: async () => ({ id: 'fresh' }) };
    const ctx = await createContext({
      browser: dead,
      relaunch: async () => { relaunches++; return live; },
    });
    assert.strictEqual(ctx.id, 'fresh', 'must end up with a context from the RELAUNCHED browser');
    assert.strictEqual(relaunches, 1, 'must relaunch exactly once');
    console.log('  ok  a live-but-unusable browser is replaced, and a context is still returned');
  }

  // 2. isConnected() is useless here — the whole point. The old guard
  //    `if (browser && !browser.isConnected())` would never have fired on case 1.
  {
    const dead = { isConnected: () => true, newContext: async () => { throw DEAD(); } };
    assert.strictEqual(dead.isConnected(), true,
      'the browser claims to be connected while every newContext throws — this is why ' +
      'ensureBrowser() never relaunched and /health reported ok:true over a dead browser');
    console.log('  ok  the failure is invisible to isConnected(), so the old guard could not catch it');
  }

  // 3. An unrelated error must NOT trigger a browser relaunch — a relaunch drops every session in
  //    the fleet, so over-triggering is expensive.
  {
    let relaunches = 0;
    const browser = {
      isConnected: () => true,
      newContext: async () => { throw new Error('Maximum concurrent sessions reached'); },
    };
    await assert.rejects(
      () => createContext({ browser, relaunch: async () => { relaunches++; return browser; } }),
      /Maximum concurrent sessions/,
      'a non-dead-context error must propagate unchanged');
    assert.strictEqual(relaunches, 0, 'must not relaunch on an admission error');
    console.log('  ok  unrelated errors propagate and do not cost the fleet its sessions');
  }

  // 4. A relaunch that also fails must propagate, not loop. The caller (and the watchdog) has to
  //    learn that the service is genuinely broken.
  {
    const dead = { isConnected: () => true, newContext: async () => { throw DEAD(); } };
    await assert.rejects(
      () => createContext({
        browser: dead,
        relaunch: async () => { throw new Error('Browser launch timeout (60s)'); },
      }),
      /launch timeout/,
      'a failed relaunch must surface');
    console.log('  ok  a failed relaunch surfaces instead of retrying for ever');
  }

  // 5. Only ONE relaunch per attempt: if the fresh browser is also unusable, give up rather than
  //    recursing. A relaunch storm under a broken image is worse than a clear failure.
  {
    let relaunches = 0;
    const dead = { isConnected: () => true, newContext: async () => { throw DEAD(); } };
    await assert.rejects(
      () => createContext({
        browser: dead,
        relaunch: async () => { relaunches++; return dead; },   // still broken
      }),
      isDeadContextError,
      'a second dead-context error must propagate');
    assert.strictEqual(relaunches, 1, 'exactly one relaunch attempt, no recursion');
    console.log('  ok  exactly one relaunch attempt — no storm');
  }

  // 6. The detector and the call site must both still exist in the real server, or every test above
  //    is checking a copy of logic that has drifted out of the product.
  {
    const src = readFileSync(join(__dirname, 'server.js'), 'utf8');
    assert.ok(src.includes('function isDeadContextError'), 'isDeadContextError vanished from server.js');
    assert.ok(/Target page, context or browser has been closed/.test(src),
      'the error string the fix keys on is gone — the browser may have changed its wording');
    assert.ok(src.includes("closeBrowserFully('browser_unusable')"),
      'the browser_unusable relaunch is not wired into server.js');
    assert.ok(/catch \(err\) \{\s*\n\s*if \(!isDeadContextError\(err\)\) throw err;/.test(src),
      'the guard that keeps unrelated errors from relaunching the browser is missing');
    console.log('  ok  server.js still contains the detector and the relaunch call site');
  }

  console.log('ALL 6 UNUSABLE-BROWSER TESTS PASSED');
}

run().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
