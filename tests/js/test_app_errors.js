"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  chatErrorMessage,
  closeFocusDialog,
  conversationTimelineForSwap,
  handleFocusDialogCancel,
  isDialogBackdropClick,
  openFocusDialog,
  removeConversationEmptyState,
  resetFileReader,
  renderChatError,
  shouldClearChatError,
  syncConversationDisclosure,
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

test("first inserted conversation message removes the stale empty marker", () => {
  let removed = 0;
  const empty = {
    remove() {
      removed += 1;
    },
  };
  const timeline = {
    querySelector(selector) {
      return selector === "[data-conversation-empty]" ? empty : {};
    },
  };
  assert.equal(removeConversationEmptyState(timeline), true);
  assert.equal(removed, 1);
});

test("empty marker stays until a conversation message exists", () => {
  let removed = 0;
  const empty = {
    remove() {
      removed += 1;
    },
  };
  const timeline = {
    querySelector(selector) {
      return selector === "[data-conversation-empty]" ? empty : null;
    },
  };
  assert.equal(removeConversationEmptyState(timeline), false);
  assert.equal(removed, 0);
});

test("beforeend swaps use their timeline detail target", () => {
  const timeline = {
    classList: {
      contains(value) {
        assert.equal(value, "chat-timeline");
        return true;
      },
    },
  };
  const swapped = {
    closest() {
      throw new Error("the swapped element is unnecessary for beforeend");
    },
  };
  assert.equal(conversationTimelineForSwap(timeline, swapped), timeline);
});

test("outerHTML swaps use the newly inserted event target", () => {
  const timeline = {};
  const detachedTarget = {
    classList: {
      contains(value) {
        assert.equal(value, "chat-timeline");
        return false;
      },
    },
  };
  const swapped = {
    closest(selector) {
      assert.equal(selector, ".chat-timeline");
      return timeline;
    },
  };
  assert.equal(
    conversationTimelineForSwap(detachedTarget, swapped),
    timeline
  );
});

test("unrelated swaps resolve no conversation timeline", () => {
  const target = {
    classList: { contains() { return false; } },
  };
  const swapped = {
    closest() { return null; },
  };
  assert.equal(conversationTimelineForSwap(target, swapped), null);
});

function recordedMessage(open) {
  const details = { open };
  return {
    details,
    querySelector(selector) {
      assert.equal(selector, ".round-details");
      return details;
    },
  };
}

test("only the final recorded conversation message stays open", () => {
  const older = recordedMessage(true);
  const latest = recordedMessage(false);
  const timeline = {
    querySelectorAll(selector) {
      assert.equal(selector, ".round, .live-round");
      return [older, latest];
    },
  };
  assert.equal(syncConversationDisclosure(timeline), true);
  assert.equal(older.details.open, false);
  assert.equal(latest.details.open, true);
});

test("a final live message leaves every recorded message closed", () => {
  const older = recordedMessage(true);
  const live = {
    querySelector(selector) {
      assert.equal(selector, ".round-details");
      return null;
    },
  };
  const timeline = {
    querySelectorAll() {
      return [older, live];
    },
  };
  assert.equal(syncConversationDisclosure(timeline), false);
  assert.equal(older.details.open, false);
});

test("closing a file restores the dimmed reader placeholder", () => {
  const children = [];
  let focusOptions = null;
  const reader = {
    ownerDocument: {
      createElement(tagName) {
        assert.equal(tagName, "p");
        return { className: "", textContent: "" };
      },
    },
    replaceChildren() {
      children.length = 0;
    },
    appendChild(child) {
      children.push(child);
    },
    focus(options) {
      focusOptions = options;
    },
  };
  const empty = resetFileReader(reader);
  assert.equal(children.length, 1);
  assert.equal(children[0], empty);
  assert.equal(empty.className, "file-reader-empty");
  assert.equal(empty.textContent, "Select a text file to read it here.");
  assert.deepEqual(focusOptions, { preventScroll: true });
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
  assert.equal(
    shouldClearChatError(`/projects/${project}/files/focus?path=notes.md`),
    false
  );
  assert.equal(shouldClearChatError(`/projects/${project}/chat/select`), true);
});
