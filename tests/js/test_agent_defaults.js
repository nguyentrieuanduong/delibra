"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { applyAgentDefaults } = require("../../app/static/app.js");

function option(agent, value) {
  return { value, selected: false, agent };
}

function fakeForm(defaults) {
  const model = { value: "opus", dataset: {} };
  const options = [option("claude", "low"), option("claude", "high"), option("claude", "max"),
                   option("codex", "minimal"), option("codex", "high"), option("codex", "xhigh")];
  const effort = {
    dataset: {},
    querySelector(selector) {
      const match = /\[data-effort-agent="([^"]+)"\] option\[value="([^"]+)"\]/.exec(selector);
      return match ? options.find((item) => item.agent === match[1] && item.value === match[2]) || null : null;
    },
  };
  return {
    dataset: { agentDefaults: JSON.stringify(defaults) },
    model,
    options,
    querySelector(selector) {
      if (selector === 'input[name="model"]') return model;
      if (selector === "[data-effort-select]") return effort;
      return null;
    },
    effort,
  };
}

const DEFAULTS = { claude: { model: "opus", effort: "high" }, codex: { model: "", effort: "xhigh" } };

test("switching the agent type fills in that type's model and effort", function () {
  const form = fakeForm(DEFAULTS);
  assert.equal(applyAgentDefaults(form, "codex"), true);
  assert.equal(form.model.value, "");
  assert.deepEqual(form.options.filter((item) => item.selected).map((item) => [item.agent, item.value]),
                   [["codex", "xhigh"]]);
});

test("a field the user already edited keeps what they typed", function () {
  const form = fakeForm(DEFAULTS);
  form.model.value = "my-model";
  form.model.dataset.edited = "true";
  form.effort.dataset.edited = "true";
  applyAgentDefaults(form, "codex");
  assert.equal(form.model.value, "my-model");
  assert.deepEqual(form.options.filter((item) => item.selected), []);
});

test("a form without defaults, or with broken ones, is left alone", function () {
  const form = fakeForm(DEFAULTS);
  form.dataset.agentDefaults = "{not json";
  assert.equal(applyAgentDefaults(form, "codex"), false);
  delete form.dataset.agentDefaults;
  assert.equal(applyAgentDefaults(form, "codex"), false);
  assert.equal(applyAgentDefaults(fakeForm({}), "codex"), false);
  assert.equal(form.model.value, "opus");
});
