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
  if (detailTarget.classList.contains("chat-timeline")) {
    return detailTarget;
  }
  if (!swappedElement || typeof swappedElement.closest !== "function") {
    return null;
  }
  return swappedElement.closest(".chat-timeline");
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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    chatErrorMessage,
    closeFocusDialog,
    conversationTimelineForSwap,
    handleFocusDialogCancel,
    isDialogBackdropClick,
    openFocusDialog,
    removeConversationEmptyState,
    resetFileReader,
    renderChatError,
    shouldClearChatError,
    syncConversationDisclosure,
  };
}

if (typeof document !== "undefined") {
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

  document.addEventListener("htmx:afterSwap", function (event) {
    const target = event.detail && event.detail.target;
    if (!target) {
      return;
    }
    const timeline = conversationTimelineForSwap(target, event.target);
    if (timeline) {
      removeConversationEmptyState(timeline);
      syncConversationDisclosure(timeline);
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

  const focusDialog = document.getElementById("focus-dialog");
  if (focusDialog) {
    focusDialog.addEventListener("cancel", handleFocusDialogCancel);
    focusDialog.addEventListener("close", restoreFocusDialogTrigger);
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    const region = document.getElementById("chat-errors");
    const detail = event.detail;
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
