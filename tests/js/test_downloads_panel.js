"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { alertDownloadsError } = require("../../app/static/app.js");

function source(insidePanel) {
  return { closest: (selector) => (selector === "#downloads-panel" && insidePanel ? {} : null) };
}

test("a refused download action pops up its reason", function () {
  const shown = [];
  const xhr = { status: 409, responseText: JSON.stringify({ detail: "D1 was refused by you; it cannot be allowed" }) };
  assert.equal(alertDownloadsError(source(true), xhr, (text) => shown.push(text)), true);
  assert.deepEqual(shown, ["D1 was refused by you; it cannot be allowed"]);
});

test("errors outside the Downloads panel are left to the chat error region", function () {
  const shown = [];
  assert.equal(alertDownloadsError(source(false), { status: 409, responseText: "{}" }, (text) => shown.push(text)), false);
  assert.equal(alertDownloadsError(null, { status: 409 }, (text) => shown.push(text)), false);
  assert.deepEqual(shown, []);
});

const { staleDownloadsList } = require("../../app/static/app.js");

function listTarget(revision) {
  return { id: "downloads-list", dataset: { revision: String(revision) } };
}

test("an older list response never replaces a newer one", function () {
  assert.equal(staleDownloadsList(listTarget(7), '<div id="downloads-list" data-revision="5">'), true);
  assert.equal(staleDownloadsList(listTarget(7), '<div id="downloads-list" data-revision="7">'), false);
  assert.equal(staleDownloadsList(listTarget(7), '<div id="downloads-list" data-revision="9">'), false);
});

test("other targets and unstamped responses are left alone", function () {
  assert.equal(staleDownloadsList({ id: "chat-timeline", dataset: {} }, 'data-revision="1"'), false);
  assert.equal(staleDownloadsList(listTarget(7), "<p>no stamp</p>"), false);
  assert.equal(staleDownloadsList(null, 'data-revision="1"'), false);
});

const { newerDownloadsRevision } = require("../../app/static/app.js");

test("only a newer store revision re-fetches the list", function () {
  assert.equal(newerDownloadsRevision(4, JSON.stringify({ revision: 5 })), true);
  assert.equal(newerDownloadsRevision(4, JSON.stringify({ revision: 4 })), false);
  assert.equal(newerDownloadsRevision(4, JSON.stringify({ revision: 2 })), false);
});

test("an unreadable event or list refreshes to be safe", function () {
  assert.equal(newerDownloadsRevision(4, "not json"), true);
  assert.equal(newerDownloadsRevision(Number.NaN, JSON.stringify({ revision: 1 })), true);
});

const { openDownloadsPanelForNew } = require("../../app/static/app.js");

function shownList(awaiting, panel) {
  return { id: "downloads-list", dataset: { awaiting: String(awaiting) }, closest: () => panel };
}

test("a newly awaiting request opens the collapsed panel", function () {
  const panel = { open: false };
  assert.equal(openDownloadsPanelForNew(shownList(0, panel), '<div id="downloads-list" data-revision="3" data-awaiting="1">'), true);
  assert.equal(panel.open, true);
});

test("no new awaiting request leaves the panel as the user set it", function () {
  const panel = { open: false };
  assert.equal(openDownloadsPanelForNew(shownList(2, panel), '<div id="downloads-list" data-awaiting="2">'), false);
  assert.equal(openDownloadsPanelForNew(shownList(2, panel), '<div id="downloads-list" data-awaiting="1">'), false);
  assert.equal(openDownloadsPanelForNew({ id: "questions-list", dataset: {} }, 'data-awaiting="5"'), false);
  assert.equal(panel.open, false);
});

test("a recreated records file (new epoch) is never stale", function () {
  const target = { id: "downloads-list", dataset: { revision: "12", epoch: "aaaa" } };
  assert.equal(staleDownloadsList(target, '<div id="downloads-list" data-revision="1" data-epoch="bbbb">'), false);
  assert.equal(staleDownloadsList(target, '<div id="downloads-list" data-revision="1" data-epoch="aaaa">'), true);
});

test("an event from a new epoch re-fetches whatever its revision", function () {
  assert.equal(newerDownloadsRevision(12, JSON.stringify({ revision: 1, epoch: "bbbb" }), "aaaa"), true);
  assert.equal(newerDownloadsRevision(12, JSON.stringify({ revision: 1, epoch: "aaaa" }), "aaaa"), false);
});

test("a store reset with a dotted downloads.conversions epoch is never shown as stale", function () {
  const shown = { id: "downloads-list", dataset: { revision: "9", epoch: "ab12.cd34" } };
  // Same epoch, older revision: stale. New epoch (a store was recreated): not stale.
  assert.equal(staleDownloadsList(shown, '<div id="downloads-list" data-revision="5" data-epoch="ab12.cd34">'), true);
  assert.equal(staleDownloadsList(shown, '<div id="downloads-list" data-revision="1" data-epoch="ab12.ef56">'), false);
});

test("a conversions list follows the same staleness rule", function () {
  const target = { id: "conversions-list", dataset: { revision: "9", epoch: "a.b.c" } };
  assert.equal(staleDownloadsList(target, '<div id="conversions-list" data-revision="8" data-epoch="a.b.c">'), true);
  assert.equal(staleDownloadsList(target, '<div id="conversions-list" data-revision="3" data-epoch="a.b.d">'), false);
});

test("a newly queued conversion opens the collapsed Conversions panel", function () {
  const panel = { open: false };
  const list = { id: "conversions-list", dataset: { active: "0" }, closest: (s) => (s === "#conversions-panel" ? panel : null) };
  assert.equal(openDownloadsPanelForNew(list, '<div id="conversions-list" data-active="1">'), true);
  assert.equal(panel.open, true);
  const closed = { open: false };
  const same = { id: "conversions-list", dataset: { active: "1" }, closest: () => closed };
  assert.equal(openDownloadsPanelForNew(same, '<div id="conversions-list" data-active="1">'), false);
  assert.equal(closed.open, false);
});

test("a refused Cancel in the Conversions panel pops up too", function () {
  const shown = [];
  const source = { closest: (s) => (s === "#conversions-panel" ? {} : null) };
  const xhr = { status: 409, responseText: JSON.stringify({ detail: "C1 has nothing to cancel: it is converted" }) };
  assert.equal(alertDownloadsError(source, xhr, (text) => shown.push(text)), true);
  assert.deepEqual(shown, ["C1 has nothing to cancel: it is converted"]);
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
