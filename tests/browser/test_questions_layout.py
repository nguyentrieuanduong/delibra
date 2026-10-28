"""The Questions panel stays compact."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from app.questions import ProposedQuestion, QuestionStore
from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_an_answer_box_is_two_lines_tall(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    QuestionStore(app["store"].root, 96 * 1024).add(
        [ProposedQuestion("Which database?", "SQLite", False)],
        asked_by=app["session_ids"][0],
        asked_by_name="Alpha",
        auto_run=None,
        cycle=None,
        round_n=1,
    )
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    answer = page.locator("#questions-panel textarea[name=answer]")
    expect(answer).to_be_visible(timeout=10_000)

    box = answer.evaluate(
        """(element) => ({
          height: element.clientHeight,
          font: parseFloat(getComputedStyle(element).fontSize),
          rows: element.rows,
        })"""
    )
    # Two lines at the default ~1.15 line height plus padding; three would
    # exceed the upper bound.
    assert box["rows"] == 2, box
    assert 2 * box["font"] <= box["height"] <= 2.6 * box["font"] + 4, box


@pytest.mark.browser
def test_an_expanded_panel_starts_its_first_question_close_and_caps_its_height(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    QuestionStore(app["store"].root, 96 * 1024).add(
        [ProposedQuestion("Which database?", "SQLite", False)],
        asked_by=app["session_ids"][0],
        asked_by_name="Alpha",
        auto_run=None,
        cycle=None,
        round_n=1,
    )
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    expect(page.locator("#questions-panel .question-meta")).to_be_visible(timeout=10_000)

    box = page.evaluate(
        """() => {
          const item = document.querySelector("#questions-panel .question");
          const meta = document.querySelector("#questions-panel .question-meta");
          const style = getComputedStyle(item);
          return {
            metaGap: meta.getBoundingClientRect().top - item.getBoundingClientRect().top,
            itemTop: parseFloat(style.borderTopWidth) + parseFloat(style.paddingTop),
            listCap: getComputedStyle(document.querySelector(".questions-list")).maxHeight,
          };
        }"""
    )
    # Only the item's own border and padding above "Q1 · Alpha · open".
    assert box["metaGap"] <= box["itemTop"] + 0.5, box
    # 40vh, then 32vh, then 25% lower again at the user's request: 24vh of a
    # 1000px window, however many questions the list holds.
    assert box["listCap"] == "240px", box


@pytest.mark.browser
def test_an_open_question_is_compact(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["success"], tmp_path)
    QuestionStore(app["store"].root, 96 * 1024).add(
        [ProposedQuestion("Which database should the prototype use?", "SQLite", False)],
        asked_by=app["session_ids"][0],
        asked_by_name="Alpha",
        auto_run=None,
        cycle=None,
        round_n=1,
    )
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    expect(page.locator("#questions-panel .question-actions")).to_be_visible(timeout=10_000)

    box = page.evaluate(
        """() => {
          const rect = (element) => element.getBoundingClientRect();
          const item = document.querySelector("#questions-panel .question");
          const actions = item.querySelector(".question-actions");
          const controls = Array.from(actions.querySelectorAll("button, input"));
          // The "Duplicate of" text is one 24px line at the page's 1.5 line height.
          const labels = Array.from(actions.querySelectorAll("label"));
          return {
            item: rect(item).height,
            actions: rect(actions).height,
            tallestControl: Math.max(...[...controls, ...labels].map((element) => rect(element).height)),
            centres: controls.map((control) => (rect(control).top + rect(control).bottom) / 2),
          };
        }"""
    )
    # Every action, the duplicate box included, on one line at its own height.
    assert max(box["centres"]) - min(box["centres"]) <= 1, box
    assert box["actions"] <= box["tallestControl"] + 0.5, box
    # At least 20% below the 372px an open question took at 1400x1000.
    assert box["item"] <= 0.8 * 372, box
