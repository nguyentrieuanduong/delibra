"""The Conversions panel shows the queue live and cancels from the page (design 2026-11-20)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from typing import Any

import pytest
from playwright.sync_api import expect

from app.coding import DownloadPolicy
from app.doc_convert import ConvertResult
from app.download_manager import DownloadTurn
from app.questions import prepare_spool
from tests.browser.conftest import LiveServer


class ThreadGatedConverter:
    """The Nth conversion finishes once the test thread has released N of them."""

    def __init__(self) -> None:
        self.calls = 0
        self.released = 0
        self.lock = threading.Lock()

    def release_one(self) -> None:
        with self.lock:
            self.released += 1

    async def __call__(self, staged, **kwargs):
        kwargs["on_limit"](486.0)
        number = self.calls
        self.calls += 1
        while True:
            with self.lock:
                if self.released > number:
                    break
            await asyncio.sleep(0.02)
        return ConvertResult("converted", None, b"# md\n", "pdf")


def server_loop(app: dict[str, Any]) -> asyncio.AbstractEventLoop:
    """The live server's event loop, read by a test-only route."""

    loops: list[asyncio.AbstractEventLoop] = []

    async def capture() -> dict[str, bool]:
        loops.append(asyncio.get_running_loop())
        return {"ok": True}

    app["app"].add_api_route("/test-only/loop", capture, methods=["GET"])
    return loops


def request_conversions(app: dict[str, Any], loop: asyncio.AbstractEventLoop, tmp_path: Path,
                        names: list[str]) -> None:
    manager = app["app"].state.download_manager
    store = app["store"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in names:
        (store.project_path / name).write_bytes(b"%PDF-1.7 " + name.encode())

    async def start() -> None:
        prepare_spool(workspace, "convert", "requests", extra_names=("status.json",))
        turn = DownloadTurn(
            project_id=app["project_id"], project_root=store.project_path, store_root=store.root,
            temp_dir=workspace, auto_run=0, cycle=None, round_n=1, agent="1" * 32, agent_name="Alpha",
            session_id="1" * 32, policy=DownloadPolicy(convert=True), convert=True,
        )
        manager.begin_turn(turn)
        for index, name in enumerate(names, start=1):
            (turn.convert_spool / f"c{index}.json").write_text(json.dumps({"path": name}))
        manager.harvest(turn, final=True)
        await manager.stop_watch(turn)
        manager.end_turn(turn)

    asyncio.run_coroutine_threadsafe(start(), loop).result(10)


def settled(page: Any) -> None:
    """htmx 1.9.12 activates swapped content (the Cancel buttons) in a settle step
    about 20 ms after the swap: a click before then does nothing."""

    page.wait_for_function(
        "() => !document.querySelector('#conversions-list.htmx-settling, #conversions-list .htmx-added,"
        " #conversions-list .htmx-settling, #conversions-list.htmx-swapping')"
    )


@pytest.mark.browser
def test_the_queue_moves_live_and_cancels_from_the_panel(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    loops = server_loop(app)
    assert page.request.get(f"{app['base_url']}/test-only/loop").ok
    converter = ThreadGatedConverter()
    app["app"].state.download_manager.converter = converter
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    expect(page.locator("#conversions-panel")).to_be_attached(timeout=10_000)

    request_conversions(app, loops[0], tmp_path, ["a.pdf", "b.pdf", "c.pdf"])

    first, second, third = (page.locator(f"#conversion-C{n}") for n in (1, 2, 3))
    expect(page.locator("#conversions-panel")).to_have_attribute("open", "", timeout=10_000)
    expect(first.locator(".conversion-state")).to_have_text("converting", timeout=10_000)
    expect(first).to_contain_text("time limit 8:06")
    expect(second).to_contain_text("Position 1 in the queue")
    expect(third).to_contain_text("Position 2 in the queue")
    expect(page.locator("#conversions-summary")).to_contain_text("1 converting, 2 queued")
    clock = first.locator("[data-elapsed]")
    shown = clock.text_content()
    expect(clock).not_to_have_text(shown, timeout=5_000)  # it ticks without a refresh

    settled(page)
    third.get_by_role("button", name="Cancel").click()
    expect(third.locator(".conversion-state")).to_have_text("cancelled", timeout=5_000)
    expect(third).to_contain_text("cancelled by the user")

    converter.release_one()
    expect(first.locator(".conversion-state")).to_have_text("converted", timeout=10_000)
    expect(second.locator(".conversion-state")).to_have_text("converting", timeout=10_000)
    expect(second).to_contain_text("time limit 8:06")  # its last change before the click

    dialogs: list[str] = []
    page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    settled(page)
    second.get_by_role("button", name="Cancel").click()
    expect(second.locator(".conversion-state")).to_have_text("cancelled", timeout=10_000)
    assert dialogs == ["Stop converting b.pdf? The work done so far is lost."]
    expect(page.locator("#conversions-summary")).to_have_text("Conversions")


@pytest.mark.browser
@pytest.mark.parametrize("width", [1400, 900])
def test_the_conversions_panel_sits_between_downloads_and_the_files(
    live_server: LiveServer, page: Any, tmp_path: Path, width: int
) -> None:
    app = live_server(["success"], tmp_path)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{app['base_url']}/projects/Verify/chat")
    downloads = page.locator("#downloads-panel").bounding_box()
    conversions = page.locator("#conversions-panel").bounding_box()
    files = page.locator(".workspace-files").bounding_box()
    browser = page.locator("#file-browser-host").bounding_box()
    timeline = page.locator("#chat-timeline-frame").bounding_box()
    assert conversions["x"] >= timeline["x"] + timeline["width"]  # right of the reading column
    assert files["x"] - 1 <= conversions["x"]
    assert conversions["x"] + conversions["width"] <= files["x"] + files["width"] + 1
    assert downloads["y"] + downloads["height"] <= conversions["y"] + 1
    assert conversions["y"] + conversions["height"] <= browser["y"] + 1
