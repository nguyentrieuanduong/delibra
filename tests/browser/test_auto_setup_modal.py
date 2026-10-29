"""The Start Auto dialog is a modal at the Message box, wholly inside the viewport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


def open_auto_setup(page: Any, base_url: str, height: int) -> Any:
    page.set_viewport_size({"width": 1400, "height": height})
    page.goto(f"{base_url}/projects/Verify/chat")
    page.click("[data-auto-open]")
    dialog = page.locator(AUTO_DIALOG)
    expect(dialog).to_be_visible(timeout=10_000)
    return dialog


@pytest.mark.browser
def test_start_auto_opens_as_a_modal_at_the_message_box(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    dialog = open_auto_setup(page, app["base_url"], 900)

    assert dialog.evaluate("node => node.matches(':modal')")
    box = dialog.bounding_box()
    composer = page.locator("#chat-composer").bounding_box()
    assert abs(box["y"] - composer["y"]) <= 1, (box, composer)
    assert abs(box["x"] - composer["x"]) <= 1, (box, composer)
    assert box["y"] + box["height"] <= 900, box

    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)


@pytest.mark.browser
def test_start_auto_fits_a_short_viewport(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    dialog = open_auto_setup(page, app["base_url"], 600)

    box = dialog.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 600, box
    submit = page.locator(f"{AUTO_DIALOG} form button[type=submit]")
    submit.scroll_into_view_if_needed()
    expect(submit).to_be_in_viewport()
