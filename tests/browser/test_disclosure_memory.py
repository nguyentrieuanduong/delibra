"""Remembered Agent information and message disclosures (user, 2026-11-13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'


def _send(page: Any, text: str, round_id: str) -> None:
    page.fill(PROMPT, text)
    page.click("#chat-composer button[type=submit]")
    expect(page.locator(f"article.round#{round_id}")).to_be_visible(timeout=15_000)
    expect(page.locator(PROMPT)).to_be_enabled(timeout=15_000)


def _card(session_id: str) -> str:
    return f'.agent-card[data-session-id="{session_id}"]'


def _info_open(page: Any, session_id: str) -> bool:
    return page.evaluate(
        "(sel) => document.querySelector(sel + ' .agent-details').open",
        _card(session_id),
    )


def _message_open(page: Any, round_id: str) -> bool:
    return page.evaluate(
        "(id) => document.querySelector('#' + id + ' .round-details').open", round_id
    )


@pytest.mark.browser
def test_agent_information_opens_only_when_the_reader_opens_it(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha, beta = app["session_ids"]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert not _info_open(page, alpha)

    page.click(f"#agent-select-{beta}")
    expect(page.locator(f"#agent-select-{beta}[aria-current=true]")).to_be_visible()
    assert not _info_open(page, beta)

    page.click(f"{_card(alpha)} .agent-details > summary")
    page.click(f"#agent-select-{alpha}")
    expect(page.locator(f"#agent-select-{alpha}[aria-current=true]")).to_be_visible()
    assert _info_open(page, alpha)
    assert not _info_open(page, beta)

    page.click(f"{_card(alpha)} .agent-details details > summary")
    page.click(f"{_card(alpha)} form[data-session-config-form] button[type=submit]")
    expect(page.locator(f"{_card(alpha)} .agent-details details[open]")).to_have_count(0)
    assert _info_open(page, alpha)

    page.reload()
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert _info_open(page, alpha)

    page.click(f"{_card(alpha)} .agent-details > summary")
    page.reload()
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert not _info_open(page, alpha)


@pytest.mark.browser
def test_an_opened_message_stays_open_through_new_rounds_and_reload(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    first, second, third = (f"round-{alpha}-{n}" for n in (1, 2, 3))
    _send(page, "Message 1", first)
    _send(page, "Message 2", second)
    assert not _message_open(page, first)

    page.click(f"#{first} .round-details > summary")
    _send(page, "Message 3", third)
    expect(page.locator(f"#{third} .round-details[open]")).to_have_count(1)
    assert _message_open(page, first)
    assert not _message_open(page, second)

    page.reload()
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert [_message_open(page, r) for r in (first, second, third)] == [True, False, True]

    page.click(f"#{third} .round-details > summary")
    page.reload()
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert [_message_open(page, r) for r in (first, second, third)] == [True, False, False]


@pytest.mark.browser
def test_blocked_storage_keeps_the_rendered_state(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha, beta = app["session_ids"]
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """Object.defineProperty(window, "localStorage", {
             configurable: true,
             get() { throw new DOMException("blocked", "SecurityError"); },
           });"""
    )
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    first, second = (f"round-{alpha}-{n}" for n in (1, 2))
    _send(page, "Message 1", first)
    page.click(f"{_card(alpha)} .agent-details > summary")
    page.click(f"#{first} .round-details > summary")
    _send(page, "Message 2", second)

    assert [_message_open(page, r) for r in (first, second)] == [False, True]
    page.click(f"#agent-select-{beta}")
    expect(page.locator(f"#agent-select-{beta}[aria-current=true]")).to_be_visible()
    assert not _info_open(page, alpha)
    assert errors == []


@pytest.mark.browser
def test_collapse_all_closes_every_message_until_a_new_one_arrives(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    rounds = [f"round-{alpha}-{n}" for n in (1, 2, 3, 4)]
    collapse = page.get_by_role("button", name="Collapse all messages")
    expect(collapse).to_be_hidden()
    for n, round_id in enumerate(rounds[:3], start=1):
        _send(page, f"Message {n}", round_id)
    page.click(f"#{rounds[0]} .round-details > summary")
    assert [_message_open(page, r) for r in rounds[:3]] == [True, False, True]

    expect(collapse).to_be_visible()
    collapse.click()
    assert [_message_open(page, r) for r in rounds[:3]] == [False, False, False]

    page.reload()
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    assert [_message_open(page, r) for r in rounds[:3]] == [False, False, False]

    # A new message follows the default: the newest opens, the rest stay closed.
    _send(page, "Message 4", rounds[3])
    expect(page.locator(f"#{rounds[3]} .round-details[open]")).to_have_count(1)
    assert [_message_open(page, r) for r in rounds] == [False, False, False, True]
