"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { fileViewPath, resetScrollForNewFile } = require("../../app/static/app.js");

function container(path, scrollTop) {
  return {
    scrollTop: scrollTop,
    querySelector: function (selector) {
      assert.equal(selector, ".file-view-header h3");
      return path === null ? null : { textContent: "  " + path + "\n" };
    },
  };
}

test("reads the shown file's path from its header", function () {
  assert.equal(fileViewPath(container(".plans/a.md", 0)), ".plans/a.md");
  assert.equal(fileViewPath(container(null, 0)), null);
  assert.equal(fileViewPath(null), null);
});

test("a different file starts at the top", function () {
  const reader = container(".plans/b.md", 900);
  assert.equal(resetScrollForNewFile(reader, ".plans/a.md"), true);
  assert.equal(reader.scrollTop, 0);
});

test("the same file re-rendered keeps its place", function () {
  // Shared-context Use, Save and Stop sharing re-render the open file.
  const reader = container(".plans/a.md", 900);
  assert.equal(resetScrollForNewFile(reader, ".plans/a.md"), false);
  assert.equal(reader.scrollTop, 900);
});
