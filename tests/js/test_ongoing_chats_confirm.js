"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { ONGOING_CHATS_MESSAGE, confirmOngoingChats } = require("../../app/static/app.js");

function text(name, value, defaultValue) {
  return { name, type: "textarea", tagName: "TEXTAREA", value, defaultValue };
}

function box(name, checked, defaultChecked) {
  return { name, type: "checkbox", tagName: "INPUT", checked, defaultChecked };
}

function select(name, chosen, saved) {
  const options = ["manual", "automatic"].map((value) => ({
    value,
    selected: value === chosen,
    defaultSelected: value === saved,
  }));
  return { name, type: "select-one", tagName: "SELECT", options };
}

function button(name, value) {
  // Buttons have no defaultValue: comparing one would always count as a change.
  return { name, type: "submit", tagName: "BUTTON", value };
}

function form(elements, attributes) {
  const attrs = attributes || {};
  return {
    elements,
    hasAttribute: (name) => Object.prototype.hasOwnProperty.call(attrs, name),
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
  };
}

function recorder(answer) {
  const asked = [];
  const ask = (message) => {
    asked.push(message);
    return answer;
  };
  return { asked, ask };
}

const ONGOING = { "data-confirm-ongoing-chats": "" };

test("a form without an ongoing chat never asks and saves", () => {
  const { asked, ask } = recorder(false);
  const edited = form([text("chat_skills", "/new.md", "/old.md")], {});
  assert.equal(confirmOngoingChats(edited, null, ask), true);
  assert.deepEqual(asked, []);
});

test("an edited system-part field asks; Cancel saves nothing", () => {
  const { asked, ask } = recorder(false);
  const edited = form(
    [text("chat_skills", "/new.md", "/old.md")],
    { ...ONGOING, "data-confirm-fields": "chat_skills chat_preset" }
  );
  assert.equal(confirmOngoingChats(edited, null, ask), false);
  assert.deepEqual(asked, [ONGOING_CHATS_MESSAGE]);
});

test("OK saves", () => {
  const { ask } = recorder(true);
  const edited = form([box("use_global_instruction", false, true)], {
    ...ONGOING,
    "data-confirm-fields": "use_global_instruction",
  });
  assert.equal(confirmOngoingChats(edited, null, ask), true);
});

test("a save that leaves every listed field as shown does not ask", () => {
  const { asked, ask } = recorder(false);
  const downloads = form(
    [
      box("downloads_enabled", true, true),
      select("downloads_approval", "manual", "manual"),
      // Not a system-part field: the model check does not change the chat notes.
      text("downloads_check_model", "other", "haiku"),
      button("downloads_enabled", "save"),
    ],
    { ...ONGOING, "data-confirm-fields": "downloads_enabled downloads_approval" }
  );
  assert.equal(confirmOngoingChats(downloads, null, ask), true);
  assert.deepEqual(asked, []);
});

test("a changed select among the listed fields asks", () => {
  const { asked, ask } = recorder(false);
  const downloads = form([select("downloads_approval", "automatic", "manual")], {
    ...ONGOING,
    "data-confirm-fields": "downloads_enabled downloads_approval",
  });
  assert.equal(confirmOngoingChats(downloads, null, ask), false);
  assert.equal(asked.length, 1);
});

test("without a field list (a refused page) every save asks", () => {
  const { asked, ask } = recorder(false);
  const refused = form([text("global_instruction", "Typed.", "Typed.")], ONGOING);
  assert.equal(confirmOngoingChats(refused, null, ask), false);
  assert.equal(asked.length, 1);
});

// Chat skills (review F2): list edits reach ongoing chats; only on <-> off asks.
function skills(typed, savedOn) {
  return form([text("chat_skills", typed, typed), button("chat_preset", "code-craft")], {
    ...ONGOING,
    "data-confirm-switch": "chat_skills",
    "data-confirm-switch-on": "chat_preset",
    "data-confirm-saved-on": savedOn ? "true" : "false",
  });
}

const PRESET = { name: "chat_preset", value: "code-craft" };
const SAVE = { name: "", value: "" };

for (const [label, typed, savedOn, submitter, asks] of [
  ["off -> a typed list turns skills on", "/a/SKILL.md", false, SAVE, true],
  ["off -> a preset turns skills on", "", false, PRESET, true],
  ["off -> still empty (blank lines only)", "  \n\n ", false, SAVE, false],
  ["on -> an edited list stays on", "/a/SKILL.md\n/b/SKILL.md", true, SAVE, false],
  ["on -> a preset keeps skills on", "/a/SKILL.md", true, PRESET, false],
  ["on -> emptied turns skills off", " \n", true, SAVE, true],
]) {
  test(`chat skills ${label}: ${asks ? "asks" : "saves without asking"}`, () => {
    const { asked, ask } = recorder(false);
    assert.equal(confirmOngoingChats(skills(typed, savedOn), submitter, ask), !asks);
    assert.equal(asked.length, asks ? 1 : 0);
  });
}
