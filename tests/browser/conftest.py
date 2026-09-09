"""A live Delibra on a real socket, driven by real Chromium.

Everything above the provider subprocess is shipping code: real routes,
real templates, real vendored htmx. Only the CLI is scripted -- a live run
would spend the operator's subscription.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Protocol

import pytest
import uvicorn

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.storage import ProjectStore, RegistryStore

FAKE_CLI = Path(__file__).resolve().parents[1] / "fake_cli.py"


def _missing_commands(tmp_path: Path) -> dict[str, str]:
    """Provider commands guaranteed not to exist.

    create_app hands these to probe_all, which runs `<cmd> --version`
    (app/health.py:63-80), and to RunManager(codex_executable=...), which
    refresh_codex_quota spawns against the operator's real Codex account
    (app/runner.py:1550). adapter_factory_override intercepts neither, so
    the only way to keep the "never drive the real CLIs" constraint is to
    name commands that cannot resolve.

    The rest of the suite hardcodes /missing/claude (e.g.
    tests/test_routes_auto.py:147). Deriving them from tmp_path instead
    makes non-existence a fact this fixture owns rather than an assumption
    about the host filesystem -- and these are the tests that would spend
    the operator's subscription if the assumption ever broke.
    """

    return {
        "claude": str(tmp_path / "missing-claude"),
        "codex": str(tmp_path / "missing-codex"),
    }


class LiveServer(Protocol):
    def __call__(
        self, modes: list[str], tmp_path: Path, delay: float = 0.01
    ) -> dict[str, Any]: ...


class ScriptedAdapter:
    """The ChatAdapter shape from tests/test_routes_chat.py:37."""

    def __init__(self, mode: str, delay: float, log: list[list[str]]) -> None:
        self.mode = mode
        self.delay = delay
        self._log = log
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        argv = [
            sys.executable,
            str(FAKE_CLI),
            "--mode",
            self.mode,
            "--delay",
            str(self.delay),
        ]
        # Recorded so a test can assert that no other executable was ever
        # dispatched as a provider.
        self._log.append(argv)
        return Command(argv, context.user_prompt)

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


@pytest.fixture
def live_server() -> Iterator[LiveServer]:
    """Start a server whose Nth provider dispatch uses modes[N].

    Each test names its own modes: "success" finishes immediately, "sleep"
    holds the round open for 300s (tests/fake_cli.py:76). A test that must
    observe a round while it runs has to ask for "sleep" -- "success" can
    finish before the assertion looks.
    """

    running: list[tuple[uvicorn.Server, threading.Thread, Any, str]] = []

    def start(
        modes: list[str], tmp_path: Path, delay: float = 0.01
    ) -> dict[str, Any]:
        home = tmp_path / "home"
        project_dir = tmp_path / "project"
        project_dir.mkdir(exist_ok=True)
        settings = Settings(home=home, run_timeout=120)
        registry = RegistryStore(home)
        project = registry.register("Verify", project_dir)
        store = ProjectStore(project)
        session_ids: list[str] = []
        for index, name in enumerate(("Alpha", "Beta"), start=1):
            session_id = str(index) * 32
            store.create_session(
                SessionConfig(
                    id=session_id,
                    name=name,
                    # A codex session hits a real auth precondition against
                    # CODEX_HOME and fails before the adapter runs.
                    agent="claude",
                    model="fake",
                    effort="low",
                    role_instructions="",
                    cli_session_id=None,
                    status="idle",
                    created_at="2026-09-09T00:00:00Z",
                )
            )
            session_ids.append(session_id)

        dispatched: list[list[str]] = []

        def factory(config: SessionConfig) -> ScriptedAdapter:
            mode = modes[min(len(dispatched), len(modes) - 1)]
            return ScriptedAdapter(mode, delay, dispatched)

        commands = _missing_commands(tmp_path)
        assert not any(Path(value).exists() for value in commands.values())
        app = create_app(
            settings_override=settings,
            provider_commands=commands,
            adapter_factory_override=factory,
        )
        # Port 0 lets the kernel assign, and the port is read back from the
        # socket Uvicorn actually bound. Probing for a free port and then
        # binding it later leaves a window for another process to take it.
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        # Registered before the readiness check so teardown stops a server
        # that started but never reported ready.
        running.append((server, thread, app, project.id))
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise AssertionError("the live server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        return {
            "base_url": f"http://127.0.0.1:{port}",
            "session_ids": session_ids,
            "store": store,
            "dispatched": dispatched,
            "project_id": project.id,
        }

    yield start

    leaked = []
    for server, thread, app, project_id in running:
        server.should_exit = True
    for server, thread, app, project_id in running:
        thread.join(timeout=10)
        if thread.is_alive():
            leaked.append(thread.name)
    # A surviving server keeps a port and a project directory alive and
    # will corrupt whichever test runs next. Fail loudly instead.
    assert not leaked, f"live server threads did not stop: {leaked}"

    # Ownership, not a machine-wide process sweep. `sleep` mode holds for
    # 300s (tests/fake_cli.py:76) against a 120s run_timeout, so a torn-down
    # server could strand a subprocess holding tmp_path open. Uvicorn's
    # graceful stop runs the lifespan finally block, which awaits
    # manager.shutdown() (app/main.py:154); that terminates every ActiveRun
    # and awaits its completion future (app/runner.py:2060-2072), which
    # cannot resolve before the process is reaped. So an empty active map
    # *after* the join is proof this fixture's own runs ended -- and it says
    # nothing about, and cannot be confused by, another checkout's suite.
    stranded = [
        project_id
        for _, _, app, project_id in running
        if app.state.manager is not None
        and app.state.manager.has_active_project(project_id)
    ]
    assert not stranded, f"runs still active after shutdown: {stranded}"


@pytest.fixture
def page() -> Iterator[Any]:
    """A Chromium page with guaranteed teardown.

    Owns launch and close once, so the six tests below carry only their
    own setup and assertions and a teardown bug has one place to live.
    """

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            yield browser.new_page()
        finally:
            browser.close()
