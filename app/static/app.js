"use strict";

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

document.querySelectorAll("[data-session-config-form]").forEach(function (form) {
  syncEffortOptions(form);
  const agentSelect = form.querySelector("[data-agent-select]");
  if (agentSelect) {
    agentSelect.addEventListener("change", function () {
      form.dataset.agent = agentSelect.value;
      syncEffortOptions(form);
    });
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
