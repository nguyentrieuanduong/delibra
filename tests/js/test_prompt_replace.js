"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { promptReplaceMessage, confirmPromptReplace } = require("../../app/static/app.js");

function form(ticked) {
  return {
    querySelectorAll(selector) {
      assert.equal(selector, 'input[name="replace"]:checked');
      return ticked.map((name) => ({ dataset: { projectName: name } }));
    },
  };
}

test("nothing ticked: no confirm, the save goes ahead", () => {
  assert.equal(promptReplaceMessage([], "save"), null);
  assert.equal(confirmPromptReplace(form([]), { submitter: { value: "save" } }, () => false), true);
});

test("the confirm names every ticked project and what replaces their versions", () => {
  assert.equal(
    promptReplaceMessage(["Alpha", "Beta"], "save"),
    "Replace this prompt in Alpha, Beta with the global text? Their own versions are removed."
  );
  assert.equal(
    promptReplaceMessage(["Alpha"], "reset"),
    "Replace this prompt in Alpha with the built-in text? Their own versions are removed."
  );
});

test("the answer decides whether the form is sent", () => {
  const asked = [];
  const answer = (message) => { asked.push(message); return false; };
  assert.equal(confirmPromptReplace(form(["Alpha"]), { submitter: { value: "save" } }, answer), false);
  assert.deepEqual(asked, ["Replace this prompt in Alpha with the global text? Their own versions are removed."]);
});
