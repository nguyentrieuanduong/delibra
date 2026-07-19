"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  closeAutoSetup,
  copyAutoComposerTopic,
  formatRemainingSeconds,
  setTimeoutFormPending,
  syncAutoDisabledControls,
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
