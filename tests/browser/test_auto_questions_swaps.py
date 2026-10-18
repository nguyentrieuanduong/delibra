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
