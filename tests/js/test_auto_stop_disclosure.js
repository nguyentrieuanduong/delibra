"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  captureAutoStopDisclosure,
  restoreAutoStopDisclosure,
} = require("../../app/static/app.js");

function autoStatus(autoId, details) {
  return {
    id: "auto-status",
    dataset: { autoId },
    querySelector(selector) {
      assert.equal(selector, "details.auto-stop");
      return details;
    },
  };
}

function documentWith(status) {
  return {
    getElementById(id) {
      assert.equal(id, "auto-status");
      return status;
    },
  };
}

test("captures the Stop disclosure only from an Auto status swap", function () {
  assert.equal(captureAutoStopDisclosure({ id: "chat-timeline" }), null);
  assert.deepEqual(
    captureAutoStopDisclosure(autoStatus("a1", { open: true })),
    { autoId: "a1", open: true }
  );
  assert.deepEqual(captureAutoStopDisclosure(autoStatus("a1", null)), {
    autoId: "a1",
    open: false,
  });
});

test("a status refresh reopens the Stop disclosure the reader had open", function () {
  const details = { open: false };
  const restored = restoreAutoStopDisclosure(
    documentWith(autoStatus("a1", details)),
    { autoId: "a1", open: true }
  );

  assert.equal(restored, true);
  assert.equal(details.open, true);
});

test("the Stop disclosure stays closed for another run or a closed snapshot", function () {
  const otherRun = { open: false };
  assert.equal(
    restoreAutoStopDisclosure(documentWith(autoStatus("a2", otherRun)), {
      autoId: "a1",
      open: true,
    }),
    false
  );
  assert.equal(otherRun.open, false);

  const closed = { open: false };
  assert.equal(
    restoreAutoStopDisclosure(documentWith(autoStatus("a1", closed)), {
      autoId: "a1",
      open: false,
    }),
    false
  );
  assert.equal(closed.open, false);

  // A terminal or pending status renders no disclosure to reopen.
  assert.equal(
    restoreAutoStopDisclosure(documentWith(autoStatus("a1", null)), {
      autoId: "a1",
      open: true,
    }),
    false
  );
  assert.equal(restoreAutoStopDisclosure(documentWith(null), null), false);
});
