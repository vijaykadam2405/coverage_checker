import test from "node:test";
import assert from "node:assert/strict";
import {
  distinctPageTitle,
  textCoverageValue,
  availableRunId,
} from "../src/utils/inventory.js";

test("Removed inventories fall back to a saved inventory, never a stale ID", () => {
  const runs = [{ id: 4 }, { id: 3 }, { id: 1 }];
  assert.equal(availableRunId(runs, 5, null), 4);
  assert.equal(availableRunId(runs, 3, 4), 3);
  assert.equal(availableRunId(runs, 5, 3), 3);
  assert.equal(availableRunId([], 5, null), null);
});

test("URL labels show no duplicate URL or blank title", () => {
  const url = "https://example.test/news/report.pdf";
  assert.equal(distinctPageTitle(null, url), "");
  assert.equal(distinctPageTitle("  ", url), "");
  assert.equal(distinctPageTitle(url, url), "");
  assert.equal(distinctPageTitle(url + "/", url), "");
  assert.equal(
    distinctPageTitle("http://example.test/news/report.pdf", url),
    "",
  );
  assert.equal(distinctPageTitle("Annual report", url), "Annual report");
});

test("Text bars show measured percentages only, including zero", () => {
  for (const value of [0, 46.6, 88.2, 100]) {
    assert.equal(textCoverageValue({ text_coverage_percent: value }), value);
  }
  for (const value of [null, undefined, NaN, Infinity, -1, 101, "88.2"]) {
    assert.equal(textCoverageValue({ text_coverage_percent: value }), null);
  }
  assert.equal(textCoverageValue({ corpus_state: "found" }), null);
});
