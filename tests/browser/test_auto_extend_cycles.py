"""Add cycles to a running Auto run from the status panel, in real Chromium."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer
from tests.browser.test_auto_timeline_swaps import _start_auto


def _refresh_status(page: Any) -> None:
    """The same GET and swap an sse:status event triggers."""

    page.evaluate(
        """() => {
          window.statusBeforeRefresh = document.getElementById("auto-status");
          const refresh = document.querySelector("#auto-status [hx-target='#auto-status']");
          htmx.ajax("GET", refresh.getAttribute("hx-get"), {
            target: "#auto-status",
            swap: "outerHTML",
          });
        }"""
    )
    page.wait_for_function(
        "() => document.getElementById('auto-status') !== window.statusBeforeRefresh"
    )


@pytest.mark.browser
def test_add_cycles_previews_survives_a_refresh_and_raises_the_limit(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    first = app["session_ids"][0]
    _start_auto(page, app["base_url"], [first], "Add cycles")
    expect(page.locator(f"section.live-round#round-{first}-1")).to_be_visible(timeout=30_000)
    status = page.locator("#auto-status")
    expect(status).to_contain_text("up to 3 cycles")

    extend = page.locator("#auto-status details.auto-extend")
    extend.locator("summary").click()
    expect(extend).to_contain_text("Cycle 1 of 3")
    field = extend.get_by_label("Additional discussion cycles")
    preview = extend.locator("[data-auto-extend-preview]")
    expect(preview).to_have_text("New limit: 4 cycles")
    field.fill("3")
    expect(preview).to_have_text("New limit: 6 cycles")
    field.fill("0")
    expect(preview).to_have_text("Enter 1 to 97 cycles")
    field.fill("3")

    _refresh_status(page)
    expect(extend).to_have_attribute("open", "")
    expect(field).to_have_value("3")
    expect(preview).to_have_text("New limit: 6 cycles")

    extend.get_by_role("button", name="Add cycles").click()
    expect(status).to_contain_text("up to 6 cycles", timeout=10_000)
    expect(extend).not_to_have_attribute("open", "")
    assert app["store"].load_auto_run(app["store"].list_auto_runs()[0].id).max_cycles == 6
