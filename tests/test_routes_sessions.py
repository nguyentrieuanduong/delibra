from __future__ import annotations

import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from app.agents.base import AgentEvent, Command, RunContext
from app.agents.claude import ClaudeAdapter
from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class RecordingAdapter:
    EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
    RESUME_AFTER_CONFIG_CHANGE = True

    def __init__(self, observations: list[dict]) -> None:
        self.observations = observations
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        self.observations.append(
            {
                "model": config.model,
                "effort": config.effort,
                "context": context,
            }
        )
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", "success"],
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
            return [AgentEvent("progress", "Fake progress")]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


def seeded_client(tmp_path: Path, *, observations: list[dict] | None = None):
    settings = Settings(home=tmp_path / "home")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Sessions", project_dir)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=(
            (lambda config: RecordingAdapter(observations))
            if observations is not None
            else None
        ),
    )
    return TestClient(app, base_url="http://localhost"), project, ProjectStore(project)


def create_session(client: TestClient, project_id: str, **overrides):
    form = {
        "name": "Researcher",
        "agent": "claude",
        "model": "sonnet",
        "effort": "max",
        "role_instructions": "Be rigorous.",
    }
    form.update(overrides)
    return client.post(
        f"/projects/{project_id}/sessions",
        data=form,
        follow_redirects=False,
    )


def seed_completed_round(store: ProjectStore, session_id: str) -> None:
    config = store.load_session(session_id)
    config.cli_session_id = "original-native-id"
    config.rounds.append(
        RoundRecord(
            n=1,
            status="complete",
            error=None,
            warnings=[],
            agent=config.agent,
            model=config.model,
            effort=config.effort,
            started_at="2026-07-17T00:00:00Z",
            finished_at="2026-07-17T00:00:01Z",
            source=SourceDescriptor(type="user"),
        )
    )
    store.save_session(config)
    rounds = store.rounds_dir(session_id)
    (rounds / "round-01.prompt.md").write_text("Initial question", encoding="utf-8")
    (rounds / "round-01.md").write_text("Initial answer", encoding="utf-8")


def finish_stream(client: TestClient, project_id: str, session_id: str, round_n: int) -> None:
    url = f"/projects/{project_id}/sessions/{session_id}/rounds/{round_n}/stream"
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert any(line == "event: done" for line in response.iter_lines())


def test_create_session_validates_provider_specific_effort_and_renders_controls(
    tmp_path: Path,
) -> None:
    client, project, store = seeded_client(tmp_path)
    with client:
        claude = create_session(client, project.id)
        assert claude.status_code == 303

        codex = create_session(
            client,
            project.id,
            name="Critic",
            agent="codex",
            model="gpt-5.4",
            effort="minimal",
        )
        assert codex.status_code == 303

        assert create_session(client, project.id, effort="minimal").status_code == 422
        assert create_session(
            client, project.id, agent="codex", effort="max"
        ).status_code == 422
        assert create_session(client, project.id, agent="other").status_code == 422
        assert create_session(client, project.id, model="  ").status_code == 422
        assert create_session(
            client, project.id, role_instructions="x" * 20_001
        ).status_code == 422

        page = client.get(f"/projects/{project.id}")
    sessions = store.list_sessions()
    assert {(item.agent, item.effort) for item in sessions} == {
        ("codex", "minimal"),
        ("claude", "max"),
    }
    assert all(item.id in page.text for item in sessions)
    assert 'action="/projects/' in page.text
    assert "Create session" in page.text
    assert "Delete" in page.text
    assert '<optgroup label="Claude" data-effort-agent="claude">' in page.text
    assert '<optgroup label="Codex" data-effort-agent="codex" disabled>' in page.text
    assert 'data-agent-select' in page.text
    assert 'data-effort-select' in page.text


def test_edit_all_fields_before_first_round_then_name_only(tmp_path: Path) -> None:
    client, project, store = seeded_client(tmp_path)
    with client:
        created = create_session(client, project.id)
        session_id = created.headers["location"].rsplit("/", 1)[-1]
        edit_path = f"/projects/{project.id}/sessions/{session_id}/edit"
        edited = client.post(
            edit_path,
            data={
                "name": "Codex critic",
                "agent": "codex",
                "model": "gpt-5.4",
                "effort": "xhigh",
                "role_instructions": "Challenge assumptions.",
            },
            follow_redirects=False,
        )
        assert edited.status_code == 303
        config = store.load_session(session_id)
        assert (
            config.name,
            config.agent,
            config.model,
            config.effort,
            config.role_instructions,
        ) == (
            "Codex critic",
            "codex",
            "gpt-5.4",
            "xhigh",
            "Challenge assumptions.",
        )

        config.rounds.append(
            RoundRecord(
                n=1,
                status="complete",
                error=None,
                warnings=[],
                agent=config.agent,
                model=config.model,
                effort=config.effort,
                started_at="2026-07-17T00:00:00Z",
                finished_at="2026-07-17T00:00:01Z",
                source=SourceDescriptor(type="user"),
            )
        )
        store.save_session(config)

        rejected = client.post(
            edit_path,
            data={"name": "Wrong", "agent": "claude"},
            follow_redirects=False,
        )
        assert rejected.status_code == 409
        renamed = client.post(
            edit_path,
            data={"name": "Final name"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
    persisted = store.load_session(session_id)
    assert persisted.name == "Final name"
    assert persisted.agent == "codex"
    assert persisted.role_instructions == "Challenge assumptions."


def test_post_round_model_effort_edit_drives_next_run_and_preserves_native_id(
    tmp_path: Path,
) -> None:
    observations: list[dict] = []
    client, project, store = seeded_client(tmp_path, observations=observations)
    with client:
        created = create_session(client, project.id, effort="low")
        session_id = created.headers["location"].rsplit("/", 1)[-1]
        seed_completed_round(store, session_id)
        edit_path = f"/projects/{project.id}/sessions/{session_id}/edit"

        edited = client.post(
            edit_path,
            data={"name": "Updated", "model": "opus", "effort": "medium"},
            follow_redirects=False,
        )
        assert edited.status_code == 303
        assert store.load_session(session_id).cli_session_id == "original-native-id"

        identical = client.post(
            edit_path,
            data={"name": "Updated", "model": "opus", "effort": "medium"},
            follow_redirects=False,
        )
        assert identical.status_code == 303
        assert store.load_session(session_id).cli_session_id == "original-native-id"

        started = client.post(
            f"/projects/{project.id}/sessions/{session_id}/run",
            data={"prompt": "Continue"},
        )
        assert started.status_code == 202
        finish_stream(client, project.id, session_id, 2)

        assert client.post(
            edit_path,
            data={"name": "Updated", "agent": "codex"},
            follow_redirects=False,
        ).status_code == 409
        assert client.post(
            edit_path,
            data={"name": "Updated", "role_instructions": "New role"},
            follow_redirects=False,
        ).status_code == 409

    assert observations[0]["model"] == "opus"
    assert observations[0]["effort"] == "medium"
    assert observations[0]["context"].resume_strategy == "native"
    assert observations[0]["context"].resume_id == "original-native-id"
    persisted = store.load_session(session_id)
    assert persisted.rounds[-1].model == "opus"
    assert persisted.rounds[-1].effort == "medium"


def test_unsupported_config_change_clears_resume_then_warns_and_adopts_new_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[dict] = []
    client, project, store = seeded_client(tmp_path, observations=observations)
    monkeypatch.setattr(ClaudeAdapter, "RESUME_AFTER_CONFIG_CHANGE", False)
    with client:
        created = create_session(client, project.id, effort="low")
        session_id = created.headers["location"].rsplit("/", 1)[-1]
        seed_completed_round(store, session_id)
        edited = client.post(
            f"/projects/{project.id}/sessions/{session_id}/edit",
            data={"name": "Fallback", "model": "opus", "effort": "medium"},
            follow_redirects=False,
        )
        assert edited.status_code == 303
        assert store.load_session(session_id).cli_session_id is None

        started = client.post(
            f"/projects/{project.id}/sessions/{session_id}/run",
            data={"prompt": "Continue statelessly"},
        )
        assert started.status_code == 202
        finish_stream(client, project.id, session_id, 2)

    context = observations[0]["context"]
    assert context.resume_strategy == "stateless"
    assert [path.name for path in context.staged_history] == [
        "round-01.prompt.md",
        "round-01.md",
    ]
    persisted = store.load_session(session_id)
    assert persisted.cli_session_id == "fake-native-session"
    assert any(
        "bounded staged history" in warning
        for warning in persisted.rounds[-1].warnings
    )


def test_any_edit_while_running_is_409_and_leaves_snapshot_unchanged(
    tmp_path: Path,
) -> None:
    client, project, store = seeded_client(tmp_path)
    with client:
        created = create_session(client, project.id)
        session_id = created.headers["location"].rsplit("/", 1)[-1]
        seed_completed_round(store, session_id)
        config = store.load_session(session_id)
        config.status = "running"
        config.rounds.append(
            RoundRecord(
                n=2,
                status="running",
                error=None,
                warnings=[],
                agent=config.agent,
                model=config.model,
                effort=config.effort,
                started_at="2026-07-17T00:00:02Z",
                finished_at=None,
                source=SourceDescriptor(type="user"),
            )
        )
        store.save_session(config)
        before = store.load_session(session_id).to_dict()

        response = client.post(
            f"/projects/{project.id}/sessions/{session_id}/edit",
            data={"name": "Changed", "model": "opus", "effort": "medium"},
            follow_redirects=False,
        )
        assert response.status_code == 409

    assert store.load_session(session_id).to_dict() == before


def test_delete_rejects_running_session_and_removes_idle_owned_session(
    tmp_path: Path,
) -> None:
    client, project, store = seeded_client(tmp_path)
    with client:
        created = create_session(client, project.id)
        session_id = created.headers["location"].rsplit("/", 1)[-1]
        delete_path = f"/projects/{project.id}/sessions/{session_id}/delete"
        config = store.load_session(session_id)
        config.status = "running"
        store.save_session(config)
        assert client.post(delete_path, follow_redirects=False).status_code == 409
        assert store.session_dir(session_id).is_dir()

        config.status = "idle"
        store.save_session(config)
        deleted = client.post(delete_path, follow_redirects=False)
        assert deleted.status_code == 303
        assert deleted.headers["location"] == f"/projects/{project.id}"
    assert not store.session_dir(session_id).exists()
