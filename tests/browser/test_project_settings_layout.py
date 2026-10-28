"""Project settings: each checkbox sits on its text's line."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_coding_and_docker_checkboxes_share_a_line_with_their_text(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/settings")

    rows = page.evaluate(
        """() => ["/coding", "/coding/docker"].map((path) => {
          const label = document.querySelector(`form[action$="${path}"] label.auto-choice`);
          const box = label.querySelector("input[type=checkbox]").getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(label);
          const text = Array.from(range.getClientRects()).filter((line) => line.left >= box.right - 0.5);
          return {
            path,
            text: label.textContent.trim(),
            textMiddle: text.length ? (text[0].top + text[0].bottom) / 2 : null,
            boxMiddle: (box.top + box.bottom) / 2,
          };
        })"""
    )
    assert [row["text"] for row in rows] == [
        "Enable coding (Auto build runs)",
        "Let build agents use Docker (a Lima VM per project)",
    ], rows
    for row in rows:
        # The text starts right of the checkbox, on the checkbox's line.
        assert row["textMiddle"] is not None, row
        assert abs(row["textMiddle"] - row["boxMiddle"]) <= 6, row
