from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class InspectingAdapter:
    def __init__(self, mode: str, contexts: list[RunContext]) -> None:
        self.mode = mode
        self.contexts = contexts
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        self.contexts.append(context)
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", self.mode, "--delay", "0.03"],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "progress":
            return [AgentEvent("progress", "Inspected workspace")]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


def round_record(number: int, status: str = "complete", error: str | None = None):
    return RoundRecord(
        n=number,
        status=status,
        error=error,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at="2026-07-17T00:00:00Z",
        finished_at="2026-07-17T00:00:01Z" if status != "running" else None,
        source=SourceDescriptor(type="user"),
    )


def session_config(
    session_id: str,
    *,
    status: str = "idle",
    mode: str = "success",
    rounds: list[RoundRecord] | None = None,
    cli_session_id: str | None = None,
) -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=f"Session {session_id[0]}",
        agent="fake",
        model=mode,
        effort="low",
        role_instructions="Test role",
        cli_session_id=cli_session_id,
        status=status,
        created_at="2026-07-17T00:00:00Z",
        rounds=rounds or [],
    )


def seeded_app(tmp_path: Path, sessions: list[SessionConfig]):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Reply", project_path)
    store = ProjectStore(project)
    for session in sessions:
        store.create_session(session)
        for record in session.rounds:
            rounds = store.rounds_dir(session.id)
            (rounds / f"round-{record.n:02d}.prompt.md").write_text(
                f"Prompt {record.n}", encoding="utf-8"
            )
            if record.status == "running":
                (rounds / f"round-{record.n:02d}.partial.md").write_text(
                    "Interrupted partial", encoding="utf-8"
                )
            else:
                (rounds / f"round-{record.n:02d}.md").write_text(
                    f"Answer {record.n}", encoding="utf-8"
                )
    contexts: list[RunContext] = []
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=lambda config: InspectingAdapter(
            config.model, contexts
        ),
    )
    return app, project, store, contexts


def finish_round(client: TestClient, base: str, number: int) -> None:
    with client.stream("GET", f"{base}/rounds/{number}/stream") as response:
        assert response.status_code == 200
        response.read()


def test_reply_uses_native_then_stateless_history_and_snapshots_config(
    tmp_path: Path,
) -> None:
    config = session_config(
        "a" * 32,
        rounds=[round_record(1)],
        cli_session_id="native-thread",
    )
    app, project, store, contexts = seeded_app(tmp_path, [config])
    base = f"/projects/{quote(project.name, safe='')}/sessions/{config.id}"
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(base)
        assert 'name="prompt"' in page.text
        native = client.post(f"{base}/run", data={"prompt": "Native reply"})
        assert native.status_code == 202
        finish_round(client, base, 2)
        assert contexts[0].resume_strategy == "native"
        assert contexts[0].resume_id == "native-thread"

        persisted = store.load_session(config.id)
        persisted.cli_session_id = None
        store.save_session(persisted)
        stateless = client.post(f"{base}/run", data={"prompt": "Fallback reply"})
        assert stateless.status_code == 202
        finish_round(client, base, 3)

    assert contexts[1].resume_strategy == "stateless"
    assert contexts[1].resume_id is None
    assert [path.name for path in contexts[1].staged_history] == [
        "round-01.prompt.md",
        "round-01.md",
        "round-02.prompt.md",
        "round-02.md",
    ]
    records = store.load_session(config.id).rounds
    assert [(item.n, item.agent, item.model, item.effort) for item in records[-2:]] == [
        (2, "fake", "success", "low"),
        (3, "fake", "success", "low"),
    ]


def test_history_distinguishes_errors_cancellation_warnings_and_orphans(
    tmp_path: Path,
) -> None:
    error = round_record(1, "error", "agent exited; stderr: secret-free tail")
    error.warnings = ["provider did not return a native session id"]
    cancelled = round_record(2, "cancelled", "cancelled by user")
    config = session_config("b" * 32, rounds=[error, cancelled])
    app, project, store, _ = seeded_app(tmp_path, [config])
    (store.rounds_dir(config.id) / "round-99.md").write_text(
        "Orphan output", encoding="utf-8"
    )
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/sessions/{config.id}")
    assert page.status_code == 200
    assert 'class="round error"' in page.text
    assert 'class="round cancelled"' in page.text
    assert "stderr: secret-free tail" in page.text
    assert "provider did not return" in page.text
    assert "orphaned files" in page.text
    assert "Orphan output" in page.text
    assert "fake · success · low" in page.text


def test_running_page_attaches_live_pane_and_cancel_button(tmp_path: Path) -> None:
    config = session_config("c" * 32, mode="sleep")
    app, project, store, _ = seeded_app(tmp_path, [config])
    base = f"/projects/{quote(project.name, safe='')}/sessions/{config.id}"
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(f"{base}/run", data={"prompt": "Long"}).status_code == 202
        page = client.get(base)
        assert f'id="round-{config.id}-1"' in page.text
        assert ">Cancel</button>" in page.text
        assert client.post(f"{base}/cancel").status_code == 200
    assert store.load_session(config.id).rounds[0].status == "cancelled"


def test_restart_surfaces_partial_and_error_session_can_run_again(tmp_path: Path) -> None:
    config = session_config(
        "d" * 32,
        status="running",
        rounds=[round_record(1, "running")],
    )
    app, project, store, _ = seeded_app(tmp_path, [config])
    base = f"/projects/{quote(project.name, safe='')}/sessions/{config.id}"
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(base)
        assert "interrupted by restart" in page.text
        assert "Interrupted partial" in page.text
        assert 'class="round error"' in page.text
        restarted = client.post(f"{base}/run", data={"prompt": "Try again"})
        assert restarted.status_code == 202
        finish_round(client, base, 2)
    persisted = store.load_session(config.id)
    assert persisted.rounds[0].status == "error"
    assert persisted.rounds[1].status == "complete"
    assert persisted.status == "idle"


def test_error_round_retry_appends_linked_live_round(tmp_path: Path) -> None:
    failed = round_record(1, "error", "provider rejected request")
    config = session_config(
        "e" * 32,
        status="error",
        rounds=[failed],
        cli_session_id="failed-native-session",
    )
    app, project, store, contexts = seeded_app(tmp_path, [config])
    base = f"/projects/{quote(project.name, safe='')}/sessions/{config.id}"

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(base)
        assert f"{base}/rounds/1/retry" in page.text
        started = client.post(f"{base}/rounds/1/retry")
        assert started.status_code == 202
        assert f'id="round-{config.id}-2"' in started.text
        finish_round(client, base, 2)
        refreshed = client.get(f"{base}/rounds/2")

    records = store.load_session(config.id).rounds
    assert records[0] == failed
    assert records[1].retry_of == 1
    assert records[1].status == "complete"
    assert contexts[-1].resume_strategy == "stateless"
    assert contexts[-1].resume_id is None
    assert "Retry of round 1" in refreshed.text
    assert (store.rounds_dir(config.id) / "round-01.prompt.md").read_text(
        encoding="utf-8"
    ) == "Prompt 1"
