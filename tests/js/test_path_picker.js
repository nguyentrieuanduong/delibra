"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  appendPickedPaths,
  applyPathPicker,
  isStalePickerSwap,
  lastPathLine,
  openPathPicker,
  pickerUrl,
} = require("../../app/static/app.js");

function checkbox(value, checked = true) {
  return { checked, dataset: { pickerValue: value } };
}

// An open dialog for `field`; `ticked` are the listing's checkboxes,
// `chosen` the Will include list's value, `list` the row's read-only list id.
function fakeDocument(
  field,
  { ticked = [], subdirs = false, chosen = null, list = "", listName = "files" } = {}
) {
  const events = [];
  const calls = [];
  const body = {
    id: "path-picker-body",
    querySelectorAll(selector) {
      assert.equal(selector, "input[data-picker-value]:checked");
      return ticked.filter((box) => box.checked);
    },
    querySelector(selector) {
      assert.equal(selector, "input[data-picker-subdirs]");
      return { checked: subdirs };
    },
  };
  const dialog = {
    open: true,
    dataset: { pickerTarget: field.id, pickerSession: "4", pickerList: list, pickerListName: listName },
    close() {
      this.open = false;
    },
  };
  const elements = {
    "path-picker": dialog,
    "path-picker-body": body,
    "picker-files": { id: "picker-files" },
    [field.id]: field,
  };
  if (chosen !== null) {
    elements["picker-files-value"] = { value: chosen };
  }
  if (list) {
    elements[list] = { id: list };
  }
  field.dispatchEvent = (event) => events.push([event.type, event.bubbles]);
  return {
    calls,
    dialog,
    elements,
    events,
    doc: { getElementById: (id) => elements[id] || null },
    htmxApi: { ajax: (...args) => calls.push(args) },
  };
}

function control(selector, value) {
  return { dataset: { pickerValue: value }, matches: (candidate) => candidate === selector };
}

test("the start path is the field's last non-empty line", function () {
  assert.equal(lastPathLine("~/a\n\n /opt/b.md \n"), "/opt/b.md");
  assert.equal(lastPathLine(""), "");
});

test("picked paths are appended once and blank lines dropped", function () {
  const field = { value: "~/a\n\n~/b\n" };
  assert.equal(appendPickedPaths(field, ["~/b", "~/c", "~/c", "~/d"]), 2);
  assert.equal(field.value, "~/a\n~/b\n~/c\n~/d");
  assert.equal(appendPickedPaths(field, ["~/a"]), 0);
  assert.equal(field.value, "~/a\n~/b\n~/c\n~/d");
});

test("the browse url carries the field's last path", function () {
  assert.equal(
    pickerUrl("/browse?mode=preset", { value: "~/x y\n" }),
    "/browse?mode=preset&start=~%2Fx%20y"
  );
  assert.equal(pickerUrl("/projects/P/browse", { value: "" }), "/projects/P/browse?start=");
});

test("Add appends ticked paths, fires one bubbling input and closes", function () {
  const field = { id: "field", value: "~/a" };
  const t = fakeDocument(field, { ticked: [checkbox("~/b")] });
  assert.equal(applyPathPicker(control("[data-path-picker-add]"), t.doc, t.htmxApi), true);
  assert.equal(field.value, "~/a\n~/b");
  assert.deepEqual(t.events, [["input", true]]);
  assert.equal(t.dialog.open, false);
});

test("Choose replaces the field; Cancel changes nothing", function () {
  const field = { id: "field", value: "/old" };
  const t = fakeDocument(field);
  assert.equal(applyPathPicker(control("[data-path-picker-choose]", "/new"), t.doc, t.htmxApi), true);
  assert.equal(field.value, "/new");
  const kept = { id: "field", value: "/keep" };
  const cancelled = fakeDocument(kept, { ticked: [checkbox("/x")] });
  assert.equal(
    applyPathPicker(control("[data-path-picker-close]"), cancelled.doc, cancelled.htmxApi),
    false
  );
  assert.equal(kept.value, "/keep");
  assert.deepEqual(cancelled.events, []);
  assert.equal(cancelled.dialog.open, false);
});

test("Add ticked to list posts the picks, clears the ticks and stays open", function () {
  const field = { id: "preset-files-0", value: "~/a.md" };
  const ticks = [checkbox("~/skills"), checkbox("~/c.md"), checkbox("~/d.md", false)];
  const t = fakeDocument(field, {
    ticked: ticks,
    subdirs: true,
    chosen: "~/a.md",
    list: "preset-skills-list-0",
    listName: "skills",
  });
  assert.equal(applyPathPicker(control("[data-picker-add-files]"), t.doc, t.htmxApi), false);
  assert.deepEqual(t.calls, [
    [
      "POST",
      "/settings/preset-files",
      {
        source: t.elements["picker-files"],
        target: "#picker-files",
        swap: "innerHTML",
        values: {
          view: "dialog",
          files: "~/a.md",
          add: "~/skills\n~/c.md",
          subdirs: "true",
          list: "skills",
        },
        headers: { "X-Delibra-Picker": "4" },
      },
    ],
  ]);
  assert.deepEqual(
    ticks.map((box) => box.checked),
    [false, false, false]
  );
  assert.equal(t.dialog.open, true);
  assert.equal(field.value, "~/a.md");
});

test("Use these files copies the list into the row and refreshes its read-only view", function () {
  const field = { id: "preset-files-0", value: "~/a.md" };
  const t = fakeDocument(field, { chosen: "~/a.md\n~/b.md", list: "preset-files-list-0" });
  assert.equal(applyPathPicker(control("[data-path-picker-use]"), t.doc, t.htmxApi), true);
  assert.equal(field.value, "~/a.md\n~/b.md");
  assert.deepEqual(t.calls, [
    [
      "POST",
      "/settings/preset-files",
      {
        source: t.dialog,
        target: t.elements["preset-files-list-0"],
        swap: "innerHTML",
        values: { view: "row", files: "~/a.md\n~/b.md", list: "files" },
      },
    ],
  ]);
  assert.equal(t.dialog.open, false);
});

function openingDocument() {
  const calls = [];
  const dialog = {
    open: false,
    dataset: {},
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    showModal() {
      this.open = true;
    },
  };
  // The previous opening's controls, still in the dialog.
  const body = { id: "path-picker-body", innerHTML: "<button data-path-picker-add>Add</button>" };
  const files = {
    id: "picker-files",
    hidden: true,
    innerHTML: "<button data-path-picker-use>Use these files</button>",
  };
  const fields = { "preset-files-0": { value: "~/a.md\n~/b.md" }, "register-path": { value: "" } };
  const elements = Object.assign(
    { "path-picker": dialog, "path-picker-body": body, "picker-files": files },
    fields
  );
  const doc = { getElementById: (id) => elements[id] || null };
  const htmxApi = { ajax: (...args) => calls.push(args) };
  return { body, calls, dialog, doc, files, htmxApi };
}

function opener(url, target, list, listName) {
  const dataset = { pickerUrl: url, pickerTarget: target };
  if (list) {
    dataset.pickerFilesList = list;
  }
  if (listName) {
    dataset.pickerList = listName;
  }
  return { dataset };
}

test("opening for a preset clears both regions and loads the listing and the row's list", function () {
  const { body, calls, dialog, doc, files, htmxApi } = openingDocument();
  const button = opener("/browse?mode=preset", "preset-files-0", "preset-files-list-0");
  assert.equal(openPathPicker(button, doc, htmxApi), true);
  const headers = { "X-Delibra-Picker": "1" };
  assert.equal(dialog.dataset.pickerSession, "1");
  assert.equal(dialog.dataset.pickerTarget, "preset-files-0");
  assert.equal(dialog.dataset.pickerList, "preset-files-list-0");
  assert.equal(dialog.dataset.pickerListName, "files");
  assert.equal(dialog.attributes["hx-headers"], '{"X-Delibra-Picker":"1"}');
  assert.match(body.innerHTML, /Loading…/);
  assert.doesNotMatch(body.innerHTML, /data-path-picker-|data-picker-add-files/);
  assert.equal(files.hidden, false);
  assert.match(files.innerHTML, /Loading…/);
  assert.match(files.innerHTML, /data-path-picker-close/);
  assert.doesNotMatch(files.innerHTML, /data-path-picker-use/);
  assert.equal(dialog.open, true);
  assert.deepEqual(calls, [
    [
      "GET",
      "/browse?mode=preset&start=~%2Fb.md",
      { source: dialog, target: "#path-picker-body", swap: "outerHTML", headers },
    ],
    [
      "POST",
      "/settings/preset-files",
      {
        source: files,
        target: "#picker-files",
        swap: "innerHTML",
        values: { view: "dialog", files: "~/a.md\n~/b.md", list: "files" },
        headers,
      },
    ],
  ]);
});

test("opening a folder picker next hides the list and starts a new session", function () {
  const { body, calls, dialog, doc, files, htmxApi } = openingDocument();
  openPathPicker(opener("/browse?mode=preset", "preset-files-0", "preset-files-list-0"), doc, htmxApi);
  dialog.open = false;
  body.innerHTML = "<button data-picker-add-files>Add ticked to list</button>";
  files.innerHTML = "<button data-path-picker-use>Use these files</button>";

  openPathPicker(opener("/browse?mode=folder", "register-path"), doc, htmxApi);

  assert.equal(dialog.dataset.pickerSession, "2");
  assert.equal(dialog.dataset.pickerTarget, "register-path");
  assert.equal(dialog.dataset.pickerList, "");
  assert.equal(files.hidden, true);
  assert.equal(files.innerHTML, "");
  assert.doesNotMatch(body.innerHTML, /data-picker-add-files/);
  assert.match(body.innerHTML, /data-path-picker-close/);
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[2][2].headers, { "X-Delibra-Picker": "2" });
});

test("only the current opening's answers may be swapped in", function () {
  const dialog = { open: true, dataset: { pickerSession: "2" } };
  const doc = { getElementById: (id) => (id === "path-picker" ? dialog : null) };
  const swap = (session, id = "path-picker-body") => ({
    target: { id },
    requestConfig: { headers: session === null ? {} : { "X-Delibra-Picker": session } },
  });

  assert.equal(isStalePickerSwap(swap("2"), doc), false);
  assert.equal(isStalePickerSwap(swap("2", "picker-files"), doc), false);
  assert.equal(isStalePickerSwap(swap("1"), doc), true);
  assert.equal(isStalePickerSwap(swap("1", "picker-files"), doc), true);
  assert.equal(isStalePickerSwap(swap(null), doc), true);
  assert.equal(isStalePickerSwap(swap("1", "chat"), doc), false);
  dialog.open = false;
  assert.equal(isStalePickerSwap(swap("2"), doc), true);
});

test("opening the skills list asks for that list", function () {
  const { calls, dialog, doc, htmxApi } = openingDocument();
  openPathPicker(
    opener("/browse?mode=preset", "preset-files-0", "preset-skills-list-0", "skills"),
    doc,
    htmxApi
  );
  assert.equal(dialog.dataset.pickerListName, "skills");
  assert.deepEqual(calls[1][2].values, {
    view: "dialog",
    files: "~/a.md\n~/b.md",
    list: "skills",
  });
});
