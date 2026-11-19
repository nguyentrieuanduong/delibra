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

test("a listed submit button (a skills preset) asks even with the text unchanged", () => {
  const { asked, ask } = recorder(false);
  const skills = form([text("chat_skills", "/same.md", "/same.md")], {
    ...ONGOING,
    "data-confirm-fields": "chat_skills chat_preset",
  });
  assert.equal(confirmOngoingChats(skills, { name: "chat_preset", value: "code-craft" }, ask), false);
  assert.equal(asked.length, 1);
});

test("without a field list (a refused page) every save asks", () => {
  const { asked, ask } = recorder(false);
  const refused = form([text("chat_skills", "/typed.md", "/typed.md")], ONGOING);
  assert.equal(confirmOngoingChats(refused, null, ask), false);
  assert.equal(asked.length, 1);
});
