import test from "node:test";
import assert from "node:assert/strict";
import { NUMERIC_CONTROLS, SOURCES } from "../src/config/controls.js";

test("control room does not offer discovery count cutoffs", () => {
  const keys = Object.values(NUMERIC_CONTROLS)
    .flat()
    .map(([key]) => key);
  for (const key of [
    "max_urls",
    "max_browser_pages",
    "max_browser_actions",
    "max_filter_values",
    "maximum_pagination_probe",
  ]) {
    assert.equal(keys.includes(key), false);
  }
  assert.ok(keys.includes("website_delay_seconds"));
  assert.ok(keys.includes("corpus_interval_seconds"));
  assert.match(SOURCES.LOAD_MORE[1], /no new content/);
});

test("speed controls permit moderate pacing, not zero-delay requests", () => {
  const controls = Object.fromEntries(
    Object.values(NUMERIC_CONTROLS)
      .flat()
      .map((control) => [control[0], control]),
  );
  assert.equal(controls.website_delay_seconds[2], 1);
  assert.equal(controls.corpus_interval_seconds[2], 2);
});
