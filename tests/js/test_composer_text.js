"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { keepComposerText } = require("../../app/static/app.js");

function composer(id, value) {
  const box = { value };
  return {
    id,
    box,
    dataset: {},
    querySelector(selector) {
      assert.equal(selector, 'textarea[name="prompt"]');
      return box;
    },
  };
}

test("the unsent message moves into the re-rendered composer", () => {
  const shown = composer("chat-composer", "Draft\nline two");
  const incoming = composer("chat-composer", "");

  assert.equal(keepComposerText({ target: shown, fragment: incoming }), true);
  assert.equal(incoming.box.value, "Draft\nline two");
});

test("an empty box and other out-of-band targets are left alone", () => {
  const incoming = composer("chat-composer", "");

  assert.equal(keepComposerText({ target: composer("chat-composer", ""), fragment: incoming }), false);
  assert.equal(keepComposerText({ target: composer("auto-notes-list", "x"), fragment: incoming }), false);
  assert.equal(incoming.box.value, "");
});
