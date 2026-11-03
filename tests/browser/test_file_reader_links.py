"""A link in a file opened in the reader opens the linked file (bug 2026-11-03)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_a_relative_link_in_the_reader_opens_the_file_beside_it(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    plans = tmp_path / "project" / ".plans"
    (plans / "source-knowledge-repository").mkdir(parents=True)
    # The link sits far down, so following it starts with the reader scrolled.
    (plans / "plan.md").write_text(
        "".join(f"Paragraph {n}.\n\n" for n in range(200))
        + "| Gói | Việc |\n|---|---|\n| [01](source-knowledge-repository/01-foundation-contracts.md) | Kiểm kê |\n"
    )
    (plans / "source-knowledge-repository" / "01-foundation-contracts.md").write_text(
        "# Foundation contracts\n"
    )
    chat = f"{app['base_url']}/projects/Verify/chat"
    page.goto(chat)
    page.evaluate(
        "(url) => htmx.ajax('GET', url, '#file-reader')",
        f"{app['base_url']}/projects/Verify/files/view?path=.plans/plan.md",
    )
    link = page.locator("#file-reader a", has_text="01")
    link.scroll_into_view_if_needed(timeout=10_000)
    assert page.evaluate("document.getElementById('file-reader').scrollTop") > 500
    link.click()

    expect(page.locator("#file-reader h3")).to_have_text(
        ".plans/source-knowledge-repository/01-foundation-contracts.md", timeout=10_000
    )
    expect(page.locator("#file-reader h1")).to_have_text("Foundation contracts")
    # The link swapped the reader; the page itself did not navigate.
    assert page.url == chat
    # A different file starts at its top, not at the old file's scroll position.
    assert page.evaluate("document.getElementById('file-reader').scrollTop") == 0
