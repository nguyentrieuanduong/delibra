"""Message header times in the reader's timezone (design 2026-11-13, Part A)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterator

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'
RUNNING = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \+07$"
FINISHED = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d – \d\d:\d\d:\d\d \+07$"


@pytest.fixture
def hanoi_page(chromium: Any) -> Iterator[Any]:
    """A page whose browser clock is UTC+7, whatever the machine's is."""

    context = chromium.new_context(timezone_id="Asia/Ho_Chi_Minh")
    try:
        yield context.new_page()
    finally:
        context.close()


@pytest.mark.browser
def test_a_round_header_shows_its_local_start_then_its_range(
    live_server: LiveServer, hanoi_page: Any, tmp_path: Path
) -> None:
    page = hanoi_page
    # Slow enough to see the round while it runs.
    app = live_server(["success"], tmp_path, delay=1.5)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    page.fill(PROMPT, "Hello")
    page.click("#chat-composer button[type=submit]")

    live = page.locator(f"#round-{alpha}-1.live-round h2 .msg-time")
    expect(live).to_have_text(re.compile(RUNNING), timeout=5_000)

    done = page.locator(f"article#round-{alpha}-1 .round-header .msg-time")
    expect(done).to_have_text(re.compile(FINISHED), timeout=15_000)
    assert done.evaluate("node => getComputedStyle(node).fontSize") == "12px"
    stamp = done.bounding_box()
    focus = page.locator(f"article#round-{alpha}-1 .focus-round").bounding_box()
    assert stamp is not None and focus is not None
    # One header row: the time and the Focus button overlap vertically.
    assert stamp["y"] < focus["y"] + focus["height"]
    assert focus["y"] < stamp["y"] + stamp["height"]


@pytest.mark.browser
def test_a_note_time_stays_on_its_title_line_in_the_narrow_column(
    live_server: LiveServer, hanoi_page: Any, tmp_path: Path
) -> None:
    from app.notes import NoteStore
    from tests.test_auto import seed_durable_auto

    page = hanoi_page
    app = live_server(["success"], tmp_path)
    store = app["store"]
    record = seed_durable_auto(store, auto_id="b" * 32, status="stopped", publish=False)
    NoteStore(store.root).add("Prefer SQLite", auto_run=record.number)
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={app['session_ids'][0]}")

    meta = page.locator("#auto-notes-list .auto-note-meta").first
    time = meta.locator(".msg-time")
    expect(time).to_have_text(re.compile(RUNNING), timeout=5_000)
    title = meta.locator(".auto-note-title").bounding_box()
    stamp = time.bounding_box()
    line = meta.bounding_box()
    assert title is not None and stamp is not None and line is not None
    # Same line: the meta is one text line tall and the time sits right of the title.
    assert line["height"] < 2 * stamp["height"] + 4
    assert stamp["x"] >= title["x"] + title["width"]
    assert stamp["x"] + stamp["width"] <= line["x"] + line["width"] + 1
