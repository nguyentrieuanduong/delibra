from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class RouteFakeAdapter:
    EFFORT_LEVELS = ["low"]

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [
                sys.executable,
                str(FAKE_CLI),
                "--mode",
                self.mode,
                "--delay",
                "0.08",
            ],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return [AgentEvent("error", "malformed")]
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


def seeded_app(tmp_path: Path, *, mode: str = "success", replay_limit: int = 5 * 1024 * 1024):
    settings = replace(Settings(home=tmp_path / "home"), replay_limit=replay_limit)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Routes", project_dir)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Route session",
        agent="fake",
        model=mode,
        effort="low",
        role_instructions="Test",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )
    store.create_session(session)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=lambda config: RouteFakeAdapter(config.model),
    )
    return app, project, session, store


def parse_sse(response) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if current:
                current["data"] = "\n".join(data)
                events.append(current)
            current = {}
            data = []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data.append(value)
        else:
            current[field] = value
    return events


def test_post_run_stream_reconnect_late_done_and_final_fragment(tmp_path: Path) -> None:
    app, project, session, store = seeded_app(tmp_path)
    base = f"/projects/{project.id}/sessions/{session.id}"
    with TestClient(app, base_url="http://localhost") as client:
        started = client.post(f"{base}/run", data={"prompt": "Question"})
        assert started.status_code == 202
        dom_id = f"round-{session.id}-1"
        assert f'id="{dom_id}"' in started.text
        assert f'hx-target="#{dom_id}"' in started.text
        assert 'hx-swap="outerHTML"' in started.text
        assert 'sse-swap="reset"' in started.text

        base_page = client.get(base)
        assert '<script src="/static/app.js" defer></script>' in base_page.text

        busy = client.post(f"{base}/run", data={"prompt": "Another"})
        assert busy.status_code == 409

        stream_url = f"{base}/rounds/1/stream"
        with client.stream("GET", stream_url) as response:
            events = parse_sse(response)
        assert response.status_code == 200
        assert [event["event"] for event in events][-1] == "done"
        assert "text_delta" in [event["event"] for event in events]
        assert "progress" in [event["event"] for event in events]
        ids = [int(event["id"]) for event in events]
        assert ids == sorted(set(ids))

        pivot = ids[1]
        with client.stream(
            "GET",
            stream_url,
            headers={"Last-Event-ID": str(pivot)},
        ) as response:
            replay = parse_sse(response)
        assert all(int(event["id"]) > pivot for event in replay)
        assert replay[-1]["event"] == "done"

        with client.stream("GET", stream_url) as response:
            late = parse_sse(response)
        assert [event["event"] for event in late] == ["done"]

        fragment = client.get(f"{base}/rounds/1")
        assert fragment.status_code == 200
        assert "Hello" in fragment.text
        page = client.get(base)
        assert "Hello" in page.text
    assert (store.rounds_dir(session.id) / "round-01.md").read_text() == "Hello"


def test_replay_gap_streams_reset_snapshot_and_done(tmp_path: Path) -> None:
    app, project, session, _ = seeded_app(tmp_path, replay_limit=90)
    base = f"/projects/{project.id}/sessions/{session.id}"
    with TestClient(app, base_url="http://localhost") as client:
        client.post(f"{base}/run", data={"prompt": "Question"})
        stream_url = f"{base}/rounds/1/stream"
        with client.stream("GET", stream_url) as response:
            parse_sse(response)
        with client.stream(
            "GET", stream_url, headers={"Last-Event-ID": "0"}
        ) as response:
            replay = parse_sse(response)
    assert [event["event"] for event in replay] == ["reset", "snapshot", "done"]
    assert replay[0]["data"] == f"round-{session.id}-1"
    assert "Hello" in replay[1]["data"]


def test_cancel_route_finalizes_running_round(tmp_path: Path) -> None:
    app, project, session, store = seeded_app(tmp_path, mode="sleep")
    base = f"/projects/{project.id}/sessions/{session.id}"
    with TestClient(app, base_url="http://localhost") as client:
        started = client.post(f"{base}/run", data={"prompt": "Long question"})
        assert started.status_code == 202
        cancelled = client.post(f"{base}/cancel")
        assert cancelled.status_code == 200
        with client.stream("GET", f"{base}/rounds/1/stream") as response:
            events = parse_sse(response)
    assert events[-1]["event"] == "done"
    assert store.load_session(session.id).rounds[0].status == "cancelled"
