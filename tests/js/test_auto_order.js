"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { moveAutoOrderItem } = require("../../app/static/app.js");

function orderButton(list, item, direction) {
  return {
    dataset: { autoOrderMove: direction },
    disabled: false,
    closest(selector) {
      assert.equal(selector, "[data-auto-order-item]");
      return item;
    },
    focus() {
      list.focused = this;
    },
  };
}

function orderItem(list, name) {
  const item = {
    name,
    parentElement: list,
    get previousElementSibling() {
      return list.children[list.children.indexOf(item) - 1] || null;
    },
    get nextElementSibling() {
      return list.children[list.children.indexOf(item) + 1] || null;
    },
    querySelector(selector) {
      const match = /^\[data-auto-order-move="(up|down)"\]$/.exec(selector);
      assert.ok(match, selector);
      return item.buttons[match[1]];
    },
  };
  item.buttons = {
    up: orderButton(list, item, "up"),
    down: orderButton(list, item, "down"),
  };
  return item;
}

function orderList(names) {
  const list = {
    children: [],
    focused: null,
    insertBefore(node, reference) {
      this.children.splice(this.children.indexOf(node), 1);
      const at = reference === null ? this.children.length : this.children.indexOf(reference);
      this.children.splice(at, 0, node);
      return node;
    },
  };
  for (const name of names) {
    list.children.push(orderItem(list, name));
  }
  // The server-rendered state: only the outer ends are disabled.
  list.children[0].buttons.up.disabled = true;
  list.children[list.children.length - 1].buttons.down.disabled = true;
  return list;
}

function names(list) {
  return list.children.map((item) => item.name);
}

function disabledEnds(list) {
  return list.children.map((item) => [item.buttons.up.disabled, item.buttons.down.disabled]);
}

test("moves an agent down and keeps focus on the pressed control", function () {
  const list = orderList(["A", "B", "C"]);
  const control = list.children[0].buttons.down;
  assert.equal(moveAutoOrderItem(control), true);
  assert.deepEqual(names(list), ["B", "A", "C"]);
  assert.deepEqual(disabledEnds(list), [[true, false], [false, false], [false, true]]);
  assert.equal(list.focused, control);
});

test("moving an agent to the top hands focus to its down control", function () {
  const list = orderList(["A", "B", "C"]);
  const item = list.children[1];
  assert.equal(moveAutoOrderItem(item.buttons.up), true);
  assert.deepEqual(names(list), ["B", "A", "C"]);
  assert.equal(item.buttons.up.disabled, true);
  assert.equal(list.focused, item.buttons.down);
});

test("moving an agent to the bottom hands focus to its up control", function () {
  const list = orderList(["A", "B", "C"]);
  const item = list.children[1];
  assert.equal(moveAutoOrderItem(item.buttons.down), true);
  assert.deepEqual(names(list), ["A", "C", "B"]);
  assert.deepEqual(disabledEnds(list), [[true, false], [false, false], [false, true]]);
  assert.equal(list.focused, item.buttons.up);
});

test("ignores a move past either end", function () {
  const list = orderList(["A", "B"]);
  assert.equal(moveAutoOrderItem(list.children[0].buttons.up), false);
  assert.equal(moveAutoOrderItem(list.children[1].buttons.down), false);
  assert.deepEqual(names(list), ["A", "B"]);
  assert.equal(list.focused, null);
});

test("ignores controls outside an order list", function () {
  assert.equal(moveAutoOrderItem(null), false);
  assert.equal(moveAutoOrderItem({}), false);
  assert.equal(moveAutoOrderItem({ closest: () => null }), false);
});
