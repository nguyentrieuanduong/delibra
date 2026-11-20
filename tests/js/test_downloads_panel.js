"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { alertLiveListError, inLivePanel } = require("../../app/static/app.js");

function source(insidePanel) {
  return { closest: (selector) => (selector === "#downloads-panel" && insidePanel ? {} : null) };
}

test("a refused live-list action pops up its reason", function () {
  const shown = [];
  const xhr = { status: 409, responseText: JSON.stringify({ detail: "D1 was refused by you; it cannot be allowed" }) };
  assert.equal(alertLiveListError(xhr, (text) => shown.push(text)), true);
  assert.deepEqual(shown, ["D1 was refused by you; it cannot be allowed"]);
  assert.equal(alertLiveListError({ status: 200, responseText: "<div>" }, (text) => shown.push(text)), false);
  assert.equal(shown.length, 1);
});

test("only Downloads and Conversions actions are live-list actions", function () {
  assert.equal(inLivePanel(source(true)), true);
  assert.equal(inLivePanel({ closest: (s) => (s === "#conversions-panel" ? {} : null) }), true);
  assert.equal(inLivePanel(source(false)), false);
  assert.equal(inLivePanel(null), false);
});

const { staleLiveList } = require("../../app/static/app.js");

function listTarget(revision) {
  return { id: "downloads-list", dataset: { revision: String(revision) } };
}

test("an older list response never replaces a newer one", function () {
  assert.equal(staleLiveList(listTarget(7), '<div id="downloads-list" data-revision="5">'), true);
  assert.equal(staleLiveList(listTarget(7), '<div id="downloads-list" data-revision="7">'), false);
  assert.equal(staleLiveList(listTarget(7), '<div id="downloads-list" data-revision="9">'), false);
});

test("other targets and unstamped responses are left alone", function () {
  assert.equal(staleLiveList({ id: "chat-timeline", dataset: {} }, 'data-revision="1"'), false);
  assert.equal(staleLiveList(listTarget(7), "<p>no stamp</p>"), false);
  assert.equal(staleLiveList(null, 'data-revision="1"'), false);
});

const { newerLiveRevision } = require("../../app/static/app.js");

test("only a newer store revision re-fetches the list", function () {
  assert.equal(newerLiveRevision(4, JSON.stringify({ revision: 5 })), true);
  assert.equal(newerLiveRevision(4, JSON.stringify({ revision: 4 })), false);
  assert.equal(newerLiveRevision(4, JSON.stringify({ revision: 2 })), false);
});

test("an unreadable event or list refreshes to be safe", function () {
  assert.equal(newerLiveRevision(4, "not json"), true);
  assert.equal(newerLiveRevision(Number.NaN, JSON.stringify({ revision: 1 })), true);
});

const { openLivePanelForNew } = require("../../app/static/app.js");

function shownList(awaiting, panel) {
  return { id: "downloads-list", dataset: { awaiting: String(awaiting) }, closest: () => panel };
}

test("a newly awaiting request opens the collapsed panel", function () {
  const panel = { open: false };
  assert.equal(openLivePanelForNew(shownList(0, panel), '<div id="downloads-list" data-revision="3" data-awaiting="1">'), true);
  assert.equal(panel.open, true);
});

test("no new awaiting request leaves the panel as the user set it", function () {
  const panel = { open: false };
  assert.equal(openLivePanelForNew(shownList(2, panel), '<div id="downloads-list" data-awaiting="2">'), false);
  assert.equal(openLivePanelForNew(shownList(2, panel), '<div id="downloads-list" data-awaiting="1">'), false);
  assert.equal(openLivePanelForNew({ id: "questions-list", dataset: {} }, 'data-awaiting="5"'), false);
  assert.equal(panel.open, false);
});

test("a recreated records file (new epoch) is never stale", function () {
  const target = { id: "downloads-list", dataset: { revision: "12", epoch: "aaaa" } };
  assert.equal(staleLiveList(target, '<div id="downloads-list" data-revision="1" data-epoch="bbbb">'), false);
  assert.equal(staleLiveList(target, '<div id="downloads-list" data-revision="1" data-epoch="aaaa">'), true);
});

test("an event from a new epoch re-fetches whatever its revision", function () {
  assert.equal(newerLiveRevision(12, JSON.stringify({ revision: 1, epoch: "bbbb" }), "aaaa"), true);
  assert.equal(newerLiveRevision(12, JSON.stringify({ revision: 1, epoch: "aaaa" }), "aaaa"), false);
});


test("a conversions list follows the same staleness rule", function () {
  const target = { id: "conversions-list", dataset: { revision: "9", epoch: "a.b.c" } };
  assert.equal(staleLiveList(target, '<div id="conversions-list" data-revision="8" data-epoch="a.b.c">'), true);
  assert.equal(staleLiveList(target, '<div id="conversions-list" data-revision="3" data-epoch="a.b.d">'), false);
});

test("a newly queued conversion opens the collapsed Conversions panel", function () {
  const panel = { open: false };
  const list = { id: "conversions-list", dataset: { active: "0" }, closest: (s) => (s === "#conversions-panel" ? panel : null) };
  assert.equal(openLivePanelForNew(list, '<div id="conversions-list" data-active="1">'), true);
  assert.equal(panel.open, true);
  const closed = { open: false };
  const same = { id: "conversions-list", dataset: { active: "1" }, closest: () => closed };
  assert.equal(openLivePanelForNew(same, '<div id="conversions-list" data-active="1">'), false);
  assert.equal(closed.open, false);
});


const { tickElapsedClocks } = require("../../app/static/app.js");

test("the elapsed clock counts on from the seconds the server rendered", function () {
  const clock = { dataset: { elapsed: "125" }, textContent: "2:05" };
  const doc = { querySelectorAll: () => [clock] };
  tickElapsedClocks(doc, 1_000_000);
  assert.equal(clock.textContent, "2:05");
  tickElapsedClocks(doc, 1_000_000 + 3_600_000);
  assert.equal(clock.textContent, "1:02:05");
});
