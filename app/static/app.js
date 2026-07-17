"use strict";

document.addEventListener("htmx:sseMessage", function (event) {
  const message = event.detail;
  if (!message || message.type !== "reset") {
    return;
  }
  const round = String(message.data || "");
  const byId = /^\d+$/.test(round)
    ? document.getElementById(`live-${round}`)
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
