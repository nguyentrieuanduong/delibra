"""Continue Auto reads like a table: each setting's text, then its control."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


@pytest.mark.browser
def test_continue_auto_puts_each_setting_and_its_control_on_one_line(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Resume")
    page.fill(f"{AUTO_DIALOG} [name=max_cycles]", "1")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    summary = page.locator("details.auto-resume > summary")
    expect(summary).to_be_visible(timeout=10_000)
    summary.click()

    rows = page.evaluate(
        """() => Array.from(document.querySelectorAll("details.auto-resume form > label")).map((label) => {
          const control = label.querySelector("input").getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(label);
          const text = Array.from(range.getClientRects()).filter((line) => line.right <= control.left + 0.5);
          return {
            name: label.querySelector("input").name,
            textRight: Math.max(...text.map((line) => line.right)),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            controlLeft: control.left,
            controlMiddle: (control.top + control.bottom) / 2,
          };
        })"""
    )
    names = [row["name"] for row in rows]
    assert names == ["additional_cycles", "turn_timeout_seconds", "pause_on_blocking_questions"], rows
    for row in rows:
        # The text sits left of its control, on the control's line.
        assert row["textMiddle"] is not None and row["textRight"] <= row["controlLeft"] + 0.5, row
        assert abs(row["textMiddle"] - row["controlMiddle"]) <= 6, row
    # One control column, like a table.
    assert max(row["controlLeft"] for row in rows) - min(row["controlLeft"] for row in rows) <= 0.5, rows

    title = page.evaluate(
        """() => {
          const range = document.createRange();
          range.selectNodeContents(document.querySelector("#auto-status h2"));
          return new Set(Array.from(range.getClientRects(), (line) => Math.round(line.top))).size;
        }"""
    )
    # The open form wraps below the title instead of squeezing it.
    assert title == 1
