"""The message box grows only downward, so it cannot cover the Auto panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'


@pytest.mark.browser
def test_dragging_the_message_box_corner_does_not_widen_it(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    box = page.locator(PROMPT)
    expect(box).to_be_enabled(timeout=10_000)
    before = box.bounding_box()
    panel = page.locator("#auto-panel").bounding_box()
    assert before["x"] + before["width"] <= panel["x"]

    # The resize grip sits in the bottom-right corner.
    page.mouse.move(before["x"] + before["width"] - 3, before["y"] + before["height"] - 3)
    page.mouse.down()
    page.mouse.move(before["x"] + before["width"] + 300, before["y"] + before["height"] + 80, steps=10)
    page.mouse.up()

    after = box.bounding_box()
    assert after["width"] == before["width"]
    assert after["height"] > before["height"]
    assert after["x"] + after["width"] <= panel["x"]
