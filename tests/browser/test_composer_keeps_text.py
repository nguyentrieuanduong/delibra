"""Unsent composer text survives picking another agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'


@pytest.mark.browser
def test_picking_another_agent_keeps_the_typed_message(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha, beta = app["session_ids"]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    page.fill(PROMPT, "Draft for\ntwo lines")

    page.click(f"#agent-select-{beta}")

    expect(page.locator("#composer-agent")).to_contain_text("sending as Beta", timeout=10_000)
    expect(page.locator(PROMPT)).to_have_value("Draft for\ntwo lines")
