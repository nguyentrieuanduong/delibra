"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  freshStreamElement,
  retargetDetachedSwap,
  streamRetryDelay,
} = require("../../app/static/app.js");

function documentWith(elements) {
  return {
    getElementById(id) {
      return elements[id] || null;
    },
  };
}

test("a swap whose target is still on the page is left alone", function () {
  const target = { id: "auto-status", isConnected: true };
  const detail = { target };

  assert.equal(retargetDetachedSwap(detail, documentWith({})), true);
  assert.equal(detail.target, target);
});

test("a swap onto a replaced element goes to its successor by id", function () {
  const successor = { id: "auto-status", isConnected: true };
  const detail = { target: { id: "auto-status", isConnected: false } };

  assert.equal(
    retargetDetachedSwap(detail, documentWith({ "auto-status": successor })),
    true
  );
  assert.equal(detail.target, successor);
});

test("a swap onto a removed element with no successor is dropped", function () {
  const anonymous = { target: { id: "", isConnected: false } };
  const gone = { target: { id: "auto-status", isConnected: false } };

  assert.equal(retargetDetachedSwap(anonymous, documentWith({})), false);
  assert.equal(retargetDetachedSwap(gone, documentWith({})), false);
});

test("a fresh stream element drops the text its old stream already showed", function () {
  const text = { textContent: "Hello" };
  const progress = { textContent: "..." };
  const copy = {
    querySelectorAll(selector) {
      assert.equal(selector, ".live-text, .live-progress");
      return [text, progress];
    },
  };
  const element = {
    cloneNode(deep) {
      assert.equal(deep, true);
      return copy;
    },
  };

  assert.equal(freshStreamElement(element), copy);
  assert.equal(text.textContent, "");
  assert.equal(progress.textContent, "");
});

test("refused reconnects back off up to thirty seconds", function () {
  assert.equal(streamRetryDelay(0), 1000);
  assert.equal(streamRetryDelay(1), 2000);
  assert.equal(streamRetryDelay(4), 16000);
  assert.equal(streamRetryDelay(5), 30000);
  assert.equal(streamRetryDelay(50), 30000);
  assert.equal(streamRetryDelay(Number.NaN), 1000);
  assert.equal(streamRetryDelay(-3), 1000);
});
