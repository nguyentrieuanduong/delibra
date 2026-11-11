"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  NOTES_DISCLOSURE_KEY,
  notesSummaryText,
  rememberNotesDisclosure,
  restoreNotesDisclosure,
  syncNotesDisclosure,
} = require("../../app/static/app.js");

test("the summary counts the notes and the pending ones", function () {
  assert.equal(notesSummaryText(4, 1), " · 4 (1 pending)");
  assert.equal(notesSummaryText(3, 0), " · 3");
  assert.equal(notesSummaryText(0, 0), "");
});

function list(hidden, statuses) {
  return {
    hidden,
    querySelectorAll(selector) {
      if (selector === "li.auto-note") {
        return statuses.map(() => ({}));
      }
      assert.equal(selector, "li.auto-note.pending");
      return statuses.filter((status) => status === "pending").map(() => ({}));
    },
  };
}

function page(lists) {
  const count = { textContent: "" };
  const details = {
    hidden: false,
    querySelectorAll(selector) {
      assert.equal(selector, ".auto-notes-list");
      return lists;
    },
    querySelector(selector) {
      assert.equal(selector, "[data-notes-summary-count]");
      return count;
    },
  };
  return {
    count,
    details,
    querySelector(selector) {
      assert.equal(selector, "[data-notes-disclosure]");
      return details;
    },
  };
}

test("the count follows the list on screen, not the hidden one", function () {
  const root = page([
    list(true, ["pending", "pending", "pending"]),
    list(false, ["pending", "delivered"]),
  ]);

  assert.deepEqual(syncNotesDisclosure(root), { total: 2, pending: 1 });
  assert.equal(root.count.textContent, " · 2 (1 pending)");
  assert.equal(root.details.hidden, false);
});

test("an empty list hides the section", function () {
  const root = page([list(false, []), list(true, ["pending"])]);

  assert.deepEqual(syncNotesDisclosure(root), { total: 0, pending: 0 });
  assert.equal(root.details.hidden, true);
});

function memoryStorage(initial) {
  const values = new Map(Object.entries(initial || {}));
  return {
    values,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
  };
}

test("the chosen open state is remembered and restored", function () {
  const storage = memoryStorage();
  rememberNotesDisclosure({ open: false }, storage);
  assert.equal(storage.values.get(NOTES_DISCLOSURE_KEY), "false");

  const details = { open: true };
  assert.equal(restoreNotesDisclosure(details, storage), true);
  assert.equal(details.open, false);

  rememberNotesDisclosure({ open: true }, storage);
  restoreNotesDisclosure(details, storage);
  assert.equal(details.open, true);
});

test("no stored choice, or storage that throws, keeps the rendered state", function () {
  const details = { open: true };
  assert.equal(restoreNotesDisclosure(details, memoryStorage()), false);
  assert.equal(details.open, true);
  const broken = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  assert.equal(restoreNotesDisclosure(details, broken), false);
  assert.equal(rememberNotesDisclosure(details, broken), false);
  assert.equal(details.open, true);
});
