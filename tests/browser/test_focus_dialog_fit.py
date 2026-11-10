"""Focus dialog layout and fit sizes in real Chromium."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

VIEWPORT = {"width": 1600, "height": 900}
LONG_TEXT = "# Long\n\n" + ("word " * 600) + "\n"


def _open_file(page: Any, base: str) -> None:
    page.evaluate(
        "(url) => htmx.ajax('GET', url, '#file-reader')",
        f"{base}/projects/Verify/files/view?path=long.md",
    )


def _open_focus(page: Any) -> Any:
    page.click("#file-reader .file-view-actions button:has-text('Focus')")
    dialog = page.locator("#focus-dialog")
    expect(dialog.locator("#file-focus")).to_be_visible(timeout=10_000)
    return dialog


def _box(page: Any, selector: str) -> dict[str, float]:
    return page.locator(selector).bounding_box()


@pytest.mark.browser
def test_focus_content_uses_the_full_dialog_width(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "long.md").write_text(LONG_TEXT)
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"])
    _open_focus(page)

    toolbar = _box(page, ".focus-dialog-toolbar")
    content = _box(page, "#focus-dialog-content")
    # The Close button sits in its own row above the content, not beside it.
    assert content["y"] >= toolbar["y"] + toolbar["height"] - 1, (toolbar, content)
    assert abs(content["width"] - toolbar["width"]) <= 1, (toolbar, content)


@pytest.mark.browser
def test_fit_buttons_resize_the_dialog_and_reset_on_close(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "long.md").write_text(LONG_TEXT)
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"])
    dialog = _open_focus(page)
    default = _box(page, "#focus-dialog")
    assert default["width"] < VIEWPORT["width"] * 0.8, default

    fit_width = dialog.get_by_role("button", name="Fit width")
    fit_window = dialog.get_by_role("button", name="Fit window")

    fit_width.click()
    expect(fit_width).to_have_attribute("aria-pressed", "true")
    wide = _box(page, "#focus-dialog")
    assert wide["width"] >= VIEWPORT["width"] - 40, wide
    assert wide["height"] <= VIEWPORT["height"] * 0.86, wide

    fit_window.click()
    expect(fit_window).to_have_attribute("aria-pressed", "true")
    expect(fit_width).to_have_attribute("aria-pressed", "false")
    full = _box(page, "#focus-dialog")
    assert full["width"] >= VIEWPORT["width"] - 2, full
    assert full["height"] >= VIEWPORT["height"] - 2, full
    # The text scrolls inside the content; the toolbar stays on screen.
    expect(dialog.get_by_role("button", name="Close")).to_be_in_viewport()
    content = _box(page, "#focus-dialog-content")
    assert content["y"] + content["height"] <= VIEWPORT["height"] + 1, content

    fit_window.click()
    expect(fit_window).to_have_attribute("aria-pressed", "false")
    assert abs(_box(page, "#focus-dialog")["width"] - default["width"]) <= 1

    fit_window.click()
    dialog.get_by_role("button", name="Close").click()
    expect(dialog).to_be_hidden()
    _open_focus(page)
    expect(fit_window).to_have_attribute("aria-pressed", "false")
    assert abs(_box(page, "#focus-dialog")["width"] - default["width"]) <= 1
