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

function closeFocusDialog(dialog) {
  if (dialog.open) {
    dialog.close();
  }
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

function syncConversationDisclosure(timeline) {
  const messages = Array.from(
    timeline.querySelectorAll(".round, .live-round")
  );
  messages.forEach(function (message) {
    const details = message.querySelector(".round-details");
    if (details) {
      details.open = false;
    }
  });
  const lastMessage = messages[messages.length - 1];
  const lastDetails = lastMessage
    ? lastMessage.querySelector(".round-details")
    : null;
  if (!lastDetails) {
    return false;
  }
  lastDetails.open = true;
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
  const status = documentRoot.querySelector("#auto-status");
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

function syncAutoNotesAvailability(documentRoot) {
  if (!documentRoot || typeof documentRoot.querySelector !== "function") {
    return false;
  }
  const status = documentRoot.querySelector("#auto-status");
  const open = Boolean(status && status.dataset.autoNotesOpen === "true");
  documentRoot.querySelectorAll("[data-auto-notes-input]").forEach(function (control) {
    control.disabled = !open;
  });
  return open;
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

// Every notes list arrives out-of-band. A status update rendered before a
// note was posted can land after the post's response; its older list must
// not replace the newer one.
function skipStaleNotesSwap(detail) {
  if (!detail || !detail.target || detail.target.id !== "auto-notes-list" || !detail.fragment) {
    return false;
  }
  const element = detail.fragment.dataset
    ? detail.fragment
    : detail.fragment.firstElementChild;
  // Number("") is 0, so an absent attribute must stay NaN, not become 0.
  const revision = (value) => (value ? Number(value) : NaN);
  const incoming = revision(element && element.dataset ? element.dataset.notesRevision : "");
  const shown = revision(detail.target.dataset ? detail.target.dataset.notesRevision : "");
  if (!Number.isFinite(incoming) || !Number.isFinite(shown) || incoming >= shown) {
    return false;
  }
  detail.shouldSwap = false;
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
  dialog.dataset.pickerSession = session;
  dialog.dataset.pickerTarget = button.dataset.pickerTarget;
  dialog.dataset.pickerList = list;
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
      values: { view: "dialog", files: field.value },
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
          values: { view: "row", files: field.value },
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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    appendPickedPaths,
    applyPathPicker,
    autoSetupPlacement,
    captureAutoStopDisclosure,
    chatErrorMessage,
    captureConversationScroll,
    closeAutoSetup,
    closeFocusDialog,
    conversationTimelineForSwap,
    copyAutoComposerTopic,
    focusAutoHistoryView,
    formatRemainingSeconds,
    handleFocusDialogCancel,
    isDialogBackdropClick,
    isStalePickerSwap,
    lastPathLine,
    moveAutoOrderItem,
    openAutoSetup,
    openFocusDialog,
    openPathPicker,
    openQuestionsPanelForNew,
    pickerUrl,
    removeConversationEmptyState,
    resetFileReader,
    renderChatError,
    resetAutoNotesForm,
    restoreAutoStopDisclosure,
    restoreConversationScroll,
    showPageAlert,
    setTimeoutFormPending,
    shouldClearChatError,
    skipStaleNotesSwap,
    syncAutoOrderControls,
    syncConversationDisclosure,
    syncAutoDisabledControls,
    syncAutoNotesAvailability,
    syncComposerAvailability,
    updateTimeoutCountdown,
  };
}

if (typeof document !== "undefined") {
  const conversationScrollSnapshots = new WeakMap();
  const autoStopDisclosureSnapshots = new WeakMap();

  initializeDynamicPresentation(document);

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

  document.addEventListener("htmx:responseError", function (event) {
    const region = document.getElementById("chat-errors");
    const xhr = event.detail && event.detail.xhr;
    if (!region || !xhr) {
      return;
    }
    renderChatError(region, xhr.status, xhr.responseText || "");
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    const source = (event.detail && event.detail.elt) || event.target;
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
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    const detail = event.detail;
    const target = detail && detail.target;
    if (!target) {
      return;
    }
    initializeDynamicPresentation(event.target);
    openQuestionsPanelForNew(target, detail.xhr ? detail.xhr.responseText : "");
    const timeline = conversationTimelineForSwap(target, event.target);
    if (timeline) {
      removeConversationEmptyState(timeline);
      syncConversationDisclosure(timeline);
      const snapshot = detail.xhr
        ? conversationScrollSnapshots.get(detail.xhr)
        : null;
      restoreConversationScroll(timeline, snapshot);
      if (detail.xhr) {
        conversationScrollSnapshots.delete(detail.xhr);
      }
    }
    if (detail.xhr && autoStopDisclosureSnapshots.has(detail.xhr)) {
      restoreAutoStopDisclosure(
        document,
        autoStopDisclosureSnapshots.get(detail.xhr)
      );
      autoStopDisclosureSnapshots.delete(detail.xhr);
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

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (isStalePickerSwap(event.detail, document)) {
      event.detail.shouldSwap = false;
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

  const focusDialog = document.getElementById("focus-dialog");
  if (focusDialog) {
    focusDialog.addEventListener("cancel", handleFocusDialogCancel);
    focusDialog.addEventListener("close", restoreFocusDialogTrigger);
  }

  document.addEventListener("htmx:oobBeforeSwap", function (event) {
    skipStaleNotesSwap(event.detail);
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
