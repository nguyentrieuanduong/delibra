"""The opened-file path header stays compact in the reader and in Focus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

LONG_PATH = "docs/specs/very-long-folder-name-for-testing/2026-11-10-design-notes.md"


def _open(page: Any, base: str, path: str) -> None:
    page.evaluate(
        "(url) => htmx.ajax('GET', url, '#file-reader')",
        f"{base}/projects/Verify/files/view?path={path}",
    )
    expect(page.locator("#file-reader .file-view")).to_be_visible(timeout=10_000)


def _box(page: Any, selector: str) -> dict[str, float]:
    return page.locator(selector).bounding_box()


@pytest.mark.browser
def test_a_long_path_uses_the_reader_width_not_a_narrow_column(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    target = tmp_path / "project" / LONG_PATH
    target.parent.mkdir(parents=True)
    target.write_text("# Notes\n")
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open(page, app["base_url"], LONG_PATH)

    header = _box(page, "#file-reader .file-view-header")
    title = _box(page, "#file-reader .file-view-header h3")
    # The buttons give way to the path instead of squeezing it to a column.
    assert title["width"] >= header["width"] - 1, (header, title)


@pytest.mark.browser
def test_the_focused_path_stays_large_with_a_small_gap(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "short.md").write_text("Body text\n")
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open(page, app["base_url"], "short.md")
    page.click("#file-reader .file-view-actions button:has-text('Focus')")
    expect(page.locator("#file-focus")).to_be_visible(timeout=10_000)

    heading = page.locator("#file-focus .file-view-header h3")
    style = heading.evaluate(
        "(e) => { const c = getComputedStyle(e); "
        "return [parseFloat(c.fontSize), parseInt(c.fontWeight, 10)]; }"
    )
    # The path keeps its large bold title look.
    assert style[0] >= 23 and style[1] >= 700, style
    title = heading.bounding_box()
    body = _box(page, "#file-focus .markdown > :first-child")
    # One line of text, without extra leading.
    assert title["height"] <= style[0] * 1.3, (title, style)
    gap = body["y"] - (title["y"] + title["height"])
    assert gap <= 12, (title, body)
