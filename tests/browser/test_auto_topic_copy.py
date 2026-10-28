"""Auto copies unsent composer text into the topic, even after a conversation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


@pytest.mark.browser
def test_auto_copies_the_unsent_message_even_after_a_conversation(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    # A finished run makes its topic the durable fallback ("Earlier topic"),
    # the state AquaEvo was in; the steps copy test_auto_notes.py:24-32.
    app = live_server(["success"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Earlier topic")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-active="false"]')).to_have_count(
        1, timeout=20_000
    )

    page.click("[data-auto-open]")
    expect(page.locator(f"{AUTO_DIALOG} textarea[name=topic]")).to_have_value(
        "Earlier topic", timeout=10_000
    )
    # The existing Close button removes the dialog (closeAutoSetup).
    page.click(f"{AUTO_DIALOG} [data-auto-setup-close]")
    expect(page.locator(AUTO_DIALOG)).to_have_count(0)

    composer = page.locator('#chat-composer textarea[name="prompt"]')
    expect(composer).to_be_enabled(timeout=10_000)
    composer.fill("Plan the roadmap next")
    page.click("[data-auto-open]")

    expect(page.locator(f"{AUTO_DIALOG} textarea[name=topic]")).to_have_value(
        "Plan the roadmap next", timeout=10_000
    )
