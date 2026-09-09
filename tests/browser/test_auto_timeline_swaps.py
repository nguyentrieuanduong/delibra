"""What the browser does with what the server sends.

tests/test_routes_auto.py already proves the fragments are correct. These
prove htmx applies them -- the gap all six shipped UI bugs slipped through.

A failure here is a harness failure until the selectors below are shown to
resolve; step 5.4 establishes that separately, before any behavioural
assertion is trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import FAKE_CLI, LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


def _start_auto(page: Any, base: str, session_ids: list[str], topic: str) -> None:
    """Fill and submit the real Auto form.

    Selectors are read from app/templates/_auto_setup.html: the checkbox is
    singular `participant_id` (:25) and the submit is scoped to the dialog
    (:127), because the page also carries the composer's submit.
    """

    page.goto(f"{base}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", topic)
    for session_id in session_ids:
        page.check(f"{AUTO_DIALOG} input[name=participant_id][value='{session_id}']")
    # prepare_first stays unchecked (_auto_setup.html:33), so the run goes
    # straight to discussion and exercises _run_discussion_attempt.
    page.click(f"{AUTO_DIALOG} form button[type=submit]")


@pytest.mark.browser
def test_the_auto_form_selectors_resolve(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """Harness guard: separates a wrong selector from a product defect."""

    app = live_server(["sleep"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    expect(
        page.locator(f"{AUTO_DIALOG} input[name=participant_id]")
    ).to_have_count(2)
    expect(page.locator(f"{AUTO_DIALOG} form button[type=submit]")).to_be_enabled()
    expect(page.locator("#chat-composer button[type=submit]")).to_have_count(1)


@pytest.mark.browser
def test_a_started_round_installs_its_live_section_without_a_reload(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    # Round 1 finishes, round 2 is held open: the second round is the one
    # revision 1 of the Auto fix could not make visible.
    app = live_server(["success", "sleep"], tmp_path)
    second = app["session_ids"][1]
    _start_auto(page, app["base_url"], app["session_ids"], "Live rounds")
    # No reload between here and the assertion: if the section appears,
    # htmx installed it from an sse:status refresh.
    expect(page.locator(f"section.live-round#round-{second}-1")).to_be_visible(
        timeout=30_000
    )

    # The constraint that matters most in this file: the only provider
    # subprocess Delibra dispatched was the fake CLI. create_app was given
    # non-existent commands, so probe_all and refresh_codex_quota could not
    # reach a real one either.
    assert app["dispatched"], "no provider ran, so the run proved nothing"
    assert all(argv[1] == str(FAKE_CLI) for argv in app["dispatched"]), app[
        "dispatched"
    ]
