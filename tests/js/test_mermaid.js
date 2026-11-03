"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { mermaidErrorText, mermaidFrameSize, renderMermaidSource } = require("../../app/static/app.js");

test("renders each source once and reuses the result", async function () {
  const calls = [];
  const fake = {
    render: function (id, source) {
      calls.push([id, source]);
      return Promise.resolve({ svg: "<svg>" + source.length + "</svg>" });
    },
  };
  const first = await renderMermaidSource(fake, "graph TD\nA-->B");
  const again = await renderMermaidSource(fake, "graph TD\nA-->B");
  assert.deepEqual(first, { svg: "<svg>14</svg>" });
  assert.equal(again, first);
  assert.equal(calls.length, 1);
  assert.match(calls[0][0], /^delibra-mermaid-\d+$/);
});

test("a failed render becomes an error result, not a rejection", async function () {
  const fake = { render: () => Promise.reject(new Error("Parse error on line 2:\n...A -->\n---^")) };
  assert.deepEqual(await renderMermaidSource(fake, "flowchart LR\n  A -->"), {
    error: "Parse error on line 2: ...A --> ---^",
  });
});

test("error text is one line of at most 300 characters", function () {
  assert.equal(mermaidErrorText(new Error("x".repeat(400))).length, 300);
  assert.equal(mermaidErrorText("plain\n\nstring"), "plain string");
  assert.equal(mermaidErrorText(undefined), "unknown error");
});

test("reads the diagram's size from the frame's data URL", function () {
  const svg = '<body style="margin:0"><svg id="d" style="max-width: 360px;" viewBox="4 4 360.5 64">x</svg></body>';
  const src = "data:text/html;charset=UTF-8;base64," + Buffer.from(svg).toString("base64");
  assert.deepEqual(mermaidFrameSize(src), { width: 360.5, height: 64 });
  assert.equal(mermaidFrameSize("data:text/html;base64," + Buffer.from("<svg></svg>").toString("base64")), null);
  assert.equal(mermaidFrameSize("https://example.com/"), null);
  assert.equal(mermaidFrameSize(null), null);
});
