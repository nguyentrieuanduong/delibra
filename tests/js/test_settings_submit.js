"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { submittedForm } = require("../../app/static/app.js");

test("a cancelled submit (Replace confirm answered Cancel) does not count as leaving by saving", () => {
  const form = { id: "prompt-compact" };
  assert.equal(submittedForm({ defaultPrevented: true, target: form }, null), null);
});

test("a submit that goes ahead is the form being saved", () => {
  const form = { id: "prompt-compact" };
  assert.equal(submittedForm({ defaultPrevented: false, target: form }, null), form);
});
