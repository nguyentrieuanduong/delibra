"""The "↓ Latest" button (design C)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'
LATEST = "[data-timeline-latest]"


def _send(page: Any, text: str, round_id: str) -> None:
    page.fill(PROMPT, text)
    page.click("#chat-composer button[type=submit]")
    expect(page.locator(f"article.round#{round_id}")).to_be_visible(timeout=15_000)
    expect(page.locator(PROMPT)).to_be_enabled(timeout=15_000)


@pytest.mark.browser
def test_latest_jumps_to_the_start_of_the_last_message(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha = app["session_ids"][0]
    # Instant scrolling: a smooth scroll still under way would keep firing scroll
    # events and hide whether the layout check below works on its own.
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    expect(page.locator(LATEST)).to_be_hidden()
    page.add_style_tag(content=".chat-timeline > .round { min-height: 1200px; }")
    frame_height = page.evaluate(
        "document.getElementById('chat-timeline-frame').getBoundingClientRect().height"
    )
    for n in (1, 2, 3):
        _send(page, f"Message {n}", f"round-{alpha}-{n}")

    page.evaluate("document.getElementById('chat-timeline').scrollTop = 0")
    expect(page.locator(LATEST)).to_be_visible()
    page.click(LATEST)
    page.wait_for_function(
        """(id) => {
          const t = document.getElementById('chat-timeline');
          const m = document.getElementById(id);
          return Math.abs(m.getBoundingClientRect().top - t.getBoundingClientRect().top) <= 2;
        }""",
        arg=f"round-{alpha}-3",
    )
    assert page.evaluate(f"document.querySelector('#round-{alpha}-3 .round-details').open")
    expect(page.locator(LATEST)).to_be_hidden()
    # The button floats: the frame is no taller than before.
    assert page.evaluate(
        "document.getElementById('chat-timeline-frame').getBoundingClientRect().height"
    ) == pytest.approx(frame_height, abs=1)

    # A layout change with no scrolling: an earlier message grows and pushes the
    # last one's start below the panel. Scroll anchoring would hide the shift.
    page.add_style_tag(content=".chat-timeline { overflow-anchor: none; }")
    page.evaluate(f"document.getElementById('round-{alpha}-2').style.minHeight = '6000px'")
    expect(page.locator(LATEST)).to_be_visible(timeout=2_000)
