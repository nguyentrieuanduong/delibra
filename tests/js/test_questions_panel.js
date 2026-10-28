"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { openQuestionsPanelForNew } = require("../../app/static/app.js");

function listIn(panel) {
  return {
    id: "questions-list",
    closest(selector) {
      assert.equal(selector, "#questions-panel");
      return panel;
    },
  };
}

const ARRIVED = '<li id="question-Q3" class="question question-open">';
const NOTHING_NEW = '<input type="hidden" id="questions-cursor" name="after" value="2" hx-swap-oob="true">';

test("opens a collapsed panel when a new question is appended", function () {
  const panel = { open: false };
  assert.equal(openQuestionsPanelForNew(listIn(panel), ARRIVED), true);
  assert.equal(panel.open, true);
});

test("leaves the panel alone when no question arrived", function () {
  const panel = { open: false };
  assert.equal(openQuestionsPanelForNew(listIn(panel), NOTHING_NEW), false);
  assert.equal(panel.open, false);
});

test("ignores swaps into anything but the questions list", function () {
  const panel = { open: false };
  const item = { id: "question-Q3", closest: () => panel };
  assert.equal(openQuestionsPanelForNew(item, ARRIVED), false);
  assert.equal(openQuestionsPanelForNew(null, ARRIVED), false);
  assert.equal(panel.open, false);
});
