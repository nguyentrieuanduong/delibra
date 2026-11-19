"""The Downloads panel updates live, also after the run ended (design "UI", Codex S2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from app.download_fetch import FetchResult
from app.download_store import DownloadStore
from tests.browser.conftest import LiveServer


class StubFetcher:
    async def fetch(self, url, *, check_first, project_root, folder, filename, request_id, budget):
        return FetchResult(url, f"{folder}/arxiv.org/1.pdf", "ab" * 32, 3, "pdf")


@pytest.mark.browser
def test_approving_after_the_run_updates_the_panel_without_reload(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    downloads = DownloadStore(app["store"].root)
    record = downloads.add("https://arxiv.org/pdf/1", purpose="paper", filename=None, flags=[],
                           auto_run=1, cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha",
                           folder="raw")
    downloads.update(record.id, state="awaiting", reason="waiting for your approval")
    downloads.add("https://a.org/x?k=ghp_" + "A1b2" * 9, purpose="", filename=None, flags=["secret"],
                  auto_run=1, cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha", folder="raw")
    app["app"].state.download_manager.fetcher = StubFetcher()

    page.goto(f"{app['base_url']}/projects/Verify/chat")
    panel = page.locator("#downloads-panel")
    expect(panel).to_have_attribute("open", "", timeout=10_000)
    expect(panel).to_contain_text("(URL hidden)")
    expect(panel).not_to_contain_text("ghp_")

    page.locator("#download-D1").get_by_role("button", name="Approve").click()
    expect(page.locator("#download-D1 .download-state")).to_have_text("downloaded", timeout=5_000)
    expect(page.locator("#download-D1")).to_contain_text("raw/arxiv.org/1.pdf")


@pytest.mark.browser
def test_a_change_made_elsewhere_reaches_the_open_panel_by_its_stream(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    downloads = DownloadStore(app["store"].root)
    for url in ("https://arxiv.org/pdf/1", "https://arxiv.org/pdf/2"):
        record = downloads.add(url, purpose="paper", filename=None, flags=[], auto_run=1, cycle=1,
                               round_n=1, agent="a" * 32, agent_name="Alpha", folder="raw")
        downloads.update(record.id, state="awaiting", reason="waiting for your approval")
    app["app"].state.download_manager.fetcher = StubFetcher()

    page.goto(f"{app['base_url']}/projects/Verify/chat")
    summary = page.locator("#downloads-summary")
    expect(summary).to_contain_text("2 awaiting", timeout=10_000)
    # Approved from another client: only the panel's own stream can tell it.
    response = page.request.post(f"{app['base_url']}/projects/Verify/downloads/D2/approve")
    assert response.ok
    expect(page.locator("#download-D2 .download-state")).to_have_text("downloaded", timeout=5_000)
    expect(summary).to_contain_text("1 awaiting")


@pytest.mark.browser
def test_the_panel_follows_a_recreated_records_file(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    # Review 2026-11-15: a new requests.json restarts revisions at 1; the
    # panel used to ignore every change below the revision it showed.
    app = live_server(["success"], tmp_path)
    downloads = DownloadStore(app["store"].root)
    record = downloads.add("https://arxiv.org/pdf/old", purpose="old", filename=None, flags=[],
                           auto_run=1, cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha",
                           folder="raw")
    for _ in range(20):
        downloads.update(record.id, state="awaiting", reason="waiting for your approval")
    app["app"].state.download_manager.fetcher = StubFetcher()
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    expect(page.locator("#download-D1")).to_contain_text("arxiv.org/pdf/old", timeout=10_000)

    downloads.path.unlink()
    record = downloads.add("https://arxiv.org/pdf/new", purpose="new", filename=None, flags=[],
                           auto_run=1, cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha",
                           folder="raw")
    downloads.update(record.id, state="awaiting", reason="waiting for your approval")
    response = page.request.post(f"{app['base_url']}/projects/Verify/downloads/D1/approve")
    assert response.ok
    expect(page.locator("#download-D1")).to_contain_text("arxiv.org/pdf/new", timeout=5_000)
    expect(page.locator("#download-D1 .download-state")).to_have_text("downloaded", timeout=5_000)


@pytest.mark.browser
@pytest.mark.parametrize("width", [1400, 900])
def test_the_panel_sits_in_the_right_column_above_the_files(
    live_server: LiveServer, page: Any, tmp_path: Path, width: int
) -> None:
    app = live_server(["success"], tmp_path)
    downloads = DownloadStore(app["store"].root)
    record = downloads.add("https://arxiv.org/pdf/1", purpose="paper", filename=None, flags=[],
                           auto_run=1, cycle=1, round_n=1, agent="a" * 32, agent_name="Alpha", folder="raw")
    downloads.update(record.id, state="awaiting", reason="waiting for your approval")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    panel = page.locator("#downloads-panel")
    expect(panel).to_have_attribute("open", "", timeout=10_000)
    files = page.locator(".workspace-files").bounding_box()
    timeline = page.locator("#chat-timeline-frame").bounding_box()
    box = panel.bounding_box()
    browser = page.locator("#file-browser-host").bounding_box()
    assert box["x"] >= timeline["x"] + timeline["width"]  # right of the reading column
    assert files["x"] - 1 <= box["x"] and box["x"] + box["width"] <= files["x"] + files["width"] + 1
    assert box["y"] + box["height"] <= browser["y"] + 1  # above the file browser
