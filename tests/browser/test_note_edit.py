"""Edit an unread note in a dialog while every agent is idle (design 2026-11-13, Part B)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from app.notes import NoteStore, chat_note_store
from tests.browser.conftest import LiveServer
from tests.test_auto import seed_durable_auto

DIALOG = "#note-edit-dialog"
FIELD = "#note-edit-text"
AUTO_LIST = "#auto-notes-list"


def _mark_running(store: Any, session_id: str) -> None:
    session = store.load_session(session_id)
    session.status = "running"
    store.save_session(session)


@pytest.mark.browser
def test_an_unread_auto_note_is_edited_and_a_refusal_keeps_the_dialog(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    store = app["store"]
    alpha, beta = app["session_ids"]
    record = seed_durable_auto(store, auto_id="b" * 32, status="stopped", publish=False)
    notes = NoteStore(store.root)
    notes.add("Prefer SQLite", auto_run=record.number)
    notes.add("Already read", auto_run=record.number)
    notes.record_delivery(record.number, alpha, ["N2"], {alpha, beta})
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")

    # Only the unread note can be edited.
    expect(page.locator(f"{AUTO_LIST} [aria-label='Edit N1']")).to_be_visible(timeout=10_000)
    expect(page.locator(f"{AUTO_LIST} [aria-label='Edit N2']")).to_have_count(0)

    page.click(f"{AUTO_LIST} [aria-label='Edit N1']")
    expect(page.locator(DIALOG)).to_be_visible()
    expect(page.locator(FIELD)).to_have_value("Prefer SQLite")
    page.fill(FIELD, "Prefer Postgres")
    page.click(f"{DIALOG} button[type=submit]")
    expect(page.locator(DIALOG)).to_be_hidden(timeout=5_000)
    expect(page.locator(AUTO_LIST)).to_contain_text("Prefer Postgres")
    assert "Prefer Postgres" in notes.view_path.read_text(encoding="utf-8")

    # An agent starts after the list was drawn: the save is refused with a
    # pop-up, and the dialog keeps what was typed.
    page.click(f"{AUTO_LIST} [aria-label='Edit N1']")
    page.fill(FIELD, "Late change")
    _mark_running(store, beta)
    alerts: list[str] = []
    page.once("dialog", lambda dialog: (alerts.append(dialog.message), dialog.accept()))
    page.click(f"{DIALOG} button[type=submit]")
    for _ in range(50):
        if alerts:
            break
        page.wait_for_timeout(100)
    assert alerts == ["Notes can be edited only while no agent is running"]
    expect(page.locator(DIALOG)).to_be_visible()
    expect(page.locator(FIELD)).to_have_value("Late change")
    assert notes.load().find("N1").text == "Prefer Postgres"

    # A real out-of-band notes list arrives while the dialog is open (a note
    # posted elsewhere): the dialog and its text survive.
    page.evaluate(
        """url => htmx.ajax('POST', url, {values: {text: 'Posted elsewhere'}, swap: 'none'})""",
        "/projects/Verify/auto-notes",
    )
    expect(page.locator(AUTO_LIST)).to_contain_text("Posted elsewhere", timeout=5_000)
    expect(page.locator(DIALOG)).to_be_visible()
    expect(page.locator(FIELD)).to_have_value("Late change")
    page.click(f"{DIALOG} [data-note-edit-cancel]")
    expect(page.locator(DIALOG)).to_be_hidden()


@pytest.mark.browser
def test_an_unread_chat_note_is_edited_for_an_idle_agent(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    store = app["store"]
    alpha = app["session_ids"][0]
    chat_notes = chat_note_store(store, alpha)
    chat_notes.add("Use tables", name="Alpha")
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")

    page.click("#chat-notes-list [aria-label='Edit N1']")
    expect(page.locator(FIELD)).to_have_value("Use tables")
    page.fill(FIELD, "Use lists")
    page.click(f"{DIALOG} button[type=submit]")
    expect(page.locator(DIALOG)).to_be_hidden(timeout=5_000)
    expect(page.locator("#chat-notes-list")).to_contain_text("Use lists")
    assert chat_notes.load().find("N1").text == "Use lists"
