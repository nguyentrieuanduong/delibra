"""Project settings: each checkbox sits on its text's line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_coding_and_docker_checkboxes_share_a_line_with_their_text(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/settings")

    rows = page.evaluate(
        """() => ["/coding", "/coding/docker", "/global-instruction", "/writable-roots"].map((path) => {
          const label = document.querySelector(`form[action$="${path}"] label.auto-choice`);
          const box = label.querySelector("input[type=checkbox]").getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(label);
          const text = Array.from(range.getClientRects()).filter((line) => line.left >= box.right - 0.5);
          return {
            path,
            text: label.textContent.trim(),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            boxMiddle: (box.top + box.bottom) / 2,
          };
        })"""
    )
    assert [row["text"] for row in rows] == [
        "Enable coding (Auto build runs)",
        "Let build agents use Docker (a Lima VM per project)",
        "Use the global instruction in this project",
        "Create directories that don't exist",
    ], rows
    for row in rows:
        # The text starts right of the checkbox, on the checkbox's line.
        assert row["textMiddle"] is not None, row
        assert abs(row["textMiddle"] - row["boxMiddle"]) <= 6, row


@pytest.mark.browser
def test_chat_page_create_missing_checkbox_shares_a_line_with_its_text(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    # Open every disclosure so the agent forms are laid out.
    page.evaluate("() => document.querySelectorAll('.workspace-agents details').forEach((d) => { d.open = true; })")

    rows = page.evaluate(
        """() => Array.from(document.querySelectorAll('.workspace-agents input[name=create_missing_writable_roots]')).map((input) => {
          const label = input.closest("label");
          const box = input.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(label);
          const text = Array.from(range.getClientRects()).filter((line) => line.left >= box.right - 0.5);
          return {
            text: label.textContent.trim(),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            boxMiddle: (box.top + box.bottom) / 2,
          };
        })"""
    )
    # The create-agent form and both agents' cards.
    assert len(rows) == 3, rows
    for row in rows:
        assert row["text"] == "Create directories that don't exist", row
        assert row["textMiddle"] is not None, row
        assert abs(row["textMiddle"] - row["boxMiddle"]) <= 6, row
