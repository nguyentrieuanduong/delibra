from __future__ import annotations

import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class ChatAdapter:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", self.mode],
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
            return [AgentEvent("progress", "Chat progress")]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


def record(number: int, started_at: str) -> RoundRecord:
    return RoundRecord(
        n=number,
        status="complete",
        error=None,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at=started_at,
        finished_at=started_at,
        source=SourceDescriptor(type="user"),
    )


def session(
    session_id: str,
    name: str,
    *,
    mode: str = "success",
    rounds: list[RoundRecord] | None = None,
) -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=name,
        agent="fake",
        model=mode,
        effort="low",
        role_instructions="Test",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=rounds or [],
    )


def setup_project(tmp_path: Path, sessions: list[SessionConfig]):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Chat", project_path)
    store = ProjectStore(project)
    for config in sessions:
        store.create_session(config)
        for item in config.rounds:
            rounds = store.rounds_dir(config.id)
            (rounds / f"round-{item.n:02d}.prompt.md").write_text(
                f"Prompt {config.name}", encoding="utf-8"
            )
            (rounds / f"round-{item.n:02d}.md").write_text(
                f"Answer {config.name}", encoding="utf-8"
            )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=lambda config: ChatAdapter(config.model),
    )
    return app, project, store


def test_chat_merges_rounds_deterministically_with_unique_composite_fragments(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[record(1, "2026-07-17T00:00:02Z")],
    )
    beta = session(
        "b" * 32,
        "Beta",
        rounds=[record(1, "2026-07-17T00:00:01Z")],
    )
    app, project, _ = setup_project(tmp_path, [alpha, beta])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/chat")
        fragment = client.get(
            f"/projects/{project.id}/sessions/{alpha.id}/rounds/1"
        )

    assert response.status_code == 200
    alpha_dom_id = f"round-{alpha.id}-1"
    beta_dom_id = f"round-{beta.id}-1"
    assert response.text.count(f'id="{alpha_dom_id}"') == 1
    assert response.text.count(f'id="{beta_dom_id}"') == 1
    assert response.text.index("Answer Beta") < response.text.index("Answer Alpha")
    assert "Beta · Round 1" in response.text
    assert "Alpha · Round 1" in response.text
    assert fragment.status_code == 200
    assert f'id="{alpha_dom_id}"' in fragment.text
    assert "Alpha · Round 1" in fragment.text


def test_chat_empty_project_has_an_explicit_empty_timeline(tmp_path: Path) -> None:
    app, project, _ = setup_project(tmp_path, [])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/chat")
    assert response.status_code == 200
    assert "No conversation yet." in response.text


def test_chat_renders_two_concurrent_live_fragments_with_scoped_done_targets(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    beta = session("b" * 32, "Beta", mode="sleep")
    app, project, _ = setup_project(tmp_path, [alpha, beta])
    alpha_base = f"/projects/{project.id}/sessions/{alpha.id}"
    beta_base = f"/projects/{project.id}/sessions/{beta.id}"

    with TestClient(app, base_url="http://localhost") as client:
        alpha_started = client.post(f"{alpha_base}/run", data={"prompt": "A"})
        beta_started = client.post(f"{beta_base}/run", data={"prompt": "B"})
        response = client.get(f"/projects/{project.id}/chat")

        alpha_dom_id = f"round-{alpha.id}-1"
        beta_dom_id = f"round-{beta.id}-1"
        assert f'id="{alpha_dom_id}"' in alpha_started.text
        assert f'hx-target="#{alpha_dom_id}"' in alpha_started.text
        assert f'id="{beta_dom_id}"' in beta_started.text
        assert f'hx-target="#{beta_dom_id}"' in beta_started.text
        assert response.text.count('class="live-round"') == 2
        assert response.text.count(f'id="{alpha_dom_id}"') == 1
        assert response.text.count(f'id="{beta_dom_id}"') == 1
        assert "Alpha · Round 1 · running" in response.text
        assert "Beta · Round 1 · running" in response.text

        assert client.post(f"{alpha_base}/cancel").status_code == 200
        assert client.post(f"{beta_base}/cancel").status_code == 200
