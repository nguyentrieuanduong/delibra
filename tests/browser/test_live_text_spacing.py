"""Streamed text keeps the spaces and line breaks between chunks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'


@pytest.mark.browser
def test_live_text_keeps_leading_spaces_and_line_breaks(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["spaced"], tmp_path, delay=0.05)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    page.fill(PROMPT, "Go")
    page.click("#chat-composer button[type=submit]")

    live_text = page.locator(f"section.live-round#round-{alpha}-1 .live-text")
    expect(live_text).to_contain_text("End", timeout=15_000)
    assert live_text.evaluate("e => e.textContent") == "Hello world\nNext  line\n\nEnd"
