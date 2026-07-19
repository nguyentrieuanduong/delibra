"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  copyAutoComposerTopic,
  formatRemainingSeconds,
  updateTimeoutCountdown,
} = require("../../app/static/app.js");

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
