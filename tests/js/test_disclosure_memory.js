"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  browserStorage,
  readDisclosureChoices,
  rememberDisclosureChoice,
  syncConversationDisclosure,
  syncAgentDisclosure,
  collapseConversation,
  syncCollapseButton,
  DISCLOSURE_LIMIT,
  MESSAGE_DISCLOSURE_KEY,
} = require("../../app/static/app.js");

function memoryStorage(initial = {}) {
  const items = { ...initial };
  return {
    items,
    getItem: (key) => (key in items ? items[key] : null),
    setItem: (key, value) => {
      items[key] = String(value);
    },
  };
}

function throwingStorage() {
  return {
    getItem() {
      throw new Error("SecurityError");
    },
    setItem() {
      throw new Error("QuotaExceededError");
    },
  };
}

function message(id, hasDetails = true) {
  const details = hasDetails ? { open: false } : null;
  return { id, details, querySelector: () => details };
}

function timelineOf(messages) {
  return {
    querySelectorAll(selector) {
      assert.equal(selector, ".round, .live-round");
      return messages;
    },
  };
}

function agentCard(sessionId, open = false) {
  const details = { open };
  return { dataset: { sessionId }, details, querySelector: () => details };
}

test("a localStorage getter that throws gives no storage", () => {
  const win = {};
  Object.defineProperty(win, "localStorage", {
    get() {
      throw new Error("SecurityError");
    },
  });
  assert.equal(browserStorage(win), null);
  assert.equal(browserStorage(null), null);
  const storage = memoryStorage();
  assert.equal(browserStorage({ localStorage: storage }), storage);
});

test("unreadable choices count as none", () => {
  const key = "delibra.messageDisclosure";
  assert.deepEqual(readDisclosureChoices(null, key), {});
  assert.deepEqual(readDisclosureChoices(memoryStorage(), key), {});
  assert.deepEqual(readDisclosureChoices(throwingStorage(), key), {});
  for (const bad of ["{", "[true]", "null", "7", '"x"']) {
    assert.deepEqual(readDisclosureChoices(memoryStorage({ [key]: bad }), key), {});
  }
  const mixed = memoryStorage({ [key]: '{"a":true,"b":"yes","c":false,"d":1}' });
  assert.deepEqual(readDisclosureChoices(mixed, key), { a: true, c: false });
});

test("a recorded choice moves to the end and the oldest falls off past the limit", () => {
  const key = "delibra.agentDisclosure";
  const storage = memoryStorage();
  assert.equal(rememberDisclosureChoice(storage, key, "a", true), true);
  assert.equal(rememberDisclosureChoice(storage, key, "b", false), true);
  assert.equal(rememberDisclosureChoice(storage, key, "a", false), true);
  assert.deepEqual(Object.keys(JSON.parse(storage.items[key])), ["b", "a"]);
  assert.deepEqual(readDisclosureChoices(storage, key), { b: false, a: false });

  const full = memoryStorage();
  for (let i = 0; i <= DISCLOSURE_LIMIT; i += 1) {
    rememberDisclosureChoice(full, key, `id${i}`, true);
  }
  const kept = Object.keys(readDisclosureChoices(full, key));
  assert.equal(DISCLOSURE_LIMIT, 200);
  assert.equal(kept.length, DISCLOSURE_LIMIT);
  assert.equal(kept[0], "id1");
  assert.equal(kept[kept.length - 1], `id${DISCLOSURE_LIMIT}`);
});

test("a choice that cannot be stored reports false", () => {
  assert.equal(rememberDisclosureChoice(null, "k", "a", true), false);
  assert.equal(rememberDisclosureChoice(throwingStorage(), "k", "a", true), false);
});

test("an opened older message stays open beside the newest", () => {
  const older = message("round-a-1");
  const middle = message("round-a-2");
  const newest = message("round-a-3");
  middle.details.open = true;
  syncConversationDisclosure(timelineOf([older, middle, newest]), { "round-a-1": true });
  assert.equal(older.details.open, true);
  assert.equal(middle.details.open, false);
  assert.equal(newest.details.open, true);
});

test("a closed newest message stays closed", () => {
  const older = message("round-a-1");
  const newest = message("round-a-2");
  assert.equal(
    syncConversationDisclosure(timelineOf([older, newest]), { "round-a-2": false }),
    false
  );
  assert.equal(older.details.open, false);
  assert.equal(newest.details.open, false);
});

test("without choices only the newest message is open", () => {
  const older = message("round-a-1");
  const newest = message("round-a-2");
  assert.equal(syncConversationDisclosure(timelineOf([older, newest])), true);
  assert.equal(older.details.open, false);
  assert.equal(newest.details.open, true);
});

test("Agent information opens only where the reader opened it", () => {
  const alpha = agentCard("a");
  const beta = agentCard("b", true);
  const gamma = agentCard("c", true);
  const root = {
    querySelectorAll(selector) {
      assert.equal(selector, ".agent-card[data-session-id]");
      return [alpha, beta, gamma];
    },
  };
  assert.equal(syncAgentDisclosure(root, { a: true, c: false }), 1);
  assert.equal(alpha.details.open, true);
  assert.equal(beta.details.open, false);
  assert.equal(gamma.details.open, false);
  assert.equal(syncAgentDisclosure(root, {}), 0);
  assert.equal(alpha.details.open, false);
});

function openMessages(ids) {
  return ids.map((id) => {
    const item = message(id);
    item.details.open = true;
    return item;
  });
}

test("Collapse all closes every message and stores only what would reopen", () => {
  const key = MESSAGE_DISCLOSURE_KEY;
  const storage = memoryStorage({
    [key]: JSON.stringify({ "other-project-round": true, "round-a-1": true, "round-a-2": false }),
  });
  const messages = openMessages(["round-a-1", "round-a-2", "round-a-3", "round-a-4"]);
  messages.push(message("round-a-5", false));
  const newest = message("round-a-6");
  newest.details.open = true;
  messages.push(newest);

  assert.equal(collapseConversation(timelineOf(messages), storage), 2);
  assert.deepEqual(
    messages.filter((item) => item.details).map((item) => item.details.open),
    [false, false, false, false, false]
  );
  // The opened older message and the newest are stored closed; the rest keep
  // the default (closed) without an entry, and other entries are untouched.
  assert.deepEqual(readDisclosureChoices(storage, key), {
    "other-project-round": true,
    "round-a-2": false,
    "round-a-1": false,
    "round-a-6": false,
  });

  // After a reload or swap everything stays closed; a new message still opens.
  const later = message("round-a-7");
  const again = [...messages, later];
  assert.equal(syncConversationDisclosure(timelineOf(again), readDisclosureChoices(storage, key)), true);
  assert.deepEqual(
    again.filter((item) => item.details).map((item) => item.details.open),
    [false, false, false, false, false, true]
  );
});

test("Collapse all with a full store keeps every message closed", () => {
  const key = MESSAGE_DISCLOSURE_KEY;
  const stored = {};
  for (let i = 0; i < DISCLOSURE_LIMIT; i += 1) {
    stored[`round-a-${i}`] = true;
  }
  const storage = memoryStorage({ [key]: JSON.stringify(stored) });
  const ids = Array.from({ length: DISCLOSURE_LIMIT + 50 }, (_, i) => `round-a-${i}`);
  const messages = openMessages(ids);

  assert.equal(collapseConversation(timelineOf(messages), storage), DISCLOSURE_LIMIT + 1);
  const choices = readDisclosureChoices(storage, key);
  assert.equal(Object.keys(choices).length, DISCLOSURE_LIMIT);
  assert.equal(choices[ids[ids.length - 1]], false);
  assert.ok(Object.values(choices).every((open) => open === false));

  const reloaded = ids.map((id) => message(id));
  syncConversationDisclosure(timelineOf(reloaded), choices);
  assert.ok(reloaded.every((item) => item.details.open === false));
});

test("Collapse all without storage still closes every message", () => {
  const messages = openMessages(["round-a-1", "round-a-2"]);
  assert.equal(collapseConversation(timelineOf(messages), null), 0);
  assert.deepEqual(messages.map((item) => item.details.open), [false, false]);
  assert.equal(collapseConversation(timelineOf([]), throwingStorage()), 0);
});

test("Collapse all shows only while the conversation has a message", () => {
  const button = { hidden: false };
  const timeline = (messages) => ({
    querySelector(selector) {
      assert.equal(selector, "[data-collapse-messages]");
      return button;
    },
    querySelectorAll: () => messages,
  });
  assert.equal(syncCollapseButton(timeline([])), false);
  assert.equal(button.hidden, true);
  assert.equal(syncCollapseButton(timeline([message("round-a-1")])), true);
  assert.equal(button.hidden, false);
  assert.equal(syncCollapseButton({ querySelector: () => null, querySelectorAll: () => [] }), false);
});
