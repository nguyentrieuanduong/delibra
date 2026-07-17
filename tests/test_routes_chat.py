from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
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
    gamma = session(
        "c" * 32,
        "Gamma",
        rounds=[record(1, "2026-07-17T00:00:02Z")],
    )
    gamma.created_at = "2020-01-01T00:00:00Z"
    app, project, _ = setup_project(tmp_path, [alpha, beta, gamma])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/chat")
        fragment = client.get(
            f"/projects/{project.id}/sessions/{alpha.id}/rounds/1"
        )

    assert response.status_code == 200
    alpha_dom_id = f"round-{alpha.id}-1"
    beta_dom_id = f"round-{beta.id}-1"
    gamma_dom_id = f"round-{gamma.id}-1"
    assert response.text.count(f'id="{alpha_dom_id}"') == 1
    assert response.text.count(f'id="{beta_dom_id}"') == 1
    assert response.text.count(f'id="{gamma_dom_id}"') == 1
    timeline = response.text.split('<section class="chat-timeline"', 1)[1]
    assert timeline.index("Answer Beta") < timeline.index("Answer Alpha")
    assert timeline.index("Answer Alpha") < timeline.index("Answer Gamma")
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
    assert "Create agent" in response.text
    assert 'id="chat-composer"' in response.text
    assert re.search(r'<textarea name="prompt"[^>]+disabled', response.text)
    assert '<button type="submit" disabled>Send</button>' in response.text


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
        assert response.text.count("Running…") == 2

        assert client.post(f"{alpha_base}/cancel").status_code == 200
        assert client.post(f"{beta_base}/cancel").status_code == 200


def test_chat_selection_is_deterministic_and_invalid_selection_is_rejected(
    tmp_path: Path,
) -> None:
    zulu = session("a" * 32, "Zulu")
    alpha = session("b" * 32, "alpha")
    app, project, _ = setup_project(tmp_path, [zulu, alpha])

    with TestClient(app, base_url="http://localhost") as client:
        default = client.get(f"/projects/{project.id}/chat")
        explicit = client.get(f"/projects/{project.id}/chat?agent={zulu.id}")
        sidebar = client.get(
            f"/projects/{project.id}/chat/sidebar?agent={zulu.id}"
        )
        malformed = client.get(f"/projects/{project.id}/chat?agent=not-an-id")
        missing = client.get(f"/projects/{project.id}/chat?agent={'c' * 32}")

    assert f'value="{alpha.id}" selected' in default.text
    assert re.search(
        rf'data-session-id="{alpha.id}"\s+data-selected="true"',
        default.text,
    )
    assert f'value="{zulu.id}" selected' in explicit.text
    assert re.search(
        rf'data-session-id="{zulu.id}"\s+data-selected="true"',
        explicit.text,
    )
    assert malformed.status_code == 422
    assert missing.status_code == 404
    assert sidebar.status_code == 200
    assert 'id="agent-sidebar"' in sidebar.text
    assert '<section class="chat-timeline"' not in sidebar.text
    assert "No rounds yet." in sidebar.text


def test_chat_dispatch_validates_membership_and_runs_the_selected_agent(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    run_url = f"/projects/{project.id}/chat/run"
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    registry = RegistryStore(tmp_path / "home")
    other_project = registry.register("Other", other_path)
    outsider = session("c" * 32, "Outsider")
    ProjectStore(other_project).create_session(outsider)

    with TestClient(app, base_url="http://localhost") as client:
        invalid = client.post(run_url, data={"session_id": "bad", "prompt": "Hi"})
        missing = client.post(
            run_url,
            data={"session_id": outsider.id, "prompt": "Hi"},
        )
        started = client.post(
            run_url,
            data={"session_id": beta.id, "prompt": "Ask Beta"},
        )
        assert started.status_code == 202
        assert f'id="round-{beta.id}-1"' in started.text
        with client.stream(
            "GET",
            f"/projects/{project.id}/sessions/{beta.id}/rounds/1/stream",
        ) as response:
            response.read()

    assert invalid.status_code == 422
    assert missing.status_code == 422
    assert store.load_session(alpha.id).rounds == []
    assert store.load_session(beta.id).rounds[0].status == "complete"


def test_hx_create_selects_first_agent_and_updates_sidebar_composer_and_url(
    tmp_path: Path,
) -> None:
    app, project, store = setup_project(tmp_path, [])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{project.id}/sessions",
            headers={"HX-Request": "true"},
            data={
                "name": "Researcher",
                "agent": "claude",
                "model": "sonnet",
                "effort": "low",
                "role_instructions": "Be rigorous.",
            },
            follow_redirects=False,
        )

    created = store.list_sessions()[0]
    assert response.status_code == 200
    assert response.headers["hx-push-url"] == (
        f"/projects/{project.id}/chat?agent={created.id}"
    )
    assert 'id="agent-sidebar"' in response.text
    assert 'hx-swap-oob="outerHTML:#chat-composer"' in response.text
    assert f'value="{created.id}" selected' in response.text
    assert 'name="prompt"' in response.text
    assert '<section class="chat-timeline"' not in response.text


def test_hx_edit_preserves_selection_without_replacing_another_live_stream(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    alpha_base = f"/projects/{project.id}/sessions/{alpha.id}"

    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(f"{alpha_base}/run", data={"prompt": "Keep running"}).status_code == 202
        edited = client.post(
            f"/projects/{project.id}/sessions/{beta.id}/edit?agent={beta.id}",
            headers={"HX-Request": "true"},
            data={"name": "Beta renamed"},
            follow_redirects=False,
        )
        assert edited.status_code == 200
        assert 'id="agent-sidebar"' in edited.text
        assert 'hx-swap-oob="outerHTML:#chat-composer"' in edited.text
        assert f'value="{beta.id}" selected' in edited.text
        assert "Beta renamed" in edited.text
        assert '<section class="chat-timeline"' not in edited.text
        assert store.load_session(alpha.id).status == "running"
        assert client.post(f"{alpha_base}/cancel").status_code == 200


def test_busy_and_validation_errors_have_safe_visible_chat_contract(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    app, project, _ = setup_project(tmp_path, [alpha])
    run_url = f"/projects/{project.id}/chat/run"
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{project.id}/chat")
        assert 'id="chat-errors"' in page.text
        assert 'aria-live="polite"' in page.text
        assert client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "Keep running"},
        ).status_code == 202
        busy = client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "Again"},
        )
        invalid = client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "   "},
        )
        assert client.post(
            f"/projects/{project.id}/sessions/{alpha.id}/cancel"
        ).status_code == 200

    assert busy.status_code == 409
    assert "running" in busy.json()["detail"]
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], str)


def test_sidebar_preview_is_plain_truncated_and_send_targets_exclude_source(
    tmp_path: Path,
) -> None:
    alpha_record = record(1, "2026-07-17T00:00:01Z")
    beta_record = record(1, "2026-07-17T00:00:02Z")
    beta_record.status = "error"
    beta_record.error = "provider failed"
    alpha = session("a" * 32, "Alpha", rounds=[alpha_record])
    beta = session("b" * 32, "Beta", rounds=[beta_record])
    app, project, store = setup_project(tmp_path, [alpha, beta])
    (store.rounds_dir(alpha.id) / "round-01.md").write_text(
        "<b>unsafe</b> " + "x" * 300,
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/chat?agent={alpha.id}")

    assert "&lt;b&gt;unsafe&lt;/b&gt;" in response.text
    assert "<b>unsafe</b>" not in response.text
    assert "…" in response.text
    beta_card = re.search(
        rf'<article\s+class="agent-card[^>]*"\s+data-session-id="{beta.id}".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert beta_card is not None
    assert "Latest round 1 · error" in beta_card.group()
    assert "Answer Beta" in beta_card.group()
    assert "provider failed" in beta_card.group()
    alpha_bubble = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert alpha_bubble is not None
    assert "Send to…" in alpha_bubble.group()
    assert 'hx-target=".chat-timeline"' in alpha_bubble.group()
    assert f'<option value="{alpha.id}">' not in alpha_bubble.group()
    assert f'<option value="{beta.id}">' in alpha_bubble.group()


def test_chat_error_javascript_contract_runs_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node unavailable; chat error renderer unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_app_errors.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
