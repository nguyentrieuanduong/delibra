"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  autoSetupPlacement,
  closeAutoSetup,
  copyAutoComposerTopic,
  formatRemainingSeconds,
  openAutoSetup,
  setTimeoutFormPending,
  syncAutoDisabledControls,
  syncComposerAvailability,
  updateTimeoutCountdown,
} = require("../../app/static/app.js");

test("closes the Auto setup dialog without parsing or inserting HTML", function () {
  let closed = false;
  let removed = false;
  const dialog = {
    close() {
      closed = true;
    },
    remove() {
      removed = true;
    },
  };
  const control = {
    closest(selector) {
      assert.equal(selector, "[data-auto-setup-dialog]");
      return dialog;
    },
  };

  assert.equal(closeAutoSetup(control), true);
  assert.equal(closed, true);
  assert.equal(removed, true);
});

test("Auto status disables and restores only controls it owns", function () {
  const autoControl = { disabled: false, dataset: {} };
  const intrinsicallyDisabled = { disabled: true, dataset: {} };
  const status = { dataset: { autoActive: "true" } };
  const documentRoot = {
    querySelector(selector) {
      assert.equal(selector, "#auto-status");
      return status;
    },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-disable-during-auto]");
      return [autoControl, intrinsicallyDisabled];
    },
  };

  assert.equal(syncAutoDisabledControls(documentRoot), true);
  assert.equal(autoControl.disabled, true);
  assert.equal(autoControl.dataset.autoDisabled, "true");
  assert.equal(intrinsicallyDisabled.dataset.autoDisabled, undefined);

  status.dataset.autoActive = "false";
  assert.equal(syncAutoDisabledControls(documentRoot), false);
  assert.equal(autoControl.disabled, false);
  assert.equal(autoControl.dataset.autoDisabled, undefined);
  assert.equal(intrinsicallyDisabled.disabled, true);
});

test("timeout submissions disable and restore only controls owned by the request", function () {
  const enabled = { disabled: false, dataset: {} };
  const capped = { disabled: true, dataset: {} };
  const form = {
    querySelectorAll(selector) {
      assert.equal(selector, "button, input");
      return [enabled, capped];
    },
  };

  assert.equal(setTimeoutFormPending(form, true), true);
  assert.equal(enabled.disabled, true);
  assert.equal(enabled.dataset.timeoutPendingDisabled, "true");
  assert.equal(capped.dataset.timeoutPendingDisabled, undefined);

  assert.equal(setTimeoutFormPending(form, false), false);
  assert.equal(enabled.disabled, false);
  assert.equal(enabled.dataset.timeoutPendingDisabled, undefined);
  assert.equal(capped.disabled, true);
});

test("formats bounded relative seconds as a stable clock", function () {
  assert.equal(formatRemainingSeconds(0), "0:00");
  assert.equal(formatRemainingSeconds(5), "0:05");
  assert.equal(formatRemainingSeconds(65), "1:05");
  assert.equal(formatRemainingSeconds(3661), "1:01:01");
  assert.equal(formatRemainingSeconds(-1), "0:00");
});

test("countdown changes textContent from the server relative duration only", function () {
  let rendered = "";
  const remaining = {
    set textContent(value) {
      rendered = value;
    },
    set innerHTML(_value) {
      throw new Error("innerHTML must never be used");
    },
  };
  const controls = {
    dataset: { timeoutSeconds: "65" },
    querySelector(selector) {
      assert.equal(selector, "[data-timeout-remaining]");
      return remaining;
    },
  };

  assert.equal(updateTimeoutCountdown(controls, 10_000), 65);
  assert.equal(rendered, "1:05");
  assert.equal(updateTimeoutCountdown(controls, 15_900), 60);
  assert.equal(rendered, "1:00");
});

test("copies a composer topic into an opted-in Auto setup using values only", function () {
  const topic = { value: "" };
  const composer = { value: "Discuss <unsafe> as plain text" };
  const setup = {
    dataset: { topicSource: "composer" },
    ownerDocument: {
      querySelector(selector) {
        assert.equal(selector, '#chat-composer textarea[name="prompt"]');
        return composer;
      },
    },
    querySelector(selector) {
      assert.equal(selector, 'textarea[name="topic"]');
      return topic;
    },
  };

  assert.equal(copyAutoComposerTopic(setup), true);
  assert.equal(topic.value, composer.value);
});

function setupWith(source, composerValue, topicValue) {
  const topic = { value: topicValue };
  return {
    topic,
    setup: {
      dataset: { topicSource: source },
      ownerDocument: {
        querySelector(selector) {
          assert.equal(selector, '#chat-composer textarea[name="prompt"]');
          return { value: composerValue };
        },
      },
      querySelector(selector) {
        assert.equal(selector, 'textarea[name="topic"]');
        return topic;
      },
    },
  };
}

test("unsent composer text replaces a durable topic", function () {
  const { setup, topic } = setupWith("durable", "Plan the roadmap", "Earliest prompt");
  assert.equal(copyAutoComposerTopic(setup), true);
  assert.equal(topic.value, "Plan the roadmap");
});

test("an empty composer keeps the durable topic", function () {
  const { setup, topic } = setupWith("durable", "  \n ", "Earliest prompt");
  assert.equal(copyAutoComposerTopic(setup), false);
  assert.equal(topic.value, "Earliest prompt");
});

test("the composer is blocked by Auto, by no selection, or by a busy agent", function () {
  const sessionId = "a".repeat(32);
  const textarea = { disabled: false, dataset: {} };
  const send = { disabled: false, dataset: {} };
  const autoStatus = { dataset: { autoActive: "false" } };
  const agentStatus = { dataset: { agentStatus: "running" } };
  const composer = {
    dataset: { sessionId: sessionId },
    querySelectorAll(selector) {
      assert.equal(selector, "[data-composer-send]");
      return [textarea, send];
    },
  };
  const documentRoot = {
    querySelector(selector) {
      if (selector === "#chat-composer") return composer;
      if (selector === "#auto-status") return autoStatus;
      if (selector === `#agent-status-${sessionId}`) return agentStatus;
      throw new Error(`unexpected selector ${selector}`);
    },
  };

  // A running agent blocks the send even though Auto has finished.
  assert.equal(syncComposerAvailability(documentRoot), true);
  assert.equal(textarea.disabled, true);
  assert.equal(send.disabled, true);

  // The turn lands; the composer opens without being re-rendered.
  agentStatus.dataset.agentStatus = "idle";
  assert.equal(syncComposerAvailability(documentRoot), false);
  assert.equal(textarea.disabled, false);
  assert.equal(send.disabled, false);

  // An active Auto run blocks it again.
  autoStatus.dataset.autoActive = "true";
  assert.equal(syncComposerAvailability(documentRoot), true);
  assert.equal(textarea.disabled, true);

  // So does having no agent selected at all.
  autoStatus.dataset.autoActive = "false";
  composer.dataset.sessionId = "";
  assert.equal(syncComposerAvailability(documentRoot), true);
  assert.equal(textarea.disabled, true);
});

test("opens the Auto setup dialog as a modal once and removes it on close", function () {
  const listeners = {};
  let modalCalls = 0;
  let removed = false;
  const dialog = {
    open: false,
    dataset: {},
    showModal() {
      modalCalls += 1;
      this.open = true;
    },
    addEventListener(name, handler) {
      listeners[name] = handler;
    },
    remove() {
      removed = true;
    },
  };

  assert.equal(openAutoSetup(dialog), true);
  assert.equal(openAutoSetup(dialog), false);
  assert.equal(modalCalls, 1);
  listeners.close();
  assert.equal(removed, true);
});

test("places the Auto setup dialog at the Message box", function () {
  assert.deepEqual(autoSetupPlacement({ top: 50, left: 257 }, 672, 1400, 900), {
    top: 50,
    left: 257,
    maxHeight: 842,
  });
});

test("keeps the Auto setup dialog inside the viewport", function () {
  // Too far right to fit: slide left. Scrolled above the top: pin to the edge.
  assert.deepEqual(autoSetupPlacement({ top: -200, left: 900 }, 672, 1400, 900), {
    top: 8,
    left: 720,
    maxHeight: 884,
  });
  // Low on the page: never start below half the viewport.
  assert.equal(autoSetupPlacement({ top: 800, left: 0 }, 672, 1400, 900).top, 450);
  assert.equal(autoSetupPlacement({ top: 50, left: -40 }, 672, 1400, 900).left, 8);
});
