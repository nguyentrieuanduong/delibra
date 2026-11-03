"""Mermaid fences drawn in real Chromium (design 2026-11-03 §1)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import LiveServer

PROMPT = '#chat-composer textarea[name="prompt"]'
DIAGRAM = "```mermaid\nflowchart LR\n  Start --> Finish\n```\n"


def _open_file(page: Any, base: str, name: str) -> None:
    page.evaluate(
        "(url) => htmx.ajax('GET', url, '#file-reader')",
        f"{base}/projects/Verify/files/view?path={name}",
    )


@pytest.mark.browser
def test_a_diagram_in_a_file_is_drawn_in_a_frame_without_script(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "diagram.md").write_text("# D\n\n" + DIAGRAM)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "diagram.md")

    frame = page.locator("#file-reader .mermaid-figure iframe")
    expect(frame).to_be_visible(timeout=15_000)
    # Emptied by app.js: Mermaid's own flags allow navigation and popups.
    assert frame.get_attribute("sandbox") == ""
    # Not collapsed; a 360x64 diagram scaled into the narrow reader is ~34 px.
    assert frame.bounding_box()["height"] > 10
    # The frame shrinks with the diagram in the narrow reader: no blank band.
    drawn = page.frame_locator("#file-reader .mermaid-figure iframe").locator("svg").bounding_box()
    assert abs(frame.bounding_box()["height"] - drawn["height"]) <= 4, (frame.bounding_box(), drawn)
    expect(page.locator("#file-reader .mermaid-diagram details > summary")).to_have_text("Source")


@pytest.mark.browser
def test_a_diagram_in_an_agent_reply_is_drawn(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["mermaid"], tmp_path)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    page.fill(PROMPT, "Draw it")
    page.click("#chat-composer button[type=submit]")

    expect(page.locator(f"#round-{alpha}-1 .mermaid-figure iframe")).to_be_visible(timeout=15_000)


@pytest.mark.browser
def test_a_diagram_in_a_recorded_prompt_is_drawn(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    alpha = app["session_ids"][0]
    page.goto(f"{app['base_url']}/projects/Verify/chat?agent={alpha}")
    expect(page.locator(PROMPT)).to_be_enabled(timeout=10_000)
    page.fill(PROMPT, DIAGRAM)
    page.click("#chat-composer button[type=submit]")
    # Recorded prompts sit in a collapsed "Prompt" disclosure.
    page.locator(f"#round-{alpha}-1 details.round-prompt > summary").click(timeout=15_000)

    expect(
        page.locator(f"#round-{alpha}-1 .round-prompt-box .mermaid-figure iframe")
    ).to_be_visible(timeout=15_000)


@pytest.mark.browser
def test_a_diagram_is_drawn_in_the_focus_dialog(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "diagram.md").write_text(DIAGRAM)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "diagram.md")
    expect(page.locator("#file-reader .mermaid-figure iframe")).to_be_visible(timeout=15_000)
    page.click("#file-reader .file-view-actions button:has-text('Focus')")

    expect(page.locator("#focus-dialog-content .mermaid-figure iframe")).to_be_visible(
        timeout=15_000
    )


@pytest.mark.browser
def test_a_linked_diagram_cannot_navigate_or_open_a_popup(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "linked.md").write_text(
        "```mermaid\nflowchart LR\n  A --> B\n"
        '  click A href "https://example.com" _blank\n'
        '  click B href "https://example.com"\n```\n'
    )
    chat = f"{app['base_url']}/projects/Verify/chat"
    page.goto(chat)
    _open_file(page, app["base_url"], "linked.md")
    frame = page.locator("#file-reader .mermaid-figure iframe")
    expect(frame).to_be_visible(timeout=15_000)
    assert frame.get_attribute("sandbox") == ""
    links = page.frame_locator("#file-reader .mermaid-figure iframe").locator("a")
    expect(links).to_have_count(2, timeout=5_000)
    links.nth(0).click()
    links.nth(1).click()
    page.wait_for_timeout(1_000)

    assert page.url == chat
    assert len(page.context.pages) == 1


def _corner_luminance(page: Any, locator: Any) -> float:
    """Relative luminance of a pixel just inside the element's top-left corner."""

    encoded = base64.b64encode(locator.screenshot()).decode()
    return page.evaluate(
        """async (data) => {
          const image = new Image();
          image.src = "data:image/png;base64," + data;
          await image.decode();
          const canvas = document.createElement("canvas");
          canvas.width = image.width;
          canvas.height = image.height;
          const context = canvas.getContext("2d");
          context.drawImage(image, 0, 0);
          const [r, g, b] = context.getImageData(3, 3, 1, 1).data;
          return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
        }""",
        encoded,
    )


@pytest.mark.browser
def test_a_dark_page_draws_the_diagram_on_a_dark_backdrop(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "diagram.md").write_text(DIAGRAM)
    page.emulate_media(color_scheme="dark")
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "diagram.md")
    frame = page.locator("#file-reader .mermaid-figure iframe")
    expect(frame).to_be_visible(timeout=15_000)
    page.wait_for_timeout(300)

    # Mermaid's dark theme draws light text; an opaque light backdrop
    # behind the frame (colour-scheme mismatch) would hide it.
    assert _corner_luminance(page, frame) < 0.5


@pytest.mark.browser
def test_a_page_without_a_diagram_never_downloads_mermaid(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "plain.md").write_text("# Plain\n")
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "plain.md")
    expect(page.locator("#file-reader h3")).to_have_text("plain.md", timeout=10_000)
    page.wait_for_timeout(500)

    assert not any("mermaid" in url for url in requested), requested


@pytest.mark.browser
def test_a_broken_diagram_shows_its_source_and_the_error(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    (tmp_path / "project" / "broken.md").write_text("```mermaid\nflowchart LR\n  A -->\n```\n")
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "broken.md")

    expect(page.locator("#file-reader .mermaid-error")).to_contain_text(
        "Diagram could not be rendered:", timeout=15_000
    )
    expect(page.locator("#file-reader .mermaid-source")).to_contain_text("A -->")


@pytest.mark.browser
def test_a_hostile_diagram_runs_no_script_in_the_page(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    hostile = (
        "```mermaid\nflowchart LR\n"
        "  A[\"<img src=x onerror='top.__pwned=1'>\"] --> B\n```\n"
    )
    (tmp_path / "project" / "hostile.md").write_text(hostile)
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    _open_file(page, app["base_url"], "hostile.md")
    # Must actually render: an error result would make the check vacuous.
    expect(page.locator('#file-reader .mermaid-diagram[data-mermaid-state="rendered"]')).to_have_count(
        1, timeout=15_000
    )
    page.wait_for_timeout(500)

    assert page.evaluate("window.__pwned") is None
