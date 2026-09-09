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


@pytest.mark.browser
def test_the_auto_status_swap_does_not_close_its_event_source(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """#auto-status is swapped outerHTML on every status event, and the SSE
    connection lives inside it under hx-preserve (_auto_status.html:104).
    If htmx cleans that node instead of moving it, every later event is
    lost and the run goes silent -- the shape of the reported bug."""

    app = live_server(["success", "sleep"], tmp_path)
    second = app["session_ids"][1]
    opened: list[str] = []
    page.expose_function("recordEventSource", lambda url: opened.append(url))
    # `class ... extends`, not a plain function. app/static/sse.js:252 reads
    # the *static* EventSource.CLOSED to decide whether to reconnect; a
    # plain function has no such property, so `readyState === undefined` is
    # never true and instrumenting the page would silently disable
    # reconnection -- changing the very behaviour under measurement.
    page.add_init_script(
        """
        const Native = window.EventSource;
        window.EventSource = class extends Native {
          constructor(url, config) {
            super(url, config);
            window.recordEventSource(String(url));
          }
        };
        """
    )
    _start_auto(page, app["base_url"], app["session_ids"], "Preserved stream")

    # Prove the instrumentation is transparent before trusting what it
    # recorded.
    assert page.evaluate("window.EventSource.CLOSED") == 2

    expect(page.locator(f"section.live-round#round-{second}-1")).to_be_visible(
        timeout=30_000
    )

    # Reaching round 2 means later events arrived, so the stream survived
    # the first #auto-status swap. Pin the cause: exactly one Auto stream
    # was ever opened, i.e. it was moved, not closed and reconnected.
    auto_streams = [url for url in opened if "/auto-runs/" in url]
    assert len(auto_streams) == 1, auto_streams


@pytest.mark.browser
def test_the_composer_locks_while_its_agent_runs_and_unlocks_after(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """d3fd0cf gave the composer a single owner for its disabled state.

    The observable contract is that the gate tracks the card at both ends
    of a run. A delay long enough to observe "running" but short enough to
    finish keeps both halves in one test: the start arrives synchronously
    in the POST response (runs.py returns _live.html with agent_card_oob),
    and the finish arrives over sse:done.

    _agent_card_oob.html:3-5 swaps two independent targets, so both are
    asserted here. dc332be gave *both* stable ids and 82396e0 fixed the
    sidebar showing `running` and `Running…` after completion; a test that
    watched only the status span would leave half of each covered by
    nothing.
    """

    app = live_server(["success"], tmp_path, delay=3.0)
    first = app["session_ids"][0]
    # Assert the attribute app.js actually reads (_agent_status.html:5),
    # not the rendered label -- the label is presentation and can be
    # reworded without changing the contract.
    status = f"#agent-status-{first}"
    preview = f"#agent-preview-{first} p.agent-preview"
    prompt = "#chat-composer [name=prompt]"
    send = "#chat-composer button[type=submit]"
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={first}")
    expect(page.locator(status)).to_have_attribute(
        "data-agent-status", "idle", timeout=10_000
    )
    expect(page.locator(prompt)).to_be_enabled()

    page.fill(prompt, "first prompt")
    page.click(send)

    expect(page.locator(f"section.live-round#round-{first}-1")).to_be_visible(
        timeout=30_000
    )
    expect(page.locator(status)).to_have_attribute(
        "data-agent-status", "running", timeout=30_000
    )
    # app/views.py:253-255 is the only producer of this pair.
    expect(page.locator(f"{preview}.running")).to_have_text("Running…")
    expect(page.locator(prompt)).to_be_disabled()
    expect(page.locator(send)).to_be_disabled()

    # The other half of the gate, and the only browser coverage of a manual
    # run completing (82396e0): both card fragments must leave `running`
    # and the composer must become usable again without a reload.
    expect(page.locator(status)).to_have_attribute(
        "data-agent-status", "idle", timeout=30_000
    )
    expect(page.locator(f"article.round#round-{first}-1")).to_be_visible()
    expect(page.locator(f"{preview}.running")).to_have_count(0)
    # tests/fake_cli.py:124 emits result text "Hello".
    expect(page.locator(preview)).to_contain_text("Hello")
    expect(page.locator(prompt)).to_be_enabled()


@pytest.mark.browser
def test_another_agents_card_refresh_leaves_a_half_typed_draft_alone(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """d3fd0cf: toggling in place "is what lets a half-typed draft survive
    a card refresh" (its commit body).

    Beta runs while Alpha is selected. Beta's completion swaps Beta's two
    card fragments out of band, targeted at Beta's live section -- the
    composer node is never in the swap, and syncComposerAvailability finds
    Alpha idle and leaves it enabled. If anything ever re-renders the
    composer on a foreign card refresh, the draft dies and this fails.
    """

    app = live_server(["sleep"], tmp_path)
    alpha, beta = app["session_ids"]
    base = app["base_url"]
    prompt = "#chat-composer [name=prompt]"
    beta_status = f"#agent-status-{beta}"
    beta_preview = f"#agent-preview-{beta} p.agent-preview"

    # Start Beta from Beta's own composer: the composer always posts as the
    # selected agent (_composer.html:8), so there is no way to start a
    # foreign run from Alpha's page.
    page.goto(f"{base}/projects/Verify/chat?agent={beta}")
    page.fill(prompt, "beta prompt")
    page.click("#chat-composer button[type=submit]")
    expect(page.locator(beta_status)).to_have_attribute(
        "data-agent-status", "running", timeout=30_000
    )

    # Switch to Alpha. A full load here is setup, not the measurement --
    # every assertion below happens without another navigation.
    page.goto(f"{base}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(prompt)).to_be_enabled()
    page.fill(prompt, "half typed")

    # Non-vacuity guard. If Beta had already finished, its card would have
    # swapped before the draft existed and every assertion below would hold
    # for the wrong reason. Fail here instead.
    expect(page.locator(beta_status)).to_have_attribute(
        "data-agent-status", "running"
    )
    expect(page.locator(f"{beta_preview}.running")).to_have_count(1)

    # Real htmx, real button: _live.html:26-32 hx-posts /cancel. Ending the
    # round on demand is what makes this test independent of --delay.
    page.click(f"#round-{beta}-1 button[hx-post$='/cancel']")

    # Both of Beta's OOB targets must land before the draft is judged.
    expect(page.locator(beta_status)).not_to_have_attribute(
        "data-agent-status", "running", timeout=30_000
    )
    expect(page.locator(f"{beta_preview}.running")).to_have_count(0)

    expect(page.locator(prompt)).to_have_value("half typed")
    expect(page.locator(prompt)).to_be_enabled()


@pytest.mark.browser
def test_stopping_auto_refreshes_every_participant_card_without_a_reload(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """A terminal Auto response owns the last sidebar refresh.

    Reloading while Beta is held open establishes a real server-rendered
    `running` card. The Stop response then reaches the browser through
    htmx and must replace both OOB card fragments for every participant.
    """

    app = live_server(["success", "sleep"], tmp_path)
    alpha, beta = app["session_ids"]
    base = app["base_url"]
    _start_auto(page, base, [alpha, beta], "Terminal participant cards")
    expect(page.locator(f"section.live-round#round-{beta}-1")).to_be_visible(
        timeout=30_000
    )

    # Setup only: the reload makes Beta's persisted running state visible
    # in the card. There is no navigation after the Stop click below.
    page.reload()
    beta_status = f"#agent-status-{beta}"
    beta_preview = f"#agent-preview-{beta} p.agent-preview"
    expect(page.locator(beta_status)).to_have_attribute(
        "data-agent-status", "running", timeout=10_000
    )
    expect(page.locator(f"{beta_preview}.running")).to_have_count(1)

    page.click("#auto-status form[hx-post$='/stop'] button[type=submit]")

    expect(page.locator("#auto-status")).to_have_attribute(
        "data-auto-active", "false", timeout=30_000
    )
    expect(page.locator("#auto-history-status-1")).to_contain_text("stopped")
    for session_id in (alpha, beta):
        expect(page.locator(f"#agent-status-{session_id}")).not_to_have_attribute(
            "data-agent-status", "running"
        )
        expect(
            page.locator(f"#agent-preview-{session_id} p.agent-preview.running")
        ).to_have_count(0)
