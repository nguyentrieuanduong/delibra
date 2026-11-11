"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  autoExtendPreviewText,
  captureAutoExtendForm,
  restoreAutoExtendForm,
} = require("../../app/static/app.js");

test("the preview adds the typed cycles to the current limit", function () {
  assert.equal(autoExtendPreviewText(5, "2", 95), "New limit: 7 cycles");
  assert.equal(autoExtendPreviewText(1, "1", 99), "New limit: 2 cycles");
  assert.equal(autoExtendPreviewText(5, "95", 95), "New limit: 100 cycles");
});

test("the preview asks for a valid number instead of guessing", function () {
  for (const value of ["", "0", "-1", "1.5", "abc", "96"]) {
    assert.equal(autoExtendPreviewText(5, value, 95), "Enter 1 to 95 cycles");
  }
});

function extendStatus(autoId, details, input) {
  return {
    id: "auto-status",
    dataset: { autoId },
    querySelector(selector) {
      if (selector === "details.auto-extend") {
        return details;
      }
      if (selector === "[data-auto-extend-input]") {
        return input;
      }
      if (selector === "[data-auto-extend-preview]") {
        return details ? details.preview : null;
      }
      throw new Error(`unexpected selector ${selector}`);
    },
  };
}

function outside() {
  return { closest: () => null };
}

test("captures the open form and typed value from a status refresh", function () {
  const details = { open: true };
  const input = { value: "4" };
  assert.equal(captureAutoExtendForm({ id: "chat-timeline" }, outside()), null);
  assert.deepEqual(
    captureAutoExtendForm(extendStatus("a1", details, input), outside()),
    { autoId: "a1", open: true, value: "4" }
  );
});

test("the form's own submit does not reopen it", function () {
  const details = { open: true };
  const input = { value: "4" };
  const source = { closest: (selector) => (selector === "[data-auto-extend-form]" ? {} : null) };
  assert.deepEqual(captureAutoExtendForm(extendStatus("a1", details, input), source), {
    autoId: "a1",
    open: false,
    value: "",
  });
});

test("a refresh reopens the form, keeps the value and recomputes the preview", function () {
  const preview = { textContent: "" };
  const details = { open: false, preview };
  const input = {
    value: "1",
    max: "93",
    dataset: { autoExtendBase: "7" },
  };
  const documentRoot = {
    getElementById(id) {
      assert.equal(id, "auto-status");
      return extendStatus("a1", details, input);
    },
  };

  assert.equal(
    restoreAutoExtendForm(documentRoot, { autoId: "a1", open: true, value: "4" }),
    true
  );
  assert.equal(details.open, true);
  assert.equal(input.value, "4");
  assert.equal(preview.textContent, "New limit: 11 cycles");

  details.open = false;
  assert.equal(
    restoreAutoExtendForm(documentRoot, { autoId: "a2", open: true, value: "4" }),
    false
  );
  assert.equal(details.open, false);
});
