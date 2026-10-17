import test from "node:test";
import assert from "node:assert/strict";
import * as controls from "../src/config/controls.js";

test("Control Room exposes capture choices, not developer tuning", () => {
  assert.deepEqual(Object.keys(controls), ["CAPTURE_OPTIONS"]);
  assert.deepEqual(
    controls.CAPTURE_OPTIONS.map(({ label }) => label),
    ["HTML pages", "PDFs", "Documents", "Images", "Other links"],
  );
});

test("Every capture type has exactly one control, with file-URL-only descriptions", () => {
  const types = controls.CAPTURE_OPTIONS.flatMap((choice) => choice.types);
  assert.equal(types.length, new Set(types).size);
  assert.deepEqual([...types].sort(), [
    "API",
    "DOCUMENT",
    "FEED",
    "HTML",
    "IMAGE",
    "MEDIA",
    "OTHER",
    "PDF",
  ]);
  assert.match(
    controls.CAPTURE_OPTIONS.find(({ label }) => label === "PDFs").help,
    /URLs only/,
  );
});
