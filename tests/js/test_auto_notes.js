"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  resetAutoNotesForm,
  skipStaleNotesSwap,
  syncAutoNotesAvailability,
} = require("../../app/static/app.js");

function page({ autoActive = false, autoOpen = false, selected = "s1", running = false, compacting = false } = {}) {
  const controls = [{ disabled: true }, { disabled: true }];
  const target = { value: "auto" };
  const textarea = { attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } };
  const modes = [
    { dataset: { notesMode: "auto" }, hidden: false },
    { dataset: { notesMode: "chat" }, hidden: true },
  ];
  return {
    controls, target, textarea, modes,
    querySelector(selector) {
      if (selector === "#auto-status") {
        return { dataset: { autoActive: String(autoActive), autoNotesOpen: String(autoOpen) } };
      }
      if (selector === "#chat-composer") return { dataset: { sessionId: selected } };
      if (selector === `#agent-status-${selected}`) {
        return {
          dataset: {
            agentStatus: running || compacting ? "running" : "idle",
            agentReplying: running ? "true" : "false",
          },
        };
      }
      if (selector === "[data-notes-target]") return target;
      if (selector === "#auto-note-text") return textarea;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-auto-notes-input]") return controls;
      if (selector === "[data-notes-mode]") return modes;
      return [];
    },
  };
}

test("a replying agent takes notes first, then an Auto run that accepts them", function () {
  const chat = page({ autoOpen: true, running: true });
  assert.equal(syncAutoNotesAvailability(chat), true);
  assert.equal(chat.target.value, "chat");
  assert.deepEqual(chat.modes.map((item) => item.hidden), [true, false]);
  assert.equal(chat.textarea.attrs["aria-describedby"], "chat-note-hint");
  assert.deepEqual(chat.controls.map((item) => item.disabled), [false, false]);

  const auto = page({ autoOpen: true });
  assert.equal(syncAutoNotesAvailability(auto), true);
  assert.equal(auto.target.value, "auto");
  assert.deepEqual(auto.modes.map((item) => item.hidden), [false, true]);
  assert.equal(auto.textarea.attrs["aria-describedby"], "auto-note-hint");

  const duringAuto = page({ autoActive: true, autoOpen: true, running: true });
  syncAutoNotesAvailability(duringAuto);
  assert.equal(duringAuto.target.value, "auto");

  // Closed: nothing can be sent, but the selected agent's notes stay listed.
  const closed = page({});
  assert.equal(syncAutoNotesAvailability(closed), false);
  assert.deepEqual(closed.controls.map((item) => item.disabled), [true, true]);
  assert.deepEqual(closed.modes.map((item) => item.hidden), [true, false]);

  // A compaction runs, but it is not a reply.
  const compacting = page({ compacting: true });
  assert.equal(syncAutoNotesAvailability(compacting), false);
  assert.equal(syncAutoNotesAvailability({ querySelector: () => null, querySelectorAll: () => [] }), false);
});

test("a chat list for an agent that is not selected never lands", function () {
  const ownerDocument = { querySelector: (s) => (s === "#chat-composer" ? { dataset: { sessionId: "s2" } } : null) };
  const shown = { id: "chat-notes-list", ownerDocument, dataset: { sessionId: "s1", notesRevision: "9" } };
  const other = { shouldSwap: true, target: shown, fragment: { dataset: { sessionId: "s1", notesRevision: "10" } } };
  assert.equal(skipStaleNotesSwap(other), true);
  assert.equal(other.shouldSwap, false);
  const switched = { shouldSwap: true, target: shown, fragment: { dataset: { sessionId: "s2", notesRevision: "1" } } };
  assert.equal(skipStaleNotesSwap(switched), false);
  assert.equal(switched.shouldSwap, true);
  const own = { ...shown, dataset: { sessionId: "s2", notesRevision: "5" } };
  const ownOlder = { shouldSwap: true, target: own, fragment: { dataset: { sessionId: "s2", notesRevision: "4" } } };
  assert.equal(skipStaleNotesSwap(ownOlder), true);
  const ownNewer = { shouldSwap: true, target: own, fragment: { dataset: { sessionId: "s2", notesRevision: "6" } } };
  assert.equal(skipStaleNotesSwap(ownNewer), false);
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
