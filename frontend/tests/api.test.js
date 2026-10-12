import test from "node:test";
import assert from "node:assert/strict";
import { api } from "../src/services/api.js";

test("polling reads the local API without starting discovery or Corpus verification", async (context) => {
  const calls = [];
  context.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ runs: [] }) };
  });
  const controller = new AbortController();
  await api("/state", undefined, "GET", { signal: controller.signal });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/state");
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.signal, controller.signal);
});

test("explicit actions carry the local client header and JSON payload", async (context) => {
  context.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "/api/settings");
    assert.equal(options.method, "PUT");
    assert.equal(options.headers["X-CrawlScope-Client"], "1");
    assert.deepEqual(JSON.parse(options.body), { capture_types: ["PDF"] });
    return { ok: true, json: async () => ({ capture_types: ["PDF"] }) };
  });
  assert.deepEqual(await api("/settings", { capture_types: ["PDF"] }, "PUT"), {
    capture_types: ["PDF"],
  });
});

test("service failures remain visible, not empty successful data", async (context) => {
  context.mock.method(globalThis, "fetch", async () => ({
    ok: false,
    json: async () => ({ detail: "Corpus is paused" }),
  }));
  await assert.rejects(api("/runs/1/verify", {}), /Corpus is paused/);
});
