"use strict";

// Before any Date is made: Node reads TZ when it is set at runtime.
process.env.TZ = "Asia/Ho_Chi_Minh";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  formatOffset,
  formatLocalStamp,
  formatLocalRange,
  localizeTimes,
} = require("../../app/static/app.js");

test("offsets keep minutes only when present", () => {
  assert.equal(formatOffset(420), "+07");
  assert.equal(formatOffset(330), "+05:30");
  assert.equal(formatOffset(0), "+00");
  assert.equal(formatOffset(-210), "-03:30");
});

test("a stamp is local with seconds and offset", () => {
  assert.equal(formatLocalStamp("2026-11-13T07:05:09.5Z"), "2026-11-13 14:05:09 +07");
  assert.equal(formatLocalStamp("nonsense"), null);
  assert.equal(formatLocalStamp(""), null);
});

test("a same-day range names the day and offset once", () => {
  assert.equal(
    formatLocalRange("2026-11-13T07:05:09Z", "2026-11-13T07:12:30Z"),
    "2026-11-13 14:05:09 – 14:12:30 +07"
  );
});

test("a range across local midnight names both days", () => {
  // Same UTC day, but 23:59 to 00:03 in the reader's timezone.
  assert.equal(
    formatLocalRange("2026-11-13T16:59:01Z", "2026-11-13T17:03:40Z"),
    "2026-11-13 23:59:01 +07 – 2026-11-14 00:03:40 +07"
  );
});

test("a range without a readable finish is its start", () => {
  assert.equal(formatLocalRange("2026-11-13T07:05:09Z", ""), "2026-11-13 14:05:09 +07");
  assert.equal(formatLocalRange("2026-11-13T07:05:09Z", "later"), "2026-11-13 14:05:09 +07");
  assert.equal(formatLocalRange("bad", "2026-11-13T07:05:09Z"), null);
});

test("localizeTimes rewrites stamps and ranges and leaves unreadable ones", () => {
  const single = { dataset: {}, getAttribute: () => "2026-11-13T07:05:09Z", textContent: "x" };
  const range = {
    dataset: { start: "2026-11-13T07:05:09Z", finish: "2026-11-13T07:12:30Z" },
    textContent: "y",
  };
  const bad = { dataset: {}, getAttribute: () => "nope", textContent: "keep" };
  const root = {
    querySelectorAll(selector) {
      if (selector === "[data-local-time]") return [single, bad];
      if (selector === "[data-local-range]") return [range];
      return [];
    },
  };
  assert.equal(localizeTimes(root), 2);
  assert.equal(single.textContent, "2026-11-13 14:05:09 +07");
  assert.equal(range.textContent, "2026-11-13 14:05:09 – 14:12:30 +07");
  assert.equal(bad.textContent, "keep");
  assert.equal(localizeTimes(null), 0);
});
