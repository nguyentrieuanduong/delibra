"""Notes to a running chat reply (design B)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'
NOTE = "#auto-note-text"


@pytest.mark.browser
def test_a_note_reaches_the_replying_agent_and_closes_for_an_idle_one(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["sleep"], tmp_path)
    alpha, beta = app["session_ids"]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    expect(page.locator(NOTE)).to_be_disabled()
    page.fill(PROMPT, "Write a long answer")
    page.click("#chat-composer button[type=submit]")

    expect(page.locator(NOTE)).to_be_enabled(timeout=10_000)
    expect(page.locator("#chat-note-hint")).to_have_text("Notes reach Alpha while it replies.")
    expect(page.locator("#auto-note-hint")).to_be_hidden()
    page.fill(NOTE, "Keep it short")
    page.click("#auto-notes button[type=submit]")
    expect(page.locator("#chat-notes-list")).to_contain_text("N1 · pending", timeout=5_000)
    expect(page.locator(NOTE)).to_have_value("")
    view = app["store"].workspace_dir(alpha) / "delibra-notes.json"
    assert "Keep it short" in view.read_text(encoding="utf-8")

    page.click(f"#agent-select-{beta}")
    expect(page.locator("#composer-agent")).to_contain_text("sending as Beta", timeout=10_000)
    expect(page.locator(NOTE)).to_be_disabled()
    # Empty for Beta, so zero height: check the property, not visibility.
    expect(page.locator("#chat-notes-list")).to_have_js_property("hidden", False)
    expect(page.locator("#chat-notes-list")).to_have_attribute("data-session-id", beta)
    expect(page.locator("#chat-notes-list")).not_to_contain_text("Keep it short")
    expect(page.locator("#chat-note-hint")).to_have_text("Notes reach Beta while it replies.")
    page.click(f"#agent-select-{alpha}")
    expect(page.locator("#composer-agent")).to_contain_text("sending as Alpha", timeout=10_000)
    expect(page.locator("#chat-notes-list")).to_contain_text("N1 · pending")
    expect(page.locator(NOTE)).to_be_enabled()
