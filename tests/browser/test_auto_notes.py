"""A note typed in the Chat box reaches the running Auto run."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import pytest
from playwright.sync_api import expect

from app.notes import NoteStore
from tests.browser.conftest import LiveServer

AUTO_DIALOG = "[data-auto-setup-dialog]"


@pytest.mark.browser
def test_a_note_opens_with_the_run_posts_and_clears(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    note_box = page.locator("#auto-notes textarea")
    expect(note_box).to_be_disabled()
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Notes")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")

    expect(page.locator('#auto-status[data-auto-notes-open="true"]')).to_have_count(1, timeout=10_000)
    expect(note_box).to_be_enabled()
    note_box.fill("Prefer SQLite")
    page.get_by_role("button", name="Send note to agents").click()

    expect(page.locator("#auto-notes-list")).to_contain_text("N1 · pending")
    expect(page.locator("#auto-notes-list")).to_contain_text("delivered to 0/2")
    expect(note_box).to_have_value("")
    [note] = NoteStore(app["store"].root).load().notes
    assert (note.text, note.auto_run) == ("Prefer SQLite", 1)


@pytest.mark.browser
def test_a_status_rendered_before_the_note_cannot_blank_the_list(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    status_url = f"{app['base_url']}/projects/Verify/auto-runs/1"
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Notes")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-notes-open="true"]')).to_have_count(1, timeout=10_000)
    # A status fragment rendered before the note exists, as an in-flight
    # request would be when the post lands first.
    stale = page.request.get(status_url).text()
    assert 'data-notes-revision="1"' in stale

    page.locator("#auto-notes textarea").fill("Prefer SQLite")
    page.get_by_role("button", name="Send note to agents").click()
    expect(page.locator("#auto-notes-list")).to_contain_text("N1 · pending")

    page.route(status_url, lambda route: route.fulfill(status=200, content_type="text/html", body=stale))
    page.evaluate(
        "url => htmx.ajax('GET', url, {target: '#auto-status', swap: 'outerHTML'})",
        status_url,
    )
    # The stale status itself was applied; only its older list was refused.
    expect(page.locator("#auto-status")).to_have_count(1)
    page.wait_for_timeout(500)
    expect(page.locator("#auto-notes-list")).to_contain_text("N1 · pending")


@pytest.mark.browser
def test_a_status_swap_during_the_post_cannot_hide_the_new_note(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    status_url = f"{app['base_url']}/projects/Verify/auto-runs/1"
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Notes")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-notes-open="true"]')).to_have_count(1, timeout=10_000)

    # Hold the post while a real status refresh replaces the list it targeted.
    def hold_post(route: Any) -> None:
        time.sleep(1.0)
        route.continue_()

    page.route("**/auto-notes", hold_post)
    page.evaluate(
        "url => setTimeout(() => htmx.ajax('GET', url, {target: '#auto-status', swap: 'outerHTML'}), 200)",
        status_url,
    )
    page.locator("#auto-notes textarea").fill("Prefer SQLite")
    page.get_by_role("button", name="Send note to agents").click()

    expect(page.locator("#auto-notes-list")).to_contain_text("N1 · pending", timeout=5_000)
    assert [note.text for note in NoteStore(app["store"].root).load().notes] == ["Prefer SQLite"]


@pytest.mark.browser
def test_the_notes_box_sits_under_the_composer_beside_the_auto_status(
    live_server: LiveServer,
    page: Any,
    tmp_path: Path,
) -> None:
    app = live_server(["sleep"], tmp_path)
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(f"{app['base_url']}/projects/Verify/chat?auto_setup=1")
    expect(page.locator(AUTO_DIALOG)).to_be_visible(timeout=10_000)
    page.fill(f"{AUTO_DIALOG} [name=topic]", "Layout")
    page.click(f"{AUTO_DIALOG} form button[type=submit]")
    expect(page.locator('#auto-status[data-auto-notes-open="true"]')).to_have_count(1, timeout=10_000)

    box = page.evaluate(
        """() => {
          const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
          const composer = rect("#chat-composer");
          const notes = rect("#auto-notes");
          const panel = rect("#auto-panel");
          const top = rect("[data-conversation-top]");
          const form = rect("#auto-notes form");
          const label = rect("#auto-notes label");
          const input = rect("#auto-notes textarea");
          const send = rect("#auto-notes button");
          const lines = (selector) => {
            const range = document.createRange();
            range.selectNodeContents(document.querySelector(selector));
            return new Set(Array.from(range.getClientRects(), (line) => Math.round(line.top))).size;
          };
          return {
            messageFromTop: rect("#chat-composer label").top - top.top,
            agentAfterMessage: rect(".composer-agent-meta").left - rect("#chat-composer label").right,
            agentOffLine: Math.abs(
              (rect(".composer-agent-meta").top + rect(".composer-agent-meta").bottom) / 2
              - (rect("#chat-composer label").top + rect("#chat-composer label").bottom) / 2
            ),
            agentSize: getComputedStyle(document.querySelector(".composer-agent-meta")).fontSize,
            messageToBox: rect("#chat-composer textarea").top - rect("#chat-composer label").bottom,
            gapAbove: notes.top - composer.bottom,
            leftOfPanel: panel.left - notes.right,
            sameColumn: Math.abs(notes.left - composer.left),
            panelSpansBoth: panel.bottom - notes.bottom,
            insideTop: top.bottom - notes.bottom,
            questionsGap: rect("#questions-panel").top - top.bottom,
            formHeight: form.height,
            tallest: Math.max(label.height, input.height, send.height),
            hintBelow: rect("#auto-notes .hint").top - Math.max(input.bottom, send.bottom),
            hintHeight: rect("#auto-notes .hint").height,
            hintLeft: rect("#auto-notes .hint").left - label.left,
            labelThenInput: input.left - label.right,
            inputThenSend: send.left - input.right,
            inputHeight: input.height,
            sendHeight: send.height,
            hintLines: lines("#auto-notes .hint"),
            labelHeight: label.height,
            hintSize: getComputedStyle(document.querySelector("#auto-notes .hint")).fontSize,
          };
        }"""
    )
    # Like the message box: the left column under the composer, one .75rem
    # (12px) gap, with the Auto status to its right spanning both.
    assert 0 <= box["gapAbove"] <= 12.5, box
    # "Message" and the small "sending as ..." share the first line, at the
    # row top, just above the box.
    assert box["messageFromTop"] <= 0.5, box
    assert box["agentAfterMessage"] >= 0, box
    assert box["agentOffLine"] <= 4, box
    assert box["agentSize"] == "12px", box
    assert 0 <= box["messageToBox"] <= 4.5, box
    assert box["leftOfPanel"] >= 0, box
    assert box["sameColumn"] <= 0.5, box
    assert box["panelSpansBoth"] >= -0.5, box
    assert box["insideTop"] >= -0.5, box
    assert box["questionsGap"] <= 12.5, box
    # One row: "Note", a one-line box, then Send, left to right; the small
    # hint on the line below, just under the row.
    assert 0 <= box["hintBelow"] <= 4.5, box
    assert abs(box["hintLeft"]) <= 0.5, box
    assert box["formHeight"] <= box["tallest"] + box["hintBelow"] + box["hintHeight"] + 0.5, box
    assert box["labelThenInput"] >= 0 and box["inputThenSend"] >= 0, box
    assert box["inputHeight"] <= 2 * box["sendHeight"], box
    assert box["hintLines"] == 1, box
    # One line at the page's 1.5 line height is ~24px; two would be ~48px.
    assert box["labelHeight"] < 1.6 * box["sendHeight"], box
    assert box["hintSize"] == "12px", box
