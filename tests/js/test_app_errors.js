"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  chatErrorMessage,
  closeFocusDialog,
  handleFocusDialogCancel,
  isDialogBackdropClick,
  openFocusDialog,
  renderChatError,
  shouldClearChatError,
} = require("../../app/static/app.js");

test("extracts string details without interpreting markup", function () {
  const hostile = "<img src=x onerror=alert(1)>";
  assert.equal(chatErrorMessage(409, JSON.stringify({ detail: hostile })), hostile);
});

test("normalizes validation arrays and malformed responses", function () {
  const validation = JSON.stringify({
    detail: [{ loc: ["body", "prompt"], msg: "Field required" }],
  });
  assert.equal(chatErrorMessage(422, validation), "Field required");
  assert.equal(chatErrorMessage(500, "not-json"), "Request failed (500)");
});

test("renders hostile details through textContent only", function () {
  let assigned = "";
  const region = {
    set textContent(value) {
      assigned = value;
    },
    set innerHTML(_value) {
      throw new Error("innerHTML must never be used");
    },
  };
  const hostile = "<svg onload=alert(1)>";
  assert.equal(
    renderChatError(region, 422, JSON.stringify({ detail: hostile })),
    hostile
  );
  assert.equal(assigned, hostile);
});

test("focus dialog opens, focuses its close control, and restores its trigger", function () {
  let closeFocused = 0;
  let triggerFocused = 0;
  const closeControl = {
    focus() {
      closeFocused += 1;
    },
  };
  const trigger = {
    focus() {
      triggerFocused += 1;
    },
  };
  const dialog = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    querySelector(selector) {
      assert.equal(selector, "[data-focus-dialog-close]");
      return closeControl;
    },
  };

  openFocusDialog(dialog, trigger);
  assert.equal(dialog.open, true);
  assert.equal(closeFocused, 1);

  closeFocusDialog(dialog);
  assert.equal(dialog.open, false);
  assert.equal(triggerFocused, 1);
});

test("focus dialog recognizes only clicks on its backdrop", function () {
  const dialog = {};
  assert.equal(isDialogBackdropClick(dialog, { target: dialog }), true);
  assert.equal(isDialogBackdropClick(dialog, { target: {} }), false);
});

test("focus dialog handles Escape as a cancel and restores focus", function () {
  let prevented = 0;
  let restored = 0;
  const trigger = {
    focus() {
      restored += 1;
    },
  };
  const dialog = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    querySelector() {
      return { focus() {} };
    },
  };
  openFocusDialog(dialog, trigger);

  handleFocusDialogCancel({
    currentTarget: dialog,
    preventDefault() {
      prevented += 1;
    },
  });

  assert.equal(prevented, 1);
  assert.equal(dialog.open, false);
  assert.equal(restored, 1);
});

test("successful file fragments preserve the global chat error", function () {
  const project = "a".repeat(32);
  assert.equal(
    shouldClearChatError(`/projects/${project}/files?path=notes`),
    false
  );
  assert.equal(
    shouldClearChatError(`/projects/${project}/files/view?path=notes.md`),
    false
  );
  assert.equal(shouldClearChatError(`/projects/${project}/chat/select`), true);
});
