"""A question an Auto agent writes mid-turn reaches the browser within 5 s."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


def start_auto_run(page: Any, base_url: str) -> None:
    page.goto(f"{base_url}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Live questions")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-active="true"]')).to_have_count(
        1, timeout=10_000
    )


@pytest.mark.browser
def test_a_mid_turn_question_appears_within_five_seconds_and_is_labelled_on_stop(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    start_auto_run(page, app["base_url"])
    spool = app["store"].workspace_dir(app["session_ids"][0]) / "questions" / "outbox"
    deadline = time.monotonic() + 10
    while not spool.is_dir() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spool.is_dir()

    written = time.monotonic()
    (spool / "q1.json").write_text(
        json.dumps({"questions": [{"question": "Live in the browser?"}]}),
        encoding="utf-8",
    )
    expect(page.locator("#question-Q1")).to_be_visible(timeout=5_000)
    elapsed = time.monotonic() - written
    print(f"live question visible after {elapsed:.2f}s")
    assert elapsed < 5, elapsed
    expect(page.locator("#question-Q1-outcome")).to_have_text("")
    draft = page.locator("#question-Q1 textarea[name=answer]")
    draft.fill("typed before stop")

    page.locator("#auto-status summary", has_text="Stop Auto").click()
    page.locator("#auto-status").get_by_role("button", name="Stop now").click()

    expect(page.locator("#question-Q1-outcome")).to_have_text(
        "From stopped turn", timeout=10_000
    )
    expect(draft).to_have_value("typed before stop")
