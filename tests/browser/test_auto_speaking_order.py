"""The setup dialog's move controls reach the stored speaking order."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


@pytest.mark.browser
def test_moving_an_agent_up_changes_who_speaks_first(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    alpha, beta = app["session_ids"]
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Speaking order")

    move_beta_up = page.get_by_role("button", name="Move Beta up", exact=True)
    move_beta_up.click()

    # Beta is first now, so its Up is disabled and focus moves to its Down.
    expect(move_beta_up).to_be_disabled()
    expect(page.get_by_role("button", name="Move Beta down", exact=True)).to_be_focused()
    expect(page.get_by_role("button", name="Move Alpha down", exact=True)).to_be_disabled()
    page.click(f"{AUTO_DIALOG} form button[type=submit]")

    record = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        runs = app["store"].list_auto_runs()
        if runs and runs[-1].status not in {"preparing", "discussing"}:
            record = runs[-1]
            break
        time.sleep(0.05)
    assert record is not None, "the Auto run did not finish"
    assert [item.session_id for item in record.participants] == [beta, alpha]
    assert record.discussion[0].session_id == beta
