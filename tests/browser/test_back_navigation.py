"""Back to the chat page shows the current state, not the first-loaded page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'


@pytest.mark.browser
def test_back_from_manage_shows_the_auto_run_started_before_leaving(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["sleep"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)

    page.click("[data-auto-open]")
    page.fill("#auto-setup-host textarea[name=topic]", "Back navigation")
    page.click("#auto-setup-host button[type=submit]:has-text('Start Auto')")
    expect(page.locator("#auto-panel")).to_contain_text("Stop Auto", timeout=10_000)

    page.click("a:has-text('Manage')")
    expect(page).to_have_url(f"{app['base_url']}/projects/Verify/settings")
    page.go_back()

    expect(page.locator("#auto-panel")).to_contain_text("Stop Auto", timeout=10_000)
    expect(page.locator(PROMPT)).to_be_disabled()
