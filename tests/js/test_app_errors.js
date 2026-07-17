"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { chatErrorMessage, renderChatError } = require("../../app/static/app.js");

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
