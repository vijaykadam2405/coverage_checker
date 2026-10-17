import test from "node:test";
import assert from "node:assert/strict";
import { analysisRefreshInterval } from "../src/utils/polling.js";

test("Analysis polls saved evidence only for the selected active scan", () => {
  assert.equal(
    analysisRefreshInterval({ run: { id: 3 }, active: { run_id: 3 } }),
    15000,
  );
  assert.equal(
    analysisRefreshInterval({ run: { id: 4 }, active: { run_id: 3 } }),
    0,
  );
});

test("Analysis stops polling on completion and tolerates missing state", () => {
  for (const snapshot of [
    undefined,
    {},
    { run: { id: 3 }, active: null },
    { run: { id: 3 }, active: { run_id: 3 } },
  ]) {
    const expected = snapshot?.active?.run_id === 3 ? 15000 : 0;
    assert.equal(analysisRefreshInterval(snapshot), expected);
  }
});
