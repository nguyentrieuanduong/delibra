"""An Auto tab keeps following the run after its stream or panel is disturbed.

Both failures left a tab stuck on "discussing" while the run went on: the
status, the conversation and the questions all refresh from the hidden
stream element inside #auto-status, so losing that element, or its
listeners, silences all three at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer
from tests.browser.test_auto_timeline_swaps import _start_auto

STREAM = "[id^=auto-status-stream-]"


@pytest.mark.browser
def test_add_cycles_during_a_run_keeps_the_tab_following_it(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    # Add cycles publishes a status event, so the refresh it causes is in
    # flight when the click's own response replaces #auto-status. Landing on
    # the replaced panel threw inside htmx and lost the stream element.
    app = live_server(["success", "sleep"], tmp_path)
    second = app["session_ids"][1]
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _start_auto(page, app["base_url"], app["session_ids"], "Add cycles race")
    expect(page.locator(f"section.live-round#round-{second}-1")).to_be_visible(
        timeout=30_000
    )
    status = page.locator("#auto-status")
    expect(status).to_contain_text("up to 3 cycles")

    extend = status.locator("details.auto-extend")
    extend.locator("summary").click()
    extend.get_by_role("button", name="Add cycles").click()
    expect(status).to_contain_text("up to 4 cycles")

    # Another client raises the limit; only a tab still hearing the run
    # shows it.
    response = page.request.post(
        f"{app['base_url']}/projects/Verify/auto-runs/1/cycles",
        form={"additional_cycles": "1"},
    )
    assert response.ok, response.text()
    expect(status).to_contain_text("up to 5 cycles", timeout=10_000)
    expect(page.locator(STREAM)).to_have_count(1)
    assert errors == []


@pytest.mark.browser
def test_a_refused_reconnect_does_not_leave_the_tab_deaf(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    # A stream that ends reconnects on the same EventSource. A refused
    # reconnect closes it for good; sse.js then opens a new one, but htmx
    # kept the hx-trigger="sse:..." listeners on the closed one.
    app = live_server(["success", "success", "sleep"], tmp_path, delay=2)
    first, second = app["session_ids"]
    attempts: list[int] = []

    def flaky_stream(route: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            # Ends at once; the browser retries after 200 ms.
            route.fulfill(
                status=200,
                content_type="text/event-stream",
                body="retry: 200\n\n",
            )
        elif len(attempts) == 2:
            route.fulfill(status=503, body="unavailable")
        else:
            route.continue_()

    page.route("**/auto-runs/1/stream", flaky_stream)
    _start_auto(page, app["base_url"], app["session_ids"], "Refused reconnect")

    expect(page.locator(f"section.live-round#round-{second}-1")).to_be_visible(
        timeout=30_000
    )
    # Recovered, and it stays recovered: later status swaps preserve the
    # new stream element without setting it up again, so the tab keeps
    # hearing every turn after this one.
    expect(page.locator(f"section.live-round#round-{first}-2")).to_be_visible(
        timeout=30_000
    )
    response = page.request.post(
        f"{app['base_url']}/projects/Verify/auto-runs/1/cycles",
        form={"additional_cycles": "1"},
    )
    assert response.ok, response.text()
    expect(page.locator("#auto-status")).to_contain_text(
        "up to 4 cycles", timeout=10_000
    )
    assert len(attempts) == 3
    expect(page.locator(STREAM)).to_have_count(1)
