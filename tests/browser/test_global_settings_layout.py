"""Global settings: a saved preset's checkbox sits on its text's line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_preset_checkboxes_share_a_line_with_their_text(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/settings")
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\n")
    page.fill("input[name='preset_name_0']", "suite")
    # The list field is hidden: in the UI only the pop-up's Use these files writes it.
    page.evaluate("(value) => { document.getElementById('preset-files-0').value = value; }", str(guide))
    with page.expect_navigation():
        page.click("button:has-text('Save global settings')")

    rows = page.evaluate(
        """() => Array.from(document.querySelectorAll("form.global-settings label.auto-choice")).map((label) => {
          const box = label.querySelector("input[type=checkbox]").getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(label);
          const text = Array.from(range.getClientRects()).filter((line) => line.left >= box.right - 0.5);
          return {
            text: label.textContent.trim(),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            boxMiddle: (box.top + box.bottom) / 2,
          };
        })"""
    )
    assert [row["text"] for row in rows] == ["Remove this preset"], rows
    for row in rows:
        assert row["textMiddle"] is not None, row
        assert abs(row["textMiddle"] - row["boxMiddle"]) <= 6, row
