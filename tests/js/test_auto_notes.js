"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  resetAutoNotesForm,
  skipStaleNotesSwap,
  syncAutoNotesAvailability,
} = require("../../app/static/app.js");

function documentWith(open) {
  const controls = [{ disabled: true }, { disabled: true }];
  return {
    controls,
    querySelector(selector) {
      assert.equal(selector, "#auto-status");
      return open === null ? null : { dataset: { autoNotesOpen: open ? "true" : "false" } };
    },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-auto-notes-input]");
      return controls;
    },
  };
}

test("enables the notes inputs only while the shown run accepts notes", function () {
  const open = documentWith(true);
  assert.equal(syncAutoNotesAvailability(open), true);
  assert.deepEqual(open.controls.map((item) => item.disabled), [false, false]);
  const closed = documentWith(false);
  closed.controls.forEach((item) => { item.disabled = false; });
  assert.equal(syncAutoNotesAvailability(closed), false);
  assert.deepEqual(closed.controls.map((item) => item.disabled), [true, true]);
  assert.equal(syncAutoNotesAvailability(documentWith(null)), false);
});

test("clears the note only after a successful post", function () {
  let resets = 0;
  const form = { reset() { resets += 1; } };
  const source = { closest: (selector) => (selector === "[data-auto-notes-form]" ? form : null) };
  assert.equal(resetAutoNotesForm(source, { successful: false }), false);
  assert.equal(resetAutoNotesForm(source, { successful: true }), true);
  assert.equal(resetAutoNotesForm({ closest: () => null }, { successful: true }), false);
  assert.equal(resetAutoNotesForm(null, { successful: true }), false);
  assert.equal(resets, 1);
});

function notesTarget(revision) {
  return { id: "auto-notes-list", dataset: { notesRevision: revision } };
}

test("skips an out-of-band notes list older than the one on screen", function () {
  const stale = {
    target: notesTarget("5"),
    fragment: { firstElementChild: { dataset: { notesRevision: "3" } } },
    shouldSwap: true,
  };
  assert.equal(skipStaleNotesSwap(stale), true);
  assert.equal(stale.shouldSwap, false);
  for (const revision of ["5", "8"]) {
    const fresh = {
      target: notesTarget("5"),
      fragment: { dataset: { notesRevision: revision } },
      shouldSwap: true,
    };
    assert.equal(skipStaleNotesSwap(fresh), false);
    assert.equal(fresh.shouldSwap, true);
  }
});

test("leaves other targets and unversioned lists alone", function () {
  const other = {
    target: { id: "auto-status", dataset: {} },
    fragment: { dataset: { notesRevision: "1" } },
    shouldSwap: true,
  };
  assert.equal(skipStaleNotesSwap(other), false);
  const unversioned = { target: notesTarget("4"), fragment: { dataset: {} }, shouldSwap: true };
  assert.equal(skipStaleNotesSwap(unversioned), false);
  assert.equal(skipStaleNotesSwap(null), false);
  assert.equal(other.shouldSwap && unversioned.shouldSwap, true);
});
