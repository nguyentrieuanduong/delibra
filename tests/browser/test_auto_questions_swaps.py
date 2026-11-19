"""What the browser does with the Questions for you panel's live swaps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from app.questions import ProposedQuestion, QuestionStore
from tests.browser.conftest import LiveServer


def _ask(questions: QuestionStore, texts: list[str]) -> None:
    questions.add(
        [ProposedQuestion(text, "", False) for text in texts],
        asked_by="1" * 32,
        asked_by_name="Alpha",
        auto_run=None,
        cycle=None,
        round_n=1,
    )


@pytest.mark.browser
def test_appended_questions_update_the_cap_warning_and_keep_a_draft(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    # The fixture's Settings keep the default 96 KiB view limit.
    questions = QuestionStore(app["store"].root, 96 * 1024)
    _ask(questions, ["First question?"])
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    draft = page.locator("#question-Q1 textarea[name=answer]")
    expect(draft).to_be_visible(timeout=10_000)
    draft.fill("unsaved draft")
    expect(page.locator("#questions-omitted")).not_to_contain_text("not shown to agents")

    # 50 questions of ~2 KB push the view past 96 KiB; Q1 is the oldest
    # open question, so it is the first one left out.
    for batch in range(2):
        _ask(questions, [f"{batch}-{index}:" + "x" * 1950 for index in range(25)])
    # The same request the SSE trigger in _auto_status.html makes. No
    # reload between here and the assertions.
    page.evaluate(
        """url => htmx.ajax("GET", url, {target: "#questions-list", swap: "afterbegin"})""",
        f"{app['base_url']}/projects/Verify/questions/new?after=1",
    )

    expect(page.locator("#question-Q51")).to_be_visible(timeout=10_000)
    expect(page.locator("#questions-omitted")).to_contain_text("not shown to agents")
    expect(page.locator("#questions-omitted")).to_contain_text("Q1,")
    expect(page.locator("#questions-cursor")).to_have_value("51")
    expect(draft).to_have_value("unsaved draft")


# A "questions" message on the run's own EventSource, delivered to the
# listeners htmx registered there, exactly as a server-sent one would be.
SSE_QUESTIONS = """() => {
  const stream = document.querySelector("#auto-status [sse-connect]");
  stream["htmx-internal-data"].sseEventSource.dispatchEvent(new MessageEvent("questions", {data: ""}));
}"""


@pytest.mark.browser
def test_a_status_swap_during_a_questions_fetch_cannot_add_the_question_twice(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """The hidden SSE spans sit in the hx-preserve'd stream, so htmx processes
    them again when an #auto-status swap settles. While a span's own request
    is in flight it carries the htmx-request class; htmx 1.9 then sees a new
    attribute hash, re-initialises it and deletes its request lock, so the
    next event sends a second fetch with the same cursor and both responses
    insert Q1 (seen under parallel load, 2026-11-19). The spans now put that
    class on an indicator outside the preserved stream."""

    app = live_server(["sleep"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    dialog = "[data-auto-setup-dialog]"
    expect(page.locator(dialog)).to_be_visible(timeout=10_000)
    page.fill(f"{dialog} [name=topic]", "Questions")
    page.click(f"{dialog} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-active="true"]')).to_have_count(1, timeout=10_000)
    # Let the run's own start-up events and fetches finish first.
    page.wait_for_timeout(1_000)
    page.wait_for_function("() => !document.querySelector('.htmx-request')")
    _ask(QuestionStore(app["store"].root, 96 * 1024), ["Asked once?"])

    held: list[Any] = []
    holding = [True]

    def hold(route: Any) -> None:
        if holding[0]:
            held.append(route)
        else:
            route.continue_()

    page.route("**/questions/new*", hold)
    page.evaluate(SSE_QUESTIONS)
    for _ in range(100):  # the first fetch reaches the route and is held there
        if held:
            break
        page.wait_for_timeout(50)
    assert len(held) == 1
    # A status refresh, as sse:status sends, settles while the fetch is held.
    page.evaluate(
        """() => {
          window.statusBeforeRefresh = document.getElementById("auto-status");
          const refresh = document.querySelector("#auto-status span[hx-get][hx-target='#auto-status']");
          htmx.ajax("GET", refresh.getAttribute("hx-get"), {target: "#auto-status", swap: "outerHTML"});
        }"""
    )
    page.wait_for_function(
        "() => document.getElementById('auto-status') !== window.statusBeforeRefresh"
        " && !document.querySelector('.htmx-settling')"
    )
    page.evaluate(SSE_QUESTIONS)
    page.wait_for_timeout(300)
    in_flight = len(held)

    holding[0] = False
    for route in held:
        route.continue_()
    expect(page.locator("#questions-cursor")).to_have_value("1", timeout=10_000)
    page.wait_for_timeout(500)  # a second, queued fetch has time to land
    expect(page.locator("#question-Q1")).to_have_count(1)
    assert in_flight == 1, "a second fetch went out while the first was in flight"
