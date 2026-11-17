"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { settingsFormsDirty, formSnapshot } = require("../../app/static/app.js");

function field(name, value, type) {
  return { name: name, value: value, type: type || "text", checked: false };
}

function form(fields) {
  return { elements: fields };
}

test("a form is dirty only after a field changes", function () {
  const box = field("allowed_hosts", "a.example");
  const tick = field("enabled", "true", "checkbox");
  const settings = form([box, tick]);
  const snapshots = new Map([[settings, formSnapshot(settings)]]);

  assert.equal(settingsFormsDirty([settings], snapshots, null), false);
  box.value = "b.example";
  assert.equal(settingsFormsDirty([settings], snapshots, null), true);
  box.value = "a.example";
  tick.checked = true;
  assert.equal(settingsFormsDirty([settings], snapshots, null), true);
});

test("the form being submitted does not block leaving", function () {
  const box = field("allowed_hosts", "a.example");
  const settings = form([box]);
  const snapshots = new Map([[settings, formSnapshot(settings)]]);
  box.value = "changed";

  assert.equal(settingsFormsDirty([settings], snapshots, settings), false);
});
