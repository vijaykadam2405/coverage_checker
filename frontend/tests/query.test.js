import test from "node:test";
import assert from "node:assert/strict";
import { inventoryQuery } from "../src/utils/query.js";
import { site, period } from "../src/utils/format.js";
test("inventory requests one server page of fifty", () => {
  const p = new URLSearchParams(inventoryQuery({ type: "HTML" }, 3));
  assert.equal(p.get("page"), "3");
  assert.equal(p.get("page_size"), "50");
});
test("export retains every filter without a page restriction", () => {
  const p = new URLSearchParams(
    inventoryQuery({ host: "cdn.test", corpus_status: "missing", depth: 0 }),
  );
  assert.equal(p.get("host"), "cdn.test");
  assert.equal(p.get("corpus_status"), "missing");
  assert.equal(p.get("depth"), "0");
  assert.equal(p.has("page_size"), false);
});
test("literal URL search is encoded safely", () => {
  const search = "https://test/page?a=1&b=2 %";
  assert.equal(
    new URLSearchParams(inventoryQuery({ search })).get("search"),
    search,
  );
});
test("empty filters do not become the words null or undefined", () => {
  assert.equal(inventoryQuery({ host: "", type: undefined, depth: null }), "");
});
test("website labels are hosts, not file paths", () =>
  assert.equal(site("https://example.com/report.pdf"), "example.com"));

test("activity periods use whole elapsed days without inventing missing dates", () => {
  const now = Date.parse("2026-10-11T12:00:00Z");
  assert.equal(period("2026-10-09T12:00:00Z", now), "2d ago");
  assert.equal(period("2026-10-11T10:00:00Z", now), "0d ago");
  assert.equal(period("2026-10-12T12:00:00Z", now), "0d ago");
  assert.equal(period("invalid", now), "—");
  assert.equal(period(null, now), "—");
});
