"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { goToLatestMessage, syncLatestButton } = require("../../app/static/app.js");

function fakeDocument({ messages, viewTop = 100, viewBottom = 500, reduced = false, headingBottom = null }) {
  const scrolls = [];
  const heading =
    headingBottom === null ? null : { getBoundingClientRect: () => ({ bottom: headingBottom }) };
  const timeline = {
    scrollTop: 40,
    getBoundingClientRect: () => ({ top: viewTop, bottom: viewBottom }),
    querySelector: (selector) => {
      assert.equal(selector, ".timeline-heading");
      return heading;
    },
    querySelectorAll: (selector) => {
      assert.equal(selector, ".round, .live-round");
      return messages;
    },
    scrollTo: (options) => scrolls.push(options),
  };
  const button = { hidden: false };
  return {
    scrolls,
    button,
    timeline,
    defaultView: { matchMedia: () => ({ matches: reduced }) },
    querySelector: (selector) =>
      selector === "#chat-timeline" ? timeline : selector === "[data-timeline-latest]" ? button : null,
  };
}

function message(top, details = null) {
  return { getBoundingClientRect: () => ({ top }), querySelector: () => details };
}

test("shows the button only while the last message's start is out of view", function () {
  const below = fakeDocument({ messages: [message(120), message(900)] });
  assert.equal(syncLatestButton(below), true);
  assert.equal(below.button.hidden, false);
  const above = fakeDocument({ messages: [message(-800)] });
  assert.equal(syncLatestButton(above), true);
  const inView = fakeDocument({ messages: [message(120), message(300)] });
  assert.equal(syncLatestButton(inView), false);
  assert.equal(inView.button.hidden, true);
  const empty = fakeDocument({ messages: [] });
  assert.equal(syncLatestButton(empty), false);
  assert.equal(empty.button.hidden, true);
});

test("scrolls the last message's top to the panel top and opens it", function () {
  const details = { open: false };
  const page = fakeDocument({ messages: [message(120), message(900, details)] });
  assert.equal(goToLatestMessage(page), true);
  assert.equal(details.open, true);
  assert.deepEqual(page.scrolls, [{ top: 40 + 900 - 100, behavior: "smooth" }]);
  const reduced = fakeDocument({ messages: [message(900)], reduced: true });
  goToLatestMessage(reduced);
  assert.equal(reduced.scrolls[0].behavior, "auto");
  assert.equal(goToLatestMessage(fakeDocument({ messages: [] })), false);
});

test("the sticky Conversation heading covers the top of the panel", function () {
  // The last message starts under the heading: out of view, so the button shows.
  const covered = fakeDocument({ messages: [message(120)], headingBottom: 132 });
  assert.equal(syncLatestButton(covered), true);
  const below = fakeDocument({ messages: [message(140)], headingBottom: 132 });
  assert.equal(syncLatestButton(below), false);
  // Sub-pixel rounding after Latest: a fraction above the heading is in view.
  const rounded = fakeDocument({ messages: [message(131.7)], headingBottom: 132 });
  assert.equal(syncLatestButton(rounded), false);
  assert.equal(rounded.button.hidden, true);
  // A start within 1px of the panel's bottom shows nothing of the message.
  const atBottom = fakeDocument({ messages: [message(499.7)], headingBottom: 132 });
  assert.equal(syncLatestButton(atBottom), true);
  // Latest lands just below the heading, not under it.
  const page = fakeDocument({ messages: [message(900)], headingBottom: 132 });
  goToLatestMessage(page);
  assert.deepEqual(page.scrolls, [{ top: 40 + 900 - 132, behavior: "smooth" }]);
});
