"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { openNoteEdit, finishNoteEdit } = require("../../app/static/app.js");

function dialogPage() {
  const textarea = { value: "", focused: false, focus() { this.focused = true; } };
  const idLabel = { textContent: "" };
  const form = { dataset: {}, querySelector: (s) => (s === "textarea" ? textarea : null) };
  const dialog = {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
    querySelector(s) {
      if (s === "[data-note-edit-form]") return form;
      if (s === "[data-note-edit-id]") return idLabel;
      return null;
    },
  };
  form.closest = (s) => (s === "dialog" ? dialog : null);
  const doc = { getElementById: (id) => (id === "note-edit-dialog" ? dialog : null) };
  return { doc, dialog, form, textarea, idLabel };
}

test("Edit fills the dialog with the note's current text", () => {
  const { doc, dialog, form, textarea, idLabel } = dialogPage();
  const item = {
    querySelector: (s) => (s === ".auto-note-text" ? { textContent: "Keep it\nshort" } : null),
  };
  const button = {
    dataset: { noteEditUrl: "/p/auto-notes/N1/edit", noteId: "N1" },
    closest: (s) => (s === "li" ? item : null),
  };
  assert.equal(openNoteEdit(button, doc), true);
  assert.equal(dialog.open, true);
  assert.equal(form.dataset.noteEditUrl, "/p/auto-notes/N1/edit");
  assert.equal(idLabel.textContent, "N1");
  assert.equal(textarea.value, "Keep it\nshort");
  assert.equal(textarea.focused, true);
});

test("Edit without a dialog or note text does nothing", () => {
  const button = { dataset: {}, closest: () => null };
  assert.equal(openNoteEdit(button, { getElementById: () => null }), false);
  assert.equal(openNoteEdit(button, dialogPage().doc), false);
});

test("a saved edit closes the dialog; a refused one alerts and keeps the text", () => {
  const { dialog, form, textarea } = dialogPage();
  dialog.open = true;
  textarea.value = "typed";
  const alerts = [];
  const refused = {
    successful: false,
    xhr: {
      status: 409,
      responseText: '{"detail":"Notes can be edited only while no agent is running"}',
    },
  };
  assert.equal(finishNoteEdit(form, refused, (m) => alerts.push(m)), "alerted");
  assert.deepEqual(alerts, ["Notes can be edited only while no agent is running"]);
  assert.equal(dialog.open, true);
  assert.equal(textarea.value, "typed");
  assert.equal(finishNoteEdit(form, { successful: true }, (m) => alerts.push(m)), "closed");
  assert.equal(dialog.open, false);
  assert.equal(finishNoteEdit(null, { successful: true }, () => {}), null);
});
