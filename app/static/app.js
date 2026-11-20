"use strict";

function chatErrorMessage(status, responseText) {
  try {
    const payload = JSON.parse(responseText);
    if (typeof payload.detail === "string" && payload.detail) {
      return payload.detail;
    }
    if (Array.isArray(payload.detail)) {
      const messages = payload.detail
        .map(function (item) {
          return item && typeof item.msg === "string" ? item.msg : "";
        })
        .filter(Boolean);
      if (messages.length) {
        return messages.join("; ");
      }
    }
  } catch (_error) {
    // The stable fallback below deliberately excludes the untrusted response body.
  }
  return `Request failed (${status})`;
}

function renderChatError(region, status, responseText) {
  const message = chatErrorMessage(status, responseText);
  region.textContent = message;
  return message;
}

function formatOffset(minutesEast) {
  const sign = minutesEast < 0 ? "-" : "+";
  const total = Math.abs(minutesEast);
  const hours = String(Math.floor(total / 60)).padStart(2, "0");
  const minutes = total % 60;
  return sign + hours + (minutes ? ":" + String(minutes).padStart(2, "0") : "");
}

function localStampParts(iso) {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) {
    return null;
  }
  const pad = (value) => String(value).padStart(2, "0");
  return {
    day: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
    offset: formatOffset(-date.getTimezoneOffset()),
  };
}

function formatLocalStamp(iso) {
  const parts = localStampParts(iso);
  return parts ? `${parts.day} ${parts.time} ${parts.offset}` : null;
}

// One day and one offset are named once; a round that runs past the reader's
// midnight names both days. Without a readable finish, the start alone.
// app/timefmt.py writes the same shapes in UTC for the page before this runs.
function formatLocalRange(startIso, finishIso) {
  const start = localStampParts(startIso);
  if (!start) {
    return null;
  }
  const finish = localStampParts(finishIso);
  if (!finish) {
    return `${start.day} ${start.time} ${start.offset}`;
  }
  if (start.day === finish.day && start.offset === finish.offset) {
    return `${start.day} ${start.time} – ${finish.time} ${start.offset}`;
  }
  return `${start.day} ${start.time} ${start.offset} – ${finish.day} ${finish.time} ${finish.offset}`;
}

// The server writes UTC; the reader's clock is the browser's.
function localizeTimes(root) {
  if (!root || typeof root.querySelectorAll !== "function") {
    return 0;
  }
  let count = 0;
  root.querySelectorAll("[data-local-time]").forEach(function (element) {
    const text = formatLocalStamp(element.getAttribute("datetime"));
    if (text) {
      element.textContent = text;
      count += 1;
    }
  });
  root.querySelectorAll("[data-local-range]").forEach(function (element) {
    const text = formatLocalRange(element.dataset.start, element.dataset.finish);
    if (text) {
      element.textContent = text;
      count += 1;
    }
  });
  return count;
}

// One dialog edits any unread note; it lives outside the notes lists, which
// are replaced out-of-band, so a status update cannot wipe the typed text.
function openNoteEdit(button, doc) {
  const dialog = doc.getElementById("note-edit-dialog");
  const item = typeof button.closest === "function" ? button.closest("li") : null;
  const text = item ? item.querySelector(".auto-note-text") : null;
  const form = dialog ? dialog.querySelector("[data-note-edit-form]") : null;
  const field = form ? form.querySelector("textarea") : null;
  if (!dialog || !text || !form || !field || typeof dialog.showModal !== "function") {
    return false;
  }
  form.dataset.noteEditUrl = button.dataset.noteEditUrl;
  const label = dialog.querySelector("[data-note-edit-id]");
  if (label) {
    label.textContent = button.dataset.noteId || "";
  }
  field.value = text.textContent;
  if (!dialog.open) {
    dialog.showModal();
  }
  field.focus();
  return true;
}

// A refusal pops up and keeps what was typed (the UI error pop-up standard).
function finishNoteEdit(form, detail, alertFn) {
  if (!form || !detail) {
    return null;
  }
  const dialog = form.closest("dialog");
  if (detail.successful) {
    if (dialog && dialog.open) {
      dialog.close();
    }
    return "closed";
  }
  const xhr = detail.xhr || {};
  alertFn(chatErrorMessage(xhr.status || 0, xhr.responseText || ""));
  return "alerted";
}

// A refused Downloads or Conversions action pops up (the UI error pop-up
// standard). Whether a request is such an action is decided when it is sent:
// the list can re-render while it is in flight, and htmx's events on a
// detached button never reach the document.
function inLivePanel(source) {
  return Boolean(
    source &&
      typeof source.closest === "function" &&
      (source.closest("#downloads-panel") || source.closest("#conversions-panel"))
  );
}

function alertLiveListError(xhr, alertFn) {
  if (!xhr || !(xhr.status >= 400)) {
    return false;
  }
  alertFn(chatErrorMessage(xhr.status, xhr.responseText || ""));
  return true;
}

// The two live lists of the right column, by the stream event that refreshes each.
const LIVE_LISTS = { downloads: "downloads-list", conversions: "conversions-list" };

// The Downloads and Conversions lists are re-fetched on stream events and
// replaced by every action; responses can land out of order, so an older
// revision never replaces a newer one (the last refresh is queued, so it
// converges). Revisions only compare within one epoch: a recreated records
// file (or a restarted server's queue) starts again at 1.
function staleLiveList(target, responseText) {
  if (!target || !Object.values(LIVE_LISTS).includes(target.id) || !target.dataset) {
    return false;
  }
  const incoming = /data-revision="(\d+)"/.exec(responseText || "");
  const epoch = /data-epoch="([0-9a-f.]*)"/.exec(responseText || "");
  if (epoch && target.dataset.epoch !== undefined && epoch[1] !== target.dataset.epoch) {
    return false;
  }
  const shown = Number(target.dataset.revision);
  return Boolean(incoming) && Number.isFinite(shown) && Number(incoming[1]) < shown;
}

// Stream events carry the store revision: re-fetch only when it is newer
// than the list shown, so connecting (whose first event is the current
// revision) does not replace a list the user may be clicking in.
function newerLiveRevision(shown, data, shownEpoch) {
  let incoming = null;
  try {
    incoming = JSON.parse(data);
  } catch (_error) {
    return true;
  }
  if (!incoming || typeof incoming !== "object") {
    return true;
  }
  if (typeof incoming.epoch === "string" && shownEpoch !== undefined && incoming.epoch !== shownEpoch) {
    return true;
  }
  const revision = incoming.revision;
  return !Number.isFinite(shown) || typeof revision !== "number" || revision > shown;
}

// Like a live question: a request that starts waiting for approval (or a
// conversion that is queued or running) opens the collapsed panel; one the
// user closed stays closed otherwise.
const PANEL_COUNTS = {
  "downloads-list": ["awaiting", "#downloads-panel"],
  "conversions-list": ["active", "#conversions-panel"],
};

function openLivePanelForNew(list, responseText) {
  const counted = list && PANEL_COUNTS[list.id];
  if (!counted || !list.dataset) {
    return false;
  }
  const [name, panelSelector] = counted;
  const incoming = new RegExp(`data-${name}="(\\d+)"`).exec(responseText || "");
  const panel = typeof list.closest === "function" ? list.closest(panelSelector) : null;
  if (!incoming || !panel || panel.open || Number(incoming[1]) <= Number(list.dataset[name] || 0)) {
    return false;
  }
  panel.open = true;
  return true;
}

// `event` is the stream's message (htmx:sseBeforeMessage's detail); its type
// names the list it is about.
function liveListEventIsNewer(event) {
  const list = document.getElementById(LIVE_LISTS[event && event.type]);
  const shown = list && list.dataset ? Number(list.dataset.revision) : Number.NaN;
  const epoch = list && list.dataset ? list.dataset.epoch : undefined;
  return newerLiveRevision(shown, event && event.data, epoch);
}

// The running conversion's elapsed time ticks in the page. The server sends
// the seconds elapsed when it rendered, so the server's and the reader's
// clocks never need to agree; the first tick that sees a clock fixes its base.
function tickElapsedClocks(doc, now) {
  doc.querySelectorAll("[data-elapsed]").forEach(function (clock) {
    if (!clock.dataset.elapsedBase) {
      clock.dataset.elapsedBase = String(now - Number(clock.dataset.elapsed) * 1000);
    }
    clock.textContent = formatRemainingSeconds((now - Number(clock.dataset.elapsedBase)) / 1000);
  });
}

let focusDialogTrigger = null;

function restoreFocusDialogTrigger() {
  const trigger = focusDialogTrigger;
  focusDialogTrigger = null;
  if (
    trigger &&
    trigger.isConnected !== false &&
    typeof trigger.focus === "function"
  ) {
    trigger.focus();
  }
}

function openFocusDialog(dialog, trigger) {
  focusDialogTrigger = trigger || null;
  if (!dialog.open) {
    dialog.showModal();
  }
  const closeControl = dialog.querySelector("[data-focus-dialog-close]");
  if (closeControl) {
    closeControl.focus();
  }
}

const FOCUS_DIALOG_FIT_CLASSES = ["fit-width", "fit-window"];

// Pressing the active size again returns to the default size.
function setFocusDialogFit(dialog, mode) {
  const buttons = Array.from(dialog.querySelectorAll("[data-focus-dialog-fit]"));
  const known = buttons.some(function (button) {
    return button.dataset.focusDialogFit === mode;
  });
  const next = known && !dialog.classList.contains("fit-" + mode) ? mode : null;
  dialog.classList.remove(...FOCUS_DIALOG_FIT_CLASSES);
  if (next) {
    dialog.classList.add("fit-" + next);
  }
  buttons.forEach(function (button) {
    button.setAttribute(
      "aria-pressed",
      String(next !== null && button.dataset.focusDialogFit === next)
    );
  });
}

function closeFocusDialog(dialog) {
  if (dialog.open) {
    dialog.close();
  }
  restoreFocusDialogTrigger();
}

// Every way of closing fires "close"; each open starts at the default size.
function handleFocusDialogClosed(event) {
  setFocusDialogFit(event.currentTarget, null);
  restoreFocusDialogTrigger();
}

function isDialogBackdropClick(dialog, event) {
  return event.target === dialog;
}

function handleFocusDialogCancel(event) {
  event.preventDefault();
  closeFocusDialog(event.currentTarget);
}

function shouldClearChatError(requestPath) {
  const pathname = String(requestPath || "").split(/[?#]/, 1)[0];
  return !/^\/projects\/[^/]+\/files(?:\/(?:view|focus))?\/?$/.test(pathname);
}

function syncEffortOptions(form) {
  const agentSelect = form.querySelector("[data-agent-select]");
  const effortSelect = form.querySelector("[data-effort-select]");
  if (!effortSelect) {
    return;
  }
  const agent = agentSelect ? agentSelect.value : form.dataset.agent;
  const groups = effortSelect.querySelectorAll("[data-effort-agent]");
  groups.forEach(function (group) {
    group.disabled = group.dataset.effortAgent !== agent;
  });
  const selected = effortSelect.selectedOptions[0];
  if (!selected || selected.parentElement.disabled) {
    const enabled = effortSelect.querySelector(
      `[data-effort-agent="${agent}"] option`
    );
    if (enabled) {
      enabled.selected = true;
    }
  }
}

// New-agent forms carry each agent type's default model and effort
// (Settings → Agents). Switching the type fills them in, except a field the
// user already edited in this form. An empty model means the CLI default.
function applyAgentDefaults(form, agent) {
  let defaults;
  try {
    defaults = JSON.parse(form.dataset.agentDefaults || "")[agent];
  } catch (_error) {
    return false;
  }
  if (!defaults || typeof defaults !== "object") {
    return false;
  }
  const model = form.querySelector('input[name="model"]');
  if (model && model.dataset.edited !== "true") {
    model.value = String(defaults.model || "");
  }
  const effort = form.querySelector("[data-effort-select]");
  if (effort && effort.dataset.edited !== "true") {
    const option = effort.querySelector(
      `[data-effort-agent="${agent}"] option[value="${defaults.effort}"]`
    );
    if (option) {
      option.selected = true;
    }
  }
  return true;
}

function removeConversationEmptyState(timeline) {
  const empty = timeline.querySelector("[data-conversation-empty]");
  const message = timeline.querySelector(".round, .live-round");
  if (!empty || !message) return false;
  empty.remove();
  return true;
}

function resetFileReader(reader) {
  reader.replaceChildren();
  const empty = reader.ownerDocument.createElement("p");
  empty.className = "file-reader-empty";
  empty.textContent = "Select a text file to read it here.";
  reader.appendChild(empty);
  if (typeof reader.focus === "function") {
    reader.focus({ preventScroll: true });
  }
  return empty;
}

// The file reader and Focus swap a whole file view in. A link inside a long
// file would otherwise open the next file at the old file's scroll offset.
function fileViewPath(container) {
  const heading =
    container && typeof container.querySelector === "function"
      ? container.querySelector(".file-view-header h3")
      : null;
  return heading ? heading.textContent.trim() : null;
}

function resetScrollForNewFile(container, previousPath) {
  if (fileViewPath(container) === previousPath) {
    return false;
  }
  container.scrollTop = 0;
  return true;
}

function conversationTimelineForSwap(detailTarget, swappedElement) {
  if (
    swappedElement &&
    swappedElement.classList &&
    swappedElement.classList.contains("chat-timeline")
  ) {
    return swappedElement;
  }
  if (
    detailTarget.classList &&
    detailTarget.classList.contains("chat-timeline")
  ) {
    return detailTarget;
  }
  if (!swappedElement || typeof swappedElement.closest !== "function") {
    return null;
  }
  return swappedElement.closest(".chat-timeline");
}

function captureConversationScroll(timeline) {
  const timelineTop = timeline.getBoundingClientRect().top;
  const messages = Array.from(
    timeline.querySelectorAll(".round, .live-round")
  );
  const anchor = messages.find(function (message) {
    return message.getBoundingClientRect().bottom > timelineTop;
  });
  return {
    scrollTop: timeline.scrollTop,
    anchorId: anchor && anchor.id ? anchor.id : null,
    anchorOffset: anchor
      ? anchor.getBoundingClientRect().top - timelineTop
      : null,
  };
}

function restoreConversationScroll(timeline, snapshot) {
  if (!snapshot) {
    return false;
  }
  const documentRoot = timeline.ownerDocument;
  const anchor =
    snapshot.anchorId && documentRoot
      ? documentRoot.getElementById(snapshot.anchorId)
      : null;
  if (anchor && timeline.contains(anchor)) {
    const timelineTop = timeline.getBoundingClientRect().top;
    const anchorOffset = anchor.getBoundingClientRect().top - timelineTop;
    timeline.scrollTop += anchorOffset - snapshot.anchorOffset;
    return true;
  }
  timeline.scrollTop = snapshot.scrollTop;
  return false;
}

// The reader's own open/closed choices outlive swaps and reloads
// (user, 2026-11-13): id → boolean, oldest first, capped.
const AGENT_DISCLOSURE_KEY = "delibra.agentDisclosure";
const MESSAGE_DISCLOSURE_KEY = "delibra.messageDisclosure";
const DISCLOSURE_LIMIT = 200;

// Reading window.localStorage itself throws when storage is blocked.
function browserStorage(win) {
  try {
    return win ? win.localStorage || null : null;
  } catch (_error) {
    return null;
  }
}

function readDisclosureChoices(storage, key) {
  let parsed = null;
  try {
    parsed = storage ? JSON.parse(storage.getItem(key)) : null;
  } catch (_error) {
    return {};
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {};
  }
  const choices = {};
  Object.keys(parsed).forEach(function (id) {
    if (typeof parsed[id] === "boolean") {
      choices[id] = parsed[id];
    }
  });
  return choices;
}

function rememberDisclosureChoice(storage, key, id, open) {
  return rememberDisclosureChoices(storage, key, [[id, open]]);
}

// [id, open] pairs in one write, in order: the last ones are kept longest.
function rememberDisclosureChoices(storage, key, entries) {
  if (!storage) {
    return false;
  }
  const choices = readDisclosureChoices(storage, key);
  entries.forEach(function (entry) {
    delete choices[entry[0]];
    choices[entry[0]] = Boolean(entry[1]);
  });
  const ids = Object.keys(choices);
  ids.slice(0, Math.max(0, ids.length - DISCLOSURE_LIMIT)).forEach(function (old) {
    delete choices[old];
  });
  try {
    storage.setItem(key, JSON.stringify(choices));
    return true;
  } catch (_error) {
    return false;
  }
}

// A message is open as the reader left it, else only when it is the newest.
function syncConversationDisclosure(timeline, choices = {}) {
  const messages = Array.from(
    timeline.querySelectorAll(".round, .live-round")
  );
  let newestOpen = false;
  messages.forEach(function (message, index) {
    const details = message.querySelector(".round-details");
    if (!details) {
      return;
    }
    const newest = index === messages.length - 1;
    const chosen = Object.prototype.hasOwnProperty.call(choices, message.id);
    details.open = chosen ? choices[message.id] : newest;
    newestOpen = newest && details.open;
  });
  return newestOpen;
}

// Collapse all (user, 2026-11-20): every message closes. Only the messages
// that would open again are stored closed: the newest (open by default) and
// any the reader had opened. The others are closed by default already, so
// they take no room under DISCLOSURE_LIMIT, and a message that arrives later
// still opens as the newest. Returns how many choices were stored.
function collapseConversation(timeline, storage) {
  const messages = Array.from(
    timeline.querySelectorAll(".round, .live-round")
  );
  const choices = readDisclosureChoices(storage, MESSAGE_DISCLOSURE_KEY);
  const closed = [];
  messages.forEach(function (message, index) {
    const details = message.querySelector(".round-details");
    if (!details) {
      return;
    }
    details.open = false;
    const newest = index === messages.length - 1;
    if (message.id && (newest || choices[message.id] === true)) {
      closed.push([message.id, false]);
    }
  });
  if (!closed.length) {
    return 0;
  }
  return rememberDisclosureChoices(storage, MESSAGE_DISCLOSURE_KEY, closed)
    ? closed.length
    : 0;
}

// Collapse all shows only while there is a message to close; the first one
// arrives without the heading being rendered again.
function syncCollapseButton(timeline) {
  const button = timeline.querySelector("[data-collapse-messages]");
  if (!button) {
    return false;
  }
  button.hidden = !latestMessage(timeline);
  return !button.hidden;
}

// Agent information is closed unless the reader opened it.
function syncAgentDisclosure(root, choices) {
  let opened = 0;
  Array.from(root.querySelectorAll(".agent-card[data-session-id]")).forEach(function (card) {
    const details = card.querySelector(".agent-details");
    if (!details) {
      return;
    }
    details.open = choices[card.dataset.sessionId] === true;
    opened += details.open ? 1 : 0;
  });
  return opened;
}

// The conversation runs oldest → newest, so the last in DOM order is newest.
function latestMessage(timeline) {
  if (!timeline || typeof timeline.querySelectorAll !== "function") {
    return null;
  }
  const messages = timeline.querySelectorAll(".round, .live-round");
  return messages.length ? messages[messages.length - 1] : null;
}

// "↓ Latest" shows only while the start of the last message is outside the
// panel. New rounds never move the reader; this is the way down.
function syncLatestButton(documentRoot) {
  if (!documentRoot || typeof documentRoot.querySelector !== "function") {
    return false;
  }
  const button = documentRoot.querySelector("[data-timeline-latest]");
  const timeline = documentRoot.querySelector("#chat-timeline");
  if (!button) {
    return false;
  }
  const latest = latestMessage(timeline);
  if (!latest) {
    button.hidden = true;
    return false;
  }
  const view = timeline.getBoundingClientRect();
  const top = latest.getBoundingClientRect().top;
  const away = top < view.top || top >= view.bottom;
  button.hidden = !away;
  return away;
}

function goToLatestMessage(documentRoot) {
  const timeline = documentRoot.querySelector("#chat-timeline");
  const latest = latestMessage(timeline);
  if (!latest) {
    return false;
  }
  const details = latest.querySelector(".round-details");
  if (details) {
    details.open = true;
  }
  const view = documentRoot.defaultView;
  const reduced = Boolean(
    view && view.matchMedia && view.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
  timeline.scrollTo({
    top:
      timeline.scrollTop +
      latest.getBoundingClientRect().top -
      timeline.getBoundingClientRect().top,
    behavior: reduced ? "auto" : "smooth",
  });
  return true;
}

// Every sse:status replaces #auto-status, and a fresh stream replays the run's
// events, so an opened Stop disclosure would otherwise snap shut under the
// reader before they can choose.
function captureAutoStopDisclosure(target) {
  if (!target || target.id !== "auto-status") {
    return null;
  }
  const details = target.querySelector("details.auto-stop");
  return {
    autoId: target.dataset.autoId,
    open: Boolean(details && details.open),
  };
}

function restoreAutoStopDisclosure(documentRoot, snapshot) {
  if (!snapshot || !snapshot.open) {
    return false;
  }
  const status = documentRoot.getElementById("auto-status");
  if (!status || status.dataset.autoId !== snapshot.autoId) {
    return false;
  }
  const details = status.querySelector("details.auto-stop");
  if (!details) {
    return false;
  }
  details.open = true;
  return true;
}

function autoExtendPreviewText(base, value, max) {
  const text = String(value).trim();
  const added = /^\d+$/.test(text) ? Number(text) : NaN;
  if (!(added >= 1 && added <= Number(max))) {
    return `Enter 1 to ${max} cycles`;
  }
  return `New limit: ${Number(base) + added} cycles`;
}

function updateAutoExtendPreview(container) {
  const input = container.querySelector("[data-auto-extend-input]");
  const preview = container.querySelector("[data-auto-extend-preview]");
  if (!input || !preview) {
    return;
  }
  preview.textContent = autoExtendPreviewText(
    input.dataset.autoExtendBase,
    input.value,
    input.max
  );
}

// A status refresh replaces the Add cycles form; keep it open with what was
// typed. Its own submit's response is the exception: that closes it.
function captureAutoExtendForm(target, source) {
  if (!target || target.id !== "auto-status") {
    return null;
  }
  const ownSubmit = Boolean(
    source && typeof source.closest === "function" && source.closest("[data-auto-extend-form]")
  );
  const details = target.querySelector("details.auto-extend");
  const input = target.querySelector("[data-auto-extend-input]");
  return {
    autoId: target.dataset.autoId,
    open: !ownSubmit && Boolean(details && details.open),
    value: !ownSubmit && input ? input.value : "",
  };
}

function restoreAutoExtendForm(documentRoot, snapshot) {
  if (!snapshot || !snapshot.open) {
    return false;
  }
  const status = documentRoot.getElementById("auto-status");
  if (!status || status.dataset.autoId !== snapshot.autoId) {
    return false;
  }
  const details = status.querySelector("details.auto-extend");
  if (!details) {
    return false;
  }
  details.open = true;
  const input = status.querySelector("[data-auto-extend-input]");
  if (input) {
    input.value = snapshot.value;
  }
  updateAutoExtendPreview(status);
  return true;
}

function formatRemainingSeconds(value) {
  const parsed = Number(value);
  const bounded = Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0;
  const hours = Math.floor(bounded / 3600);
  const minutes = Math.floor((bounded % 3600) / 60);
  const seconds = bounded % 60;
  const minuteText = hours ? String(minutes).padStart(2, "0") : String(minutes);
  const secondText = String(seconds).padStart(2, "0");
  return hours
    ? `${hours}:${minuteText}:${secondText}`
    : `${minuteText}:${secondText}`;
}

function updateTimeoutCountdown(controls, nowMilliseconds) {
  const remainingNode = controls.querySelector("[data-timeout-remaining]");
  if (!remainingNode) {
    return 0;
  }
  const now = Number.isFinite(nowMilliseconds) ? nowMilliseconds : Date.now();
  const supplied = Number(controls.dataset.timeoutSeconds);
  const duration = Number.isFinite(supplied) ? Math.max(0, Math.floor(supplied)) : 0;
  let startedAt = Number(controls.dataset.timeoutStartedAt);
  if (!Number.isFinite(startedAt) || controls.dataset.timeoutStartedAt === "") {
    startedAt = now;
    controls.dataset.timeoutStartedAt = String(now);
  }
  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000));
  const remaining = Math.max(0, duration - elapsed);
  remainingNode.textContent = formatRemainingSeconds(remaining);
  return remaining;
}

function copyAutoComposerTopic(setup) {
  if (!setup || !setup.dataset) {
    return false;
  }
  const topic = setup.querySelector('textarea[name="topic"]');
  const composer = setup.ownerDocument.querySelector(
    '#chat-composer textarea[name="prompt"]'
  );
  if (!topic || !composer) {
    return false;
  }
  // Unsent text is what the user is about to discuss, so it wins over the
  // durable fallback; an empty composer leaves that fallback alone.
  if (setup.dataset.topicSource !== "composer" && !composer.value.trim()) {
    return false;
  }
  topic.value = composer.value;
  return true;
}

function autoSetupPlacement(anchor, width, viewportWidth, viewportHeight) {
  // Top-left at the Message box, kept inside the viewport: never above the
  // top edge, never starting below half the height, never past the right.
  const margin = 8;
  const top = Math.min(Math.max(anchor.top, margin), Math.max(margin, viewportHeight / 2));
  const left = Math.max(margin, Math.min(anchor.left, viewportWidth - width - margin));
  return { top: top, left: left, maxHeight: viewportHeight - top - margin };
}

function openAutoSetup(dialog) {
  // A modal sits in the top layer with a backdrop; `open` in the markup made
  // it a page-flow box that ran off the bottom of long pages.
  if (!dialog || dialog.open || typeof dialog.showModal !== "function") {
    return false;
  }
  dialog.addEventListener("close", function () {
    dialog.remove();
  });
  dialog.showModal();
  const doc = dialog.ownerDocument;
  const view = doc && doc.defaultView;
  const composer = doc && doc.getElementById("chat-composer");
  if (view && composer) {
    // Set before the next paint, so the dialog never shows centred first.
    const place = autoSetupPlacement(
      composer.getBoundingClientRect(),
      dialog.getBoundingClientRect().width,
      view.innerWidth,
      view.innerHeight
    );
    dialog.style.setProperty("--auto-setup-top", place.top + "px");
    dialog.style.setProperty("--auto-setup-left", place.left + "px");
    dialog.style.setProperty("--auto-setup-max-height", place.maxHeight + "px");
  }
  return true;
}

function closeAutoSetup(control) {
  if (!control || typeof control.closest !== "function") {
    return false;
  }
  const dialog = control.closest("[data-auto-setup-dialog]");
  if (!dialog) {
    return false;
  }
  if (typeof dialog.close === "function") {
    dialog.close();
  }
  if (typeof dialog.remove === "function") {
    dialog.remove();
  }
  return true;
}

function syncAutoOrderControls(list) {
  const items = Array.from(list.children);
  items.forEach(function (item, index) {
    const up = item.querySelector('[data-auto-order-move="up"]');
    const down = item.querySelector('[data-auto-order-move="down"]');
    if (up) {
      up.disabled = index === 0;
    }
    if (down) {
      down.disabled = index === items.length - 1;
    }
  });
}

function moveAutoOrderItem(control) {
  if (!control || typeof control.closest !== "function") {
    return false;
  }
  const item = control.closest("[data-auto-order-item]");
  const list = item ? item.parentElement : null;
  if (!list) {
    return false;
  }
  const direction = control.dataset.autoOrderMove;
  // Move the neighbour, not the item: detaching the focused row blurs it.
  if (direction === "up" && item.previousElementSibling) {
    list.insertBefore(item.previousElementSibling, item.nextElementSibling);
  } else if (direction === "down" && item.nextElementSibling) {
    list.insertBefore(item.nextElementSibling, item);
  } else {
    return false;
  }
  syncAutoOrderControls(list);
  const opposite = direction === "up" ? "down" : "up";
  const target = control.disabled
    ? item.querySelector(`[data-auto-order-move="${opposite}"]`)
    : control;
  if (target && typeof target.focus === "function") {
    target.focus();
  }
  return true;
}

function focusAutoHistoryView(target) {
  if (
    !target ||
    target.id !== "auto-history-view" ||
    typeof target.focus !== "function"
  ) {
    return false;
  }
  target.focus();
  return true;
}

function syncAutoDisabledControls(documentRoot) {
  if (!documentRoot || typeof documentRoot.querySelector !== "function") {
    return false;
  }
  // The chat page has #auto-status; settings pages poll #settings-lock.
  const status = documentRoot.querySelector("#auto-status, #settings-lock");
  const active = Boolean(status && status.dataset.autoActive === "true");
  documentRoot.querySelectorAll("[data-disable-during-auto]").forEach(function (control) {
    if (active) {
      if (!control.disabled) {
        control.disabled = true;
        control.dataset.autoDisabled = "true";
      }
    } else if (control.dataset.autoDisabled === "true") {
      control.disabled = false;
      delete control.dataset.autoDisabled;
    }
  });
  return active;
}

function syncComposerAvailability(documentRoot) {
  if (!documentRoot || typeof documentRoot.querySelector !== "function") {
    return false;
  }
  const composer = documentRoot.querySelector("#chat-composer");
  if (!composer) {
    return false;
  }
  const autoStatus = documentRoot.querySelector("#auto-status");
  const autoActive = Boolean(
    autoStatus && autoStatus.dataset.autoActive === "true"
  );
  const sessionId = composer.dataset.sessionId || "";
  // The card is the only live record of whether that agent is mid-turn. It is
  // refreshed at both ends of a run, so it is never behind the server for
  // longer than one swap.
  const card = sessionId
    ? documentRoot.querySelector(`#agent-status-${sessionId}`)
    : null;
  const agentBusy = Boolean(card && card.dataset.agentStatus === "running");
  const blocked = autoActive || !sessionId || agentBusy;
  composer.querySelectorAll("[data-composer-send]").forEach(function (control) {
    control.disabled = blocked;
  });
  return blocked;
}

// Where a note goes, from what the page shows. A replying chat agent comes
// first (user decision 2026-11-02); otherwise an Auto run that accepts notes.
// The form sends the answer, so the server never reroutes a note. "Replying"
// comes from the agent card: a compaction runs but is not a reply.
function notesTarget(documentRoot) {
  const status = documentRoot.querySelector("#auto-status");
  const autoActive = Boolean(status && status.dataset.autoActive === "true");
  const autoOpen = Boolean(status && status.dataset.autoNotesOpen === "true");
  const composer = documentRoot.querySelector("#chat-composer");
  const sessionId = composer && composer.dataset ? composer.dataset.sessionId || "" : "";
  const card = sessionId ? documentRoot.querySelector(`#agent-status-${sessionId}`) : null;
  if (!autoActive && card && card.dataset.agentReplying === "true") {
    return "chat";
  }
  return autoOpen ? "auto" : "closed";
}

function syncAutoNotesAvailability(documentRoot) {
  if (!documentRoot || typeof documentRoot.querySelector !== "function") {
    return false;
  }
  const target = notesTarget(documentRoot);
  // Closed still lists the selected agent's notes, so they can be read and deleted.
  const mode = target === "auto" ? "auto" : "chat";
  documentRoot.querySelectorAll("[data-auto-notes-input]").forEach(function (control) {
    control.disabled = target === "closed";
  });
  const field = documentRoot.querySelector("[data-notes-target]");
  if (field) {
    field.value = mode;
  }
  documentRoot.querySelectorAll("[data-notes-mode]").forEach(function (element) {
    element.hidden = element.dataset.notesMode !== mode;
  });
  const textarea = documentRoot.querySelector("#auto-note-text");
  if (textarea) {
    textarea.setAttribute("aria-describedby", `${mode}-note-hint`);
  }
  syncNotesDisclosure(documentRoot);
  return target !== "closed";
}

const NOTES_DISCLOSURE_KEY = "delibra.notesOpen";

function notesSummaryText(total, pending) {
  if (!total) {
    return "";
  }
  return pending ? ` · ${total} (${pending} pending)` : ` · ${total}`;
}

// Counted from the list on screen: every list swap and mode change runs this,
// so the summary needs no request of its own.
function syncNotesDisclosure(documentRoot) {
  const details =
    documentRoot && typeof documentRoot.querySelector === "function"
      ? documentRoot.querySelector("[data-notes-disclosure]")
      : null;
  if (!details) {
    return null;
  }
  const shown = Array.from(details.querySelectorAll(".auto-notes-list")).find(function (list) {
    return !list.hidden;
  });
  const total = shown ? shown.querySelectorAll("li.auto-note").length : 0;
  const pending = shown ? shown.querySelectorAll("li.auto-note.pending").length : 0;
  const count = details.querySelector("[data-notes-summary-count]");
  if (count) {
    count.textContent = notesSummaryText(total, pending);
  }
  details.hidden = total === 0;
  return { total, pending };
}

// Storage can throw (blocked or private browsing); the rendered state stands.
function rememberNotesDisclosure(details, storage) {
  try {
    storage.setItem(NOTES_DISCLOSURE_KEY, String(Boolean(details.open)));
    return true;
  } catch (_error) {
    return false;
  }
}

function restoreNotesDisclosure(details, storage) {
  let stored = null;
  try {
    stored = storage.getItem(NOTES_DISCLOSURE_KEY);
  } catch (_error) {
    return false;
  }
  if (stored !== "true" && stored !== "false") {
    return false;
  }
  details.open = stored === "true";
  return true;
}

function resetAutoNotesForm(source, detail) {
  const form =
    source && typeof source.closest === "function"
      ? source.closest("[data-auto-notes-form]")
      : null;
  if (!form || !detail || !detail.successful) {
    return false;
  }
  form.reset();
  return true;
}

// Every notes list arrives out-of-band. An older list must not replace a newer
// one, and a chat list belongs on the page only while its agent is selected.
function skipStaleNotesSwap(detail) {
  const id = detail && detail.target ? detail.target.id : "";
  if (!detail || !detail.fragment || (id !== "auto-notes-list" && id !== "chat-notes-list")) {
    return false;
  }
  const element = detail.fragment.dataset ? detail.fragment : detail.fragment.firstElementChild;
  const incoming = element && element.dataset ? element.dataset : {};
  const shown = detail.target.dataset || {};
  if (id === "chat-notes-list") {
    const root = detail.target.ownerDocument;
    const composer = root && root.querySelector ? root.querySelector("#chat-composer") : null;
    const selected = composer && composer.dataset ? composer.dataset.sessionId || "" : "";
    if ((incoming.sessionId || "") !== selected) {
      detail.shouldSwap = false;
      return true;
    }
    if ((shown.sessionId || "") !== incoming.sessionId) {
      return false;
    }
  }
  // Number("") is 0, so an absent attribute must stay NaN, not become 0.
  const revision = (value) => (value ? Number(value) : NaN);
  const next = revision(incoming.notesRevision);
  const current = revision(shown.notesRevision);
  if (!Number.isFinite(next) || !Number.isFinite(current) || next >= current) {
    return false;
  }
  detail.shouldSwap = false;
  return true;
}

// Picking, creating or editing an agent re-renders the whole composer
// out-of-band; the unsent message moves into the new box instead of being lost.
function keepComposerText(detail) {
  if (!detail || !detail.target || detail.target.id !== "chat-composer" || !detail.fragment) {
    return false;
  }
  const current = detail.target.querySelector('textarea[name="prompt"]');
  const element = detail.fragment.dataset
    ? detail.fragment
    : detail.fragment.firstElementChild;
  const next = element && element.querySelector('textarea[name="prompt"]');
  if (!current || !next || !current.value) {
    return false;
  }
  next.value = current.value;
  return true;
}

function setTimeoutFormPending(form, pending) {
  if (!form || typeof form.querySelectorAll !== "function") {
    return false;
  }
  form.querySelectorAll("button, input").forEach(function (control) {
    if (pending && !control.disabled) {
      control.disabled = true;
      control.dataset.timeoutPendingDisabled = "true";
    } else if (!pending && control.dataset.timeoutPendingDisabled === "true") {
      control.disabled = false;
      delete control.dataset.timeoutPendingDisabled;
    }
  });
  return Boolean(pending);
}

function timeoutControlsWithin(root) {
  const controls = [];
  if (root && typeof root.matches === "function" && root.matches("[data-timeout-controls]")) {
    controls.push(root);
  }
  if (root && typeof root.querySelectorAll === "function") {
    controls.push(...root.querySelectorAll("[data-timeout-controls]"));
  }
  return controls;
}

function initializeTimeoutCountdown(controls) {
  const tick = function () {
    const remaining = updateTimeoutCountdown(controls);
    if (remaining <= 0 || controls.isConnected === false) {
      clearInterval(intervalId);
    }
  };
  updateTimeoutCountdown(controls);
  if (controls.dataset.timeoutActive !== "true") {
    return;
  }
  const intervalId = setInterval(tick, 1000);
}

function openQuestionsPanelForNew(list, responseText) {
  // A live question can land while the panel is collapsed (it starts that
  // way when nothing is open); open it so the question is seen within
  // seconds, not only counted in the summary.
  if (!list || list.id !== "questions-list" || !/<li id="question-Q\d+"/.test(responseText || "")) {
    return false;
  }
  const panel = list.closest("#questions-panel");
  if (!panel || panel.open) {
    return false;
  }
  panel.open = true;
  return true;
}

function lastPathLine(value) {
  const lines = String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.length ? lines[lines.length - 1] : "";
}

function appendPickedPaths(field, values) {
  const present = String(field.value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const added = values.filter(
    (value, index) => value && !present.includes(value) && values.indexOf(value) === index
  );
  if (!added.length) {
    return 0;
  }
  field.value = present.concat(added).join("\n");
  return added.length;
}

function pickerUrl(url, field) {
  const separator = url.indexOf("?") === -1 ? "?" : "&";
  return url + separator + "start=" + encodeURIComponent(lastPathLine(field.value));
}

const PICKER_SESSION_HEADER = "X-Delibra-Picker";
const PICKER_FILES_URL = "/settings/preset-files";
const PICKER_REGIONS = ["path-picker-body", "picker-files"];
const PICKER_LOADING_HTML = '<p class="hint">Loading…</p>';
const PICKER_CANCEL_HTML =
  '<div class="path-picker-actions"><button type="button" data-path-picker-close>Cancel</button></div>';

// One dialog serves every Browse… button. Each opening is a new session: the
// last opening's controls are gone before anything new is asked for, and
// isStalePickerSwap drops any answer for an earlier opening.
function openPathPicker(button, doc, htmxApi) {
  const dialog = doc.getElementById("path-picker");
  const body = doc.getElementById("path-picker-body");
  const files = doc.getElementById("picker-files");
  const field = doc.getElementById(button.dataset.pickerTarget || "");
  if (!dialog || !body || !files || !field || !htmxApi || typeof dialog.showModal !== "function") {
    return false;
  }
  const session = String(Number(dialog.dataset.pickerSession || "0") + 1);
  const headers = { [PICKER_SESSION_HEADER]: session };
  // A preset's fixed file list is edited below the listing in this dialog.
  const list = button.dataset.pickerFilesList || "";
  // Which of a preset's lists: "files" (pasted every turn) or "skills".
  const listName = button.dataset.pickerList || "files";
  dialog.dataset.pickerSession = session;
  dialog.dataset.pickerTarget = button.dataset.pickerTarget;
  dialog.dataset.pickerList = list;
  dialog.dataset.pickerListName = listName;
  // Clicks inside the dialog inherit this, so they carry the session too.
  dialog.setAttribute("hx-headers", JSON.stringify(headers));
  body.innerHTML = PICKER_LOADING_HTML + (list ? "" : PICKER_CANCEL_HTML);
  files.innerHTML = list ? PICKER_LOADING_HTML + PICKER_CANCEL_HTML : "";
  files.hidden = !list;
  if (!dialog.open) {
    dialog.showModal();
  }
  // From the dialog, whose hx-sync aborts the previous opening's listing.
  htmxApi.ajax("GET", pickerUrl(button.dataset.pickerUrl, field), {
    source: dialog,
    target: "#path-picker-body",
    swap: "outerHTML",
    headers: headers,
  });
  if (list) {
    htmxApi.ajax("POST", PICKER_FILES_URL, {
      source: files,
      target: "#picker-files",
      swap: "innerHTML",
      values: { view: "dialog", files: field.value, list: listName },
      headers: headers,
    });
  }
  return true;
}

function isStalePickerSwap(detail, doc) {
  if (!detail.target || !PICKER_REGIONS.includes(detail.target.id)) {
    return false;
  }
  const dialog = doc.getElementById("path-picker");
  const headers = (detail.requestConfig && detail.requestConfig.headers) || {};
  return (
    !dialog || !dialog.open || headers[PICKER_SESSION_HEADER] !== dialog.dataset.pickerSession
  );
}

function addPickedFiles(dialog, doc, htmxApi) {
  const body = doc.getElementById("path-picker-body");
  const files = doc.getElementById("picker-files");
  const current = doc.getElementById("picker-files-value");
  if (!body || !files || !current || !htmxApi) {
    return;
  }
  const ticked = Array.from(body.querySelectorAll("input[data-picker-value]:checked"));
  if (!ticked.length) {
    return;
  }
  const subdirs = body.querySelector("input[data-picker-subdirs]");
  htmxApi.ajax("POST", PICKER_FILES_URL, {
    source: files,
    target: "#picker-files",
    swap: "innerHTML",
    values: {
      view: "dialog",
      files: current.value,
      add: ticked.map((input) => input.dataset.pickerValue).join("\n"),
      subdirs: subdirs && subdirs.checked ? "true" : "false",
      list: dialog.dataset.pickerListName || "files",
    },
    headers: { [PICKER_SESSION_HEADER]: dialog.dataset.pickerSession },
  });
  ticked.forEach(function (input) {
    input.checked = false;
  });
}

// Fills the field the dialog was opened for; the form's own button saves.
function applyPathPicker(control, doc, htmxApi) {
  const dialog = doc.getElementById("path-picker");
  if (!dialog) {
    return false;
  }
  if (control.matches("[data-picker-add-files]")) {
    addPickedFiles(dialog, doc, htmxApi);
    return false;
  }
  const field = doc.getElementById(dialog.dataset.pickerTarget || "");
  let changed = false;
  if (field && control.matches("[data-path-picker-choose]")) {
    changed = field.value !== control.dataset.pickerValue;
    field.value = control.dataset.pickerValue;
  } else if (field && control.matches("[data-path-picker-add]")) {
    const body = doc.getElementById("path-picker-body");
    const picked = body
      ? Array.from(body.querySelectorAll("input[data-picker-value]:checked")).map(
          (input) => input.dataset.pickerValue
        )
      : [];
    changed = appendPickedPaths(field, picked) > 0;
  } else if (field && control.matches("[data-path-picker-use]")) {
    const chosen = doc.getElementById("picker-files-value");
    const list = doc.getElementById(dialog.dataset.pickerList || "");
    if (chosen) {
      changed = field.value !== chosen.value;
      field.value = chosen.value;
      if (list && htmxApi) {
        // Sent from the dialog, which sits outside the settings form, so only
        // these values are posted.
        htmxApi.ajax("POST", PICKER_FILES_URL, {
          source: dialog,
          target: list,
          swap: "innerHTML",
          values: {
            view: "row",
            files: field.value,
            list: dialog.dataset.pickerListName || "files",
          },
        });
      }
    }
  }
  if (changed) {
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (dialog.open) {
    dialog.close();
  }
  return changed;
}

// A page answered to a refused form names the reason in [data-page-alert];
// it pops up once as a browser alert, the same kind of dialog as the
// confirm before Start new phase.
function showPageAlert(doc, alertFn) {
  const marker = doc.querySelector("[data-page-alert]");
  if (!marker) {
    return null;
  }
  const message = marker.textContent.trim();
  marker.removeAttribute("data-page-alert");
  alertFn(message);
  return message;
}

function initializeDynamicPresentation(root) {
  timeoutControlsWithin(root).forEach(initializeTimeoutCountdown);
  if (root && typeof root.matches === "function" && root.matches("[data-auto-setup-dialog]")) {
    copyAutoComposerTopic(root);
  }
  if (root && typeof root.querySelectorAll === "function") {
    root.querySelectorAll("[data-auto-setup-dialog]").forEach(copyAutoComposerTopic);
  }
  if (root && typeof root.matches === "function" && root.matches("[data-auto-setup-dialog]")) {
    openAutoSetup(root);
  }
  if (root && typeof root.querySelectorAll === "function") {
    root.querySelectorAll("[data-auto-setup-dialog]").forEach(openAutoSetup);
  }
  const documentRoot = root && root.ownerDocument ? root.ownerDocument : root;
  syncAutoDisabledControls(documentRoot);
  // Runs second and unconditionally: it owns [data-composer-send] outright,
  // so it is not fighting the marker bookkeeping above.
  syncComposerAvailability(documentRoot);
  syncAutoNotesAvailability(documentRoot);
}

// htmx's history snapshot keeps the controls' markup, not the values typed or
// picked since, so after Back they would disagree with the restored list.
// The URL holds the restored view. An unknown sort means the server's default,
// which is listed first.
function syncProjectListControls(form, search) {
  const params = new URLSearchParams(search);
  const query = form.querySelector('input[name="q"]');
  const sort = form.querySelector('select[name="sort"]');
  if (query) {
    query.value = params.get("q") || "";
  }
  if (sort) {
    const wanted = params.get("sort");
    const known = Array.from(sort.options).some((option) => option.value === wanted);
    sort.value = known ? wanted : sort.options[0].value;
  }
}

// Diagram source is untrusted (agent replies, project files). "sandbox"
// draws each diagram in an iframe that may not run script (design
// 2026-11-03); sandboxedMermaidFrame below tightens Mermaid's own flags.
const MERMAID_CONFIG = { startOnLoad: false, securityLevel: "sandbox" };
// Least recently used first: Map keeps insertion order, and a hit re-inserts.
const MERMAID_CACHE_LIMIT = 50;
const mermaidRenders = new Map();
let mermaidRenderCount = 0;
let mermaidLoading = null;

function mermaidErrorText(error) {
  const raw = error && error.message ? error.message : error;
  const text = String(raw === undefined || raw === null ? "unknown error" : raw)
    .replace(/\s+/g, " ")
    .trim();
  return (text || "unknown error").slice(0, 300);
}

// Auto status swaps replace the whole timeline, so one source is drawn once
// and its result reused by every later copy of the block. Only the
// MERMAID_CACHE_LIMIT most recently used results are kept.
function renderMermaidSource(mermaid, source) {
  const cached = mermaidRenders.get(source);
  if (cached) {
    mermaidRenders.delete(source);
    mermaidRenders.set(source, cached);
    return cached;
  }
  mermaidRenderCount += 1;
  const rendered = Promise.resolve()
    .then(function () {
      return mermaid.render("delibra-mermaid-" + mermaidRenderCount, source);
    })
    .then(
      function (result) {
        return { svg: result.svg };
      },
      function (error) {
        return { error: mermaidErrorText(error) };
      }
    );
  mermaidRenders.set(source, rendered);
  while (mermaidRenders.size > MERMAID_CACHE_LIMIT) {
    mermaidRenders.delete(mermaidRenders.keys().next().value);
  }
  return rendered;
}

function loadMermaid(documentRoot) {
  const view = documentRoot.defaultView;
  if (view.mermaid) {
    return Promise.resolve(view.mermaid);
  }
  if (!mermaidLoading) {
    mermaidLoading = new Promise(function (resolve, reject) {
      const script = documentRoot.createElement("script");
      script.src = documentRoot.body.dataset.mermaidSrc;
      script.onload = function () {
        const dark = Boolean(
          view.matchMedia && view.matchMedia("(prefers-color-scheme: dark)").matches
        );
        view.mermaid.initialize(
          Object.assign({ theme: dark ? "dark" : "default" }, MERMAID_CONFIG)
        );
        resolve(view.mermaid);
      };
      script.onerror = function () {
        mermaidLoading = null;
        reject(new Error("the diagram library did not load"));
      };
      documentRoot.head.appendChild(script);
    });
  }
  return mermaidLoading;
}

// Mermaid sizes its frame to the diagram's full height, but the SVG inside
// shrinks to a narrow column, leaving a blank band. The SVG's viewBox, in the
// frame's data URL, gives the proportions to shrink the frame with it.
function mermaidFrameSize(src) {
  const match = /^data:[^,]*;base64,(.*)$/.exec(String(src || ""));
  if (!match) {
    return null;
  }
  let markup;
  try {
    markup = atob(match[1]);
  } catch (error) {
    return null;
  }
  const box = /viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"/.exec(markup);
  const width = box ? Number(box[1]) : 0;
  const height = box ? Number(box[2]) : 0;
  return width > 0 && height > 0 ? { width: width, height: height } : null;
}

// Mermaid's sandbox frame still allows top navigation on click and popups
// (Codex review H1, seen on 12.2.0), so only a lone iframe is accepted and
// its sandbox is emptied while it sits in an inert template, before it
// ever loads.
function sandboxedMermaidFrame(documentRoot, markup) {
  const template = documentRoot.createElement("template");
  template.innerHTML = markup;
  const content = template.content;
  const frame = content.firstElementChild;
  const strayText = Array.from(content.childNodes).some(function (node) {
    return node.nodeType === 3 && node.textContent.trim() !== "";
  });
  if (content.childElementCount !== 1 || frame.tagName !== "IFRAME" || strayText) {
    return null;
  }
  frame.setAttribute("sandbox", "");
  const size = mermaidFrameSize(frame.getAttribute("src"));
  if (size) {
    frame.style.maxWidth = size.width + "px";
    frame.style.aspectRatio = size.width + " / " + size.height;
    frame.style.height = "auto";
  }
  return frame;
}

function showMermaidResult(block, result) {
  const documentRoot = block.ownerDocument;
  const source = block.querySelector(".mermaid-source");
  const frame = result.error ? null : sandboxedMermaidFrame(documentRoot, result.svg);
  if (!frame) {
    const message = documentRoot.createElement("p");
    message.className = "mermaid-error";
    message.textContent =
      "Diagram could not be rendered: " + (result.error || "unexpected diagram output");
    block.insertBefore(message, source);
    block.dataset.mermaidState = "error";
    return;
  }
  const figure = documentRoot.createElement("div");
  figure.className = "mermaid-figure";
  figure.append(frame);
  const details = documentRoot.createElement("details");
  const summary = documentRoot.createElement("summary");
  summary.textContent = "Source";
  details.append(summary, source);
  block.replaceChildren(figure, details);
  block.dataset.mermaidState = "rendered";
}

// Loads the library only when a page has a diagram to draw.
function renderMermaidBlocks(documentRoot) {
  const blocks = Array.from(
    documentRoot.querySelectorAll("[data-mermaid]:not([data-mermaid-state])")
  );
  if (!blocks.length) {
    return Promise.resolve(0);
  }
  blocks.forEach(function (block) {
    block.dataset.mermaidState = "pending";
  });
  return loadMermaid(documentRoot)
    .then(
      function (mermaid) {
        return Promise.all(
          blocks.map(function (block) {
            const source = block.querySelector(".mermaid-source");
            return renderMermaidSource(mermaid, source ? source.textContent : "").then(
              function (result) {
                showMermaidResult(block, result);
              }
            );
          })
        );
      },
      function (error) {
        blocks.forEach(function (block) {
          showMermaidResult(block, { error: mermaidErrorText(error) });
        });
      }
    )
    .then(function () {
      return blocks.length;
    });
}

// htmx fixes a request's target when the request is sent. If a response
// arrives after another swap replaced that target, swapping onto the
// detached element throws inside htmx, which then never lets the requesting
// element send again; and hx-preserve has already moved the Auto status
// stream into the lost response. Swap into the element now holding the id.
// Returns false when there is none and the response must be dropped.
function retargetDetachedSwap(detail, documentRoot) {
  const target = detail && detail.target;
  if (!target || target.isConnected) {
    return true;
  }
  const successor = target.id ? documentRoot.getElementById(target.id) : null;
  if (!successor) {
    return false;
  }
  detail.target = successor;
  return true;
}

// A copy for a new stream. The new stream replays the round from its start,
// so the text the old one showed would otherwise appear twice.
function freshStreamElement(element) {
  const fresh = element.cloneNode(true);
  fresh.querySelectorAll(".live-text, .live-progress").forEach(function (node) {
    node.textContent = "";
  });
  return fresh;
}

const STREAM_RETRY_MAX_MS = 30000;

function streamRetryDelay(attempts) {
  const count = Number.isFinite(attempts) && attempts > 0 ? attempts : 0;
  return Math.min(STREAM_RETRY_MAX_MS, 1000 * 2 ** count);
}

// Settings tabs are separate pages: leaving one with an edited, unsent form
// would lose the edit, so the browser asks first (design 2026-11-15).
function formSnapshot(form) {
  return Array.prototype.map
    .call(form.elements || [], function (element) {
      const kind = element.type || "";
      const value = kind === "checkbox" || kind === "radio" ? String(element.checked) : String(element.value || "");
      return (element.name || "") + "=" + value;
    })
    .join("\n");
}

function settingsFormsDirty(forms, snapshots, submitting) {
  return Array.prototype.some.call(forms, function (form) {
    return form !== submitting && snapshots.has(form) && snapshots.get(form) !== formSnapshot(form);
  });
}

// The form a page leave belongs to: a submit cancelled by its own confirm
// (Cancel) is not a save, so the unsaved-changes warning must still apply.
function submittedForm(event, current) {
  return event.defaultPrevented ? current : event.target;
}

// Global Prompts tab: ticked "Replace in <project>" boxes ask first (2026-11-17).
function promptReplaceMessage(names, submitterValue) {
  if (!names.length) return null;
  const target = submitterValue === "reset" ? "the built-in text" : "the global text";
  return `Replace this prompt in ${names.join(", ")} with ${target}? Their own versions are removed.`;
}

function confirmPromptReplace(form, event, ask) {
  const names = Array.from(form.querySelectorAll('input[name="replace"]:checked')).map(
    (box) => box.dataset.projectName
  );
  const message = promptReplaceMessage(names, event && event.submitter ? event.submitter.value : "save");
  if (message === null) return true;
  return (ask || window.confirm.bind(window))(message);
}

// Settings that feed the chat system part (design 2026-11-19): a resumed native
// chat keeps the version it started with, so saving one asks first. The server
// marks the form only while an affected agent has a native chat session.
const ONGOING_CHATS_MESSAGE =
  "Ongoing chats keep the version they started with until you Compact or Clear context. " +
  "New chat sessions and Auto turns use this from their next turn. Save?";

function controlChanged(control) {
  if (control.type === "checkbox" || control.type === "radio") {
    return control.checked !== control.defaultChecked;
  }
  if (control.tagName === "SELECT") {
    return Array.prototype.some.call(control.options || [], function (option) {
      return option.selected !== option.defaultSelected;
    });
  }
  return control.value !== control.defaultValue;
}

// Buttons send a value but show none: only the submitter's own name counts for them.
const NON_FIELD_TYPES = ["submit", "button", "reset", "image"];

// No field list (a page shown after a refused save holds unsaved values) means
// every save counts; otherwise a listed field must differ from what the page showed.
function ongoingChatsFieldsChanged(form) {
  const listed = form.getAttribute("data-confirm-fields");
  if (listed === null) return true;
  const names = listed.split(/\s+/).filter(Boolean);
  return Array.prototype.some.call(form.elements || [], function (control) {
    return names.includes(control.name) && !NON_FIELD_TYPES.includes(control.type) && controlChanged(control);
  });
}

// Chat skills (review F2): the chat note names only the skills index, which every
// send rewrites, so list edits reach ongoing chats; only turning skills on (empty to
// listed) or off asks. The saved state comes from the server; a preset button
// always leaves skills on.
function ongoingChatsSwitched(form, submitter) {
  const savedOn = form.getAttribute("data-confirm-saved-on") === "true";
  const field = form.getAttribute("data-confirm-switch");
  const on =
    (submitter && submitter.name && submitter.name === form.getAttribute("data-confirm-switch-on")) ||
    Array.prototype.some.call(form.elements || [], function (control) {
      return control.name === field && String(control.value || "").trim() !== "";
    });
  return Boolean(on) !== savedOn;
}

function confirmOngoingChats(form, submitter, ask) {
  if (!form || typeof form.hasAttribute !== "function" || !form.hasAttribute("data-confirm-ongoing-chats")) {
    return true;
  }
  const matters = form.hasAttribute("data-confirm-switch")
    ? ongoingChatsSwitched(form, submitter)
    : ongoingChatsFieldsChanged(form);
  if (!matters) return true;
  return (ask || window.confirm.bind(window))(ONGOING_CHATS_MESSAGE);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    ONGOING_CHATS_MESSAGE,
    confirmOngoingChats,
    promptReplaceMessage,
    submittedForm,
    confirmPromptReplace,
    applyAgentDefaults,
    formSnapshot,
    settingsFormsDirty,
    alertLiveListError,
    inLivePanel,
    newerLiveRevision,
    openLivePanelForNew,
    staleLiveList,
    appendPickedPaths,
    goToLatestMessage,
    syncLatestButton,
    syncNotesDisclosure,
    applyPathPicker,
    autoExtendPreviewText,
    autoSetupPlacement,
    captureAutoExtendForm,
    captureAutoStopDisclosure,
    chatErrorMessage,
    captureConversationScroll,
    closeAutoSetup,
    closeFocusDialog,
    conversationTimelineForSwap,
    copyAutoComposerTopic,
    fileViewPath,
    finishNoteEdit,
    focusAutoHistoryView,
    formatLocalRange,
    formatLocalStamp,
    formatOffset,
    formatRemainingSeconds,
    tickElapsedClocks,
    freshStreamElement,
    handleFocusDialogCancel,
    handleFocusDialogClosed,
    isDialogBackdropClick,
    isStalePickerSwap,
    keepComposerText,
    lastPathLine,
    localizeTimes,
    MERMAID_CACHE_LIMIT,
    mermaidErrorText,
    mermaidFrameSize,
    NOTES_DISCLOSURE_KEY,
    notesSummaryText,
    moveAutoOrderItem,
    openAutoSetup,
    openFocusDialog,
    openNoteEdit,
    openPathPicker,
    openQuestionsPanelForNew,
    pickerUrl,
    renderMermaidSource,
    removeConversationEmptyState,
    resetFileReader,
    renderChatError,
    resetAutoNotesForm,
    rememberNotesDisclosure,
    resetScrollForNewFile,
    restoreAutoExtendForm,
    restoreAutoStopDisclosure,
    restoreConversationScroll,
    restoreNotesDisclosure,
    retargetDetachedSwap,
    setFocusDialogFit,
    showPageAlert,
    setTimeoutFormPending,
    shouldClearChatError,
    skipStaleNotesSwap,
    streamRetryDelay,
    syncAutoOrderControls,
    syncConversationDisclosure,
    collapseConversation,
    syncCollapseButton,
    syncAgentDisclosure,
    browserStorage,
    readDisclosureChoices,
    rememberDisclosureChoice,
    DISCLOSURE_LIMIT,
    AGENT_DISCLOSURE_KEY,
    MESSAGE_DISCLOSURE_KEY,
    syncAutoDisabledControls,
    syncAutoNotesAvailability,
    syncComposerAvailability,
    updateTimeoutCountdown,
  };
}

if (typeof document !== "undefined") {
  const conversationScrollSnapshots = new WeakMap();
  const fileViewPaths = new WeakMap();
  const autoStopDisclosureSnapshots = new WeakMap();
  const autoExtendSnapshots = new WeakMap();
  // Refused reconnects per stream element. Never an attribute: htmx sets an
  // element up again whenever its attributes change, which would reopen the
  // stream and leave the children's sse: listeners on the closed one.
  const streamRetries = new WeakMap();

  initializeDynamicPresentation(document);
  if (document.querySelector("#conversions-panel")) {
    tickElapsedClocks(document, Date.now());
    setInterval(function () {
      tickElapsedClocks(document, Date.now());
    }, 1000);
  }

  // Capture phase: a cancelled save must be prevented before the unsaved-changes
  // bookkeeping below reads defaultPrevented. Only plain settings forms are marked.
  document.addEventListener(
    "submit",
    function (event) {
      if (!confirmOngoingChats(event.target, event.submitter)) event.preventDefault();
    },
    true
  );

  if (document.body && document.body.classList.contains("settings-page")) {
    const settingsForms = document.querySelectorAll("main form[method='post']");
    const settingsSnapshots = new Map();
    settingsForms.forEach(function (form) {
      settingsSnapshots.set(form, formSnapshot(form));
    });
    let submittingForm = null;
    document.addEventListener("submit", function (event) {
      submittingForm = submittedForm(event, submittingForm);
    });
    window.addEventListener("beforeunload", function (event) {
      if (settingsFormsDirty(settingsForms, settingsSnapshots, submittingForm)) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  const disclosureStorage = browserStorage(window);
  const applyAgentChoices = function () {
    syncAgentDisclosure(
      document,
      readDisclosureChoices(disclosureStorage, AGENT_DISCLOSURE_KEY)
    );
  };
  const applyMessageChoices = function (timeline) {
    syncConversationDisclosure(
      timeline,
      readDisclosureChoices(disclosureStorage, MESSAGE_DISCLOSURE_KEY)
    );
  };
  syncLatestButton(document);
  renderMermaidBlocks(document);
  localizeTimes(document);
  applyAgentChoices();
  const bootTimeline = document.getElementById("chat-timeline");
  if (bootTimeline) {
    applyMessageChoices(bootTimeline);
    syncCollapseButton(bootTimeline);
  }
  // After settle, not afterSwap: an outerHTML swap's event target is the
  // replaced element, and OOB swaps land elsewhere. One scan finds them all.
  document.addEventListener("htmx:afterSettle", function () {
    renderMermaidBlocks(document);
    localizeTimes(document);
    applyAgentChoices();
    tickElapsedClocks(document, Date.now());  // a re-rendered clock starts from the server's value now
  });
  // A click on the summary itself is the reader's choice; it runs before the
  // toggle, so the new state is the opposite of the current one.
  document.addEventListener("click", function (event) {
    const summary = event.target.closest ? event.target.closest("summary") : null;
    const details = summary ? summary.parentElement : null;
    if (!details || details.tagName !== "DETAILS") {
      return;
    }
    if (details.classList.contains("agent-details")) {
      const card = details.closest(".agent-card[data-session-id]");
      if (card) {
        rememberDisclosureChoice(
          disclosureStorage, AGENT_DISCLOSURE_KEY, card.dataset.sessionId, !details.open
        );
      }
    } else if (details.classList.contains("round-details")) {
      const message = details.closest(".round[id], .live-round[id]");
      if (message) {
        rememberDisclosureChoice(
          disclosureStorage, MESSAGE_DISCLOSURE_KEY, message.id, !details.open
        );
      }
    }
  });
  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("[data-timeline-latest]")) {
      goToLatestMessage(document);
    }
  });
  document.addEventListener("click", function (event) {
    const button = event.target.closest
      ? event.target.closest("[data-collapse-messages]")
      : null;
    const timeline = button ? button.closest("#chat-timeline") : null;
    if (timeline) {
      collapseConversation(timeline, disclosureStorage);
    }
  });
  // Scroll does not bubble; capture sees the timeline's own scrolling.
  document.addEventListener(
    "scroll",
    function (event) {
      if (event.target && event.target.id === "chat-timeline") {
        syncLatestButton(document);
      }
    },
    true
  );
  window.addEventListener("resize", function () {
    syncLatestButton(document);
  });
  // Opening a message or streaming text moves the last message without any
  // scroll. An IntersectionObserver cannot tell when the top edge of a message
  // taller than the panel crosses it, so watch every message's size instead.
  const latestResize =
    typeof ResizeObserver === "function"
      ? new ResizeObserver(function () {
          syncLatestButton(document);
        })
      : null;
  function observeMessages() {
    if (!latestResize) {
      return;
    }
    latestResize.disconnect();
    document
      .querySelectorAll("#chat-timeline .round, #chat-timeline .live-round")
      .forEach(function (message) {
        latestResize.observe(message);
      });
  }
  observeMessages();

  window.addEventListener("load", function () {
    // After load, so the page is drawn behind the alert.
    window.setTimeout(function () {
      showPageAlert(document, function (message) {
        window.alert(message);
      });
    }, 0);
  });

  document.querySelectorAll("[data-session-config-form]").forEach(function (form) {
    syncEffortOptions(form);
  });

  document.addEventListener("change", function (event) {
    const agentSelect = event.target.closest("[data-agent-select]");
    if (!agentSelect) {
      return;
    }
    const form = agentSelect.closest("[data-session-config-form]");
    if (!form) {
      return;
    }
    form.dataset.agent = agentSelect.value;
    syncEffortOptions(form);
    applyAgentDefaults(form, agentSelect.value);
  });

  // A field typed in, or an effort picked, is the user's: defaults no longer replace it.
  document.addEventListener("input", function (event) {
    if (event.target.matches && event.target.matches('[data-agent-defaults] input[name="model"]')) {
      event.target.dataset.edited = "true";
    }
  });
  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-agent-defaults] [data-effort-select]")) {
      event.target.dataset.edited = "true";
    }
  });

  document.addEventListener("htmx:sseMessage", function (event) {
    const message = event.detail;
    if (!message || message.type !== "reset") {
      return;
    }
    const domId = String(message.data || "");
    const byId = /^round-[0-9a-f]{32}-\d+$/.test(domId)
      ? document.getElementById(domId)
      : null;
    const live = byId || event.target.closest(".live-round");
    if (!live) {
      return;
    }
    const text = live.querySelector(".live-text");
    const progress = live.querySelector(".live-progress");
    if (text) {
      text.textContent = "";
    }
    if (progress) {
      progress.textContent = "";
    }
  });

  // Dispatched on the requesting element, which for the Auto refreshes stays
  // on the page while their target is replaced.
  document.addEventListener("htmx:beforeOnLoad", function (event) {
    if (!retargetDetachedSwap(event.detail, document)) {
      event.preventDefault();
    }
  });

  // A stream that ends reconnects on the same EventSource. One the browser
  // closes for good (a refused reconnect) is replaced by sse.js with a new
  // EventSource, but htmx keeps the hx-trigger="sse:..." listeners on the
  // closed one: the tab would look connected and hear nothing. A fresh copy
  // is set up from scratch instead; removing the old element also stops
  // sse.js's own retry, which checks that the element is still on the page.
  document.addEventListener("htmx:sseError", function (event) {
    const element = event.target;
    const source = event.detail && event.detail.source;
    if (
      !source ||
      source.readyState !== EventSource.CLOSED ||
      !element.isConnected ||
      !element.hasAttribute("sse-connect")
    ) {
      return;
    }
    const attempts = streamRetries.get(element) || 0;
    const fresh = freshStreamElement(element);
    streamRetries.set(fresh, attempts + 1);
    element.replaceWith(fresh);
    window.setTimeout(function () {
      if (fresh.isConnected) {
        htmx.process(fresh);
      }
    }, streamRetryDelay(attempts));
  });

  document.addEventListener("htmx:sseOpen", function (event) {
    streamRetries.delete(event.target);
  });

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || typeof form.matches !== "function" || !form.matches("[data-note-edit-form]")) {
      return;
    }
    event.preventDefault();
    htmx.ajax("POST", form.dataset.noteEditUrl, { source: form, swap: "none" });
  });

  document.addEventListener("htmx:responseError", function (event) {
    const editSource = (event.detail && event.detail.elt) || event.target;
    if (
      editSource &&
      typeof editSource.closest === "function" &&
      editSource.closest("[data-note-edit-form]")
    ) {
      return; // afterRequest pops it up instead
    }
    if (inLivePanel(editSource)) {
      return; // its own loadend listener pops it up (htmx:beforeRequest)
    }
    const region = document.getElementById("chat-errors");
    const xhr = event.detail && event.detail.xhr;
    if (!region || !xhr) {
      return;
    }
    renderChatError(region, xhr.status, xhr.responseText || "");
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    const source = (event.detail && event.detail.elt) || event.target;
    const actionXhr = event.detail && event.detail.xhr;
    if (actionXhr && inLivePanel(source)) {
      actionXhr.addEventListener("loadend", function () {
        alertLiveListError(actionXhr, window.alert.bind(window));
      });
    }
    const form =
      source && typeof source.closest === "function"
        ? source.closest("[data-timeout-extension-form]")
        : null;
    if (form) {
      setTimeoutFormPending(form, true);
    }
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    const detail = event.detail;
    const target = detail && detail.target;
    const xhr = detail && detail.xhr;
    if (!target || !xhr) {
      return;
    }
    const timeline = conversationTimelineForSwap(target, target);
    if (timeline) {
      conversationScrollSnapshots.set(xhr, captureConversationScroll(timeline));
    }
    const autoStop = captureAutoStopDisclosure(target);
    if (autoStop) {
      autoStopDisclosureSnapshots.set(xhr, autoStop);
    }
    const autoExtend = captureAutoExtendForm(
      target,
      detail.requestConfig ? detail.requestConfig.elt : null
    );
    if (autoExtend) {
      autoExtendSnapshots.set(xhr, autoExtend);
    }
    if (target.id === "file-reader" || target.id === "focus-dialog-content") {
      fileViewPaths.set(xhr, fileViewPath(target));
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    const detail = event.detail;
    const target = detail && detail.target;
    if (!target) {
      return;
    }
    initializeDynamicPresentation(event.target);
    // Here as well as after settle: settling comes ~20ms later, long enough
    // for a re-rendered card to flash closed.
    applyAgentChoices();
    openQuestionsPanelForNew(target, detail.xhr ? detail.xhr.responseText : "");
    const timeline = conversationTimelineForSwap(target, event.target);
    if (timeline) {
      removeConversationEmptyState(timeline);
      applyMessageChoices(timeline);
      syncCollapseButton(timeline);
      const snapshot = detail.xhr
        ? conversationScrollSnapshots.get(detail.xhr)
        : null;
      restoreConversationScroll(timeline, snapshot);
      if (detail.xhr) {
        conversationScrollSnapshots.delete(detail.xhr);
      }
    }
    // After the reading position is restored: the swap may have added a round.
    observeMessages();
    syncLatestButton(document);
    if (detail.xhr && autoStopDisclosureSnapshots.has(detail.xhr)) {
      restoreAutoStopDisclosure(
        document,
        autoStopDisclosureSnapshots.get(detail.xhr)
      );
      autoStopDisclosureSnapshots.delete(detail.xhr);
    }
    if (detail.xhr && autoExtendSnapshots.has(detail.xhr)) {
      restoreAutoExtendForm(document, autoExtendSnapshots.get(detail.xhr));
      autoExtendSnapshots.delete(detail.xhr);
    }
    if (detail.xhr && fileViewPaths.has(detail.xhr)) {
      resetScrollForNewFile(target, fileViewPaths.get(detail.xhr));
      fileViewPaths.delete(detail.xhr);
    }
    if (focusAutoHistoryView(target)) {
      return;
    }
    if (target.id !== "focus-dialog-content") {
      return;
    }
    const dialog = document.getElementById("focus-dialog");
    if (dialog) {
      openFocusDialog(dialog, document.activeElement);
    }
  });

  document.addEventListener("click", function (event) {
    const noteEdit =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-note-edit], [data-note-edit-cancel]")
        : null;
    if (noteEdit) {
      if (noteEdit.matches("[data-note-edit]")) {
        openNoteEdit(noteEdit, document);
      } else {
        const editDialog = document.getElementById("note-edit-dialog");
        if (editDialog && editDialog.open) {
          editDialog.close();
        }
      }
      return;
    }
    const pickerControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest(
            "[data-path-picker-open], [data-path-picker-add], [data-path-picker-choose], [data-path-picker-close], [data-picker-add-files], [data-path-picker-use]"
          )
        : null;
    if (pickerControl) {
      if (pickerControl.matches("[data-path-picker-open]")) {
        openPathPicker(pickerControl, document, window.htmx);
      } else {
        applyPathPicker(pickerControl, document, window.htmx);
      }
      return;
    }
    const autoCloseControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-auto-setup-close]")
        : null;
    if (autoCloseControl) {
      closeAutoSetup(autoCloseControl);
      return;
    }
    const autoOrderControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-auto-order-move]")
        : null;
    if (autoOrderControl) {
      moveAutoOrderItem(autoOrderControl);
      return;
    }
    const fileCloseControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-file-reader-close]")
        : null;
    if (fileCloseControl) {
      const reader = document.getElementById("file-reader");
      if (reader) {
        resetFileReader(reader);
      }
      return;
    }
    const fitControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-focus-dialog-fit]")
        : null;
    if (fitControl) {
      const fitDialog = fitControl.closest("#focus-dialog");
      if (fitDialog) {
        setFocusDialogFit(fitDialog, fitControl.dataset.focusDialogFit);
      }
      return;
    }
    const closeControl =
      event.target && typeof event.target.closest === "function"
        ? event.target.closest("[data-focus-dialog-close]")
        : null;
    const dialog = document.getElementById("focus-dialog");
    if (!dialog) {
      return;
    }
    if (closeControl || isDialogBackdropClick(dialog, event)) {
      closeFocusDialog(dialog);
    }
  });

  document.addEventListener("htmx:sseBeforeMessage", function (event) {
    const panels = event.target;
    if (!panels || panels.id !== "transfer-panels") {
      return;
    }
    event.preventDefault(); // the data is only a revision; nothing to swap in
    const name = event.detail && event.detail.type;
    const refresh = LIVE_LISTS[name] ? document.getElementById(`${name}-refresh`) : null;
    if (refresh && liveListEventIsNewer(event.detail)) {
      htmx.trigger(refresh, `${name}-changed`);
    }
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (isStalePickerSwap(event.detail, document)) {
      event.detail.shouldSwap = false;
    }
    const detail = event.detail || {};
    const responseText = detail.xhr && detail.xhr.responseText;
    if (staleLiveList(detail.target, responseText)) {
      detail.shouldSwap = false;
    } else if (detail.shouldSwap) {
      openLivePanelForNew(detail.target, responseText);
    }
  });

  document.addEventListener("change", function (event) {
    const all = event.target;
    if (!all || typeof all.matches !== "function" || !all.matches("[data-picker-select-all]")) {
      return;
    }
    const body = all.closest("#path-picker-body");
    if (body) {
      body.querySelectorAll("input[data-picker-value]").forEach(function (input) {
        input.checked = all.checked;
      });
    }
  });

  document.addEventListener("input", function (event) {
    const input = event.target;
    if (input && typeof input.matches === "function" && input.matches("[data-auto-extend-input]")) {
      updateAutoExtendPreview(input.closest("form"));
    }
  });

  const notesDisclosure = document.querySelector("[data-notes-disclosure]");
  if (notesDisclosure && disclosureStorage) {
    restoreNotesDisclosure(notesDisclosure, disclosureStorage);
    notesDisclosure.addEventListener("toggle", function () {
      rememberNotesDisclosure(notesDisclosure, disclosureStorage);
    });
  }

  const focusDialog = document.getElementById("focus-dialog");
  if (focusDialog) {
    focusDialog.addEventListener("cancel", handleFocusDialogCancel);
    focusDialog.addEventListener("close", handleFocusDialogClosed);
  }

  document.addEventListener("htmx:oobBeforeSwap", function (event) {
    skipStaleNotesSwap(event.detail);
    keepComposerText(event.detail);
  });

  // Out-of-band swaps (agent card, composer, notes lists) can change where a
  // note goes; the main swap's handler may not run for hx-swap="none".
  document.addEventListener("htmx:oobAfterSwap", function () {
    syncAutoNotesAvailability(document);
  });

  document.addEventListener("htmx:historyRestore", function (event) {
    const form = document.getElementById("project-list-controls");
    if (form) {
      const path = (event.detail && event.detail.path) || window.location.href;
      syncProjectListControls(form, new URL(path, window.location.href).search);
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    const source = (event.detail && event.detail.elt) || event.target;
    const timeoutForm =
      source && typeof source.closest === "function"
        ? source.closest("[data-timeout-extension-form]")
        : null;
    if (timeoutForm) {
      setTimeoutFormPending(timeoutForm, false);
    }
    const noteEditForm =
      source && typeof source.closest === "function"
        ? source.closest("[data-note-edit-form]")
        : null;
    if (noteEditForm) {
      finishNoteEdit(noteEditForm, event.detail, function (message) {
        window.alert(message);
      });
      return;
    }
    const region = document.getElementById("chat-errors");
    const detail = event.detail;
    resetAutoNotesForm(source, detail);
    const requestPath =
      (detail && detail.pathInfo && detail.pathInfo.requestPath) ||
      (detail && detail.requestConfig && detail.requestConfig.path) ||
      "";
    if (
      region &&
      detail &&
      detail.successful &&
      shouldClearChatError(requestPath)
    ) {
      region.textContent = "";
    }
  });
}
