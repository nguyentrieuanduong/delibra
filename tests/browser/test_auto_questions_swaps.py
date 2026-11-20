"""What the browser does with the Questions for you panel's live swaps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from app.questions import ProposedQuestion, QuestionStore
from tests.browser.conftest import LiveServer
from tests.browser.test_auto_timeline_swaps import _refresh_status, _start_auto


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


# Counts the questions span's requests from page load: htmx fires
# beforeRequest when a request is sent (a queued one fires it only once it
# is sent) and afterRequest when it ends, whatever the outcome.
COUNT_QUESTION_FETCHES = """
window.questionFetches = 0;
window.questionFetchesInFlight = 0;
const isQuestionFetch = event => event.detail.elt.matches("[hx-get$='/questions/new']");
document.addEventListener("htmx:beforeRequest", event => {
  if (isQuestionFetch(event)) { window.questionFetches += 1; window.questionFetchesInFlight += 1; }
});
document.addEventListener("htmx:afterRequest", event => {
  if (isQuestionFetch(event)) { window.questionFetchesInFlight -= 1; }
});
"""

# A "questions" message on the run's own EventSource, delivered to the
# listeners htmx registered there exactly as a server-sent one would be.
# htmx sends (or queues) the fetch synchronously, so the count it returns
# already includes a fetch this message sent.
SSE_QUESTIONS = """() => {
  const stream = document.querySelector("#auto-status [sse-connect]");
  stream["htmx-internal-data"].sseEventSource.dispatchEvent(new MessageEvent("questions", {data: ""}));
  return window.questionFetches;
}"""


@pytest.mark.browser
def test_a_status_swap_during_a_questions_fetch_cannot_add_the_question_twice(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    """The hidden SSE spans sit in the hx-preserve'd stream, so htmx processes
    them again when an #auto-status swap settles. While a span's own request
    is in flight it used to carry the htmx-request class; htmx 1.9 then saw a
    new attribute hash, re-initialised it and deleted its request lock, so the
    next event sent a second fetch with the same cursor and both responses
    inserted Q1 (seen under parallel load, 2026-11-19). The spans now put that
    class on an indicator outside the preserved stream."""

    app = live_server(["sleep"], tmp_path)
    page.add_init_script(COUNT_QUESTION_FETCHES)
    _start_auto(page, app["base_url"], [app["session_ids"][0]], "Questions")
    expect(page.locator('#auto-status[data-auto-active="true"]')).to_have_count(1, timeout=10_000)
    page.wait_for_function("() => window.questionFetchesInFlight === 0")
    _ask(QuestionStore(app["store"].root, 96 * 1024), ["Asked once?"])

    held: list[Any] = []
    holding = [True]

    def hold(route: Any) -> None:
        if holding[0]:
            held.append(route)
        else:
            route.continue_()

    page.route("**/questions/new*", hold)
    # A fetch sent from now on is held. If one of the run's own events got in
    # first, this message is queued behind it and is sent when that lands.
    with page.expect_request(lambda request: "/questions/new" in request.url):
        page.evaluate(SSE_QUESTIONS)
    sent = page.evaluate("() => window.questionFetches")
    _refresh_status(page)  # settles while that fetch is held in flight
    # With its lock intact, htmx queues this one instead of sending it.
    assert page.evaluate(SSE_QUESTIONS) == sent, "a second fetch went out while the first was in flight"

    holding[0] = False
    for route in held:
        route.continue_()
    # The queued fetch goes out once the held one lands; wait for both to end.
    page.wait_for_function(
        "count => window.questionFetches > count && window.questionFetchesInFlight === 0",
        arg=sent,
    )
    expect(page.locator("#questions-cursor")).to_have_value("1")
    expect(page.locator("#question-Q1")).to_have_count(1)
