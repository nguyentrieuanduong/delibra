from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


def test_session_page_renders_seeded_rounds_and_safe_markdown(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Seeded Project", project_dir)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="Be rigorous.",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[
            RoundRecord(
                n=1,
                status="complete",
                error=None,
                warnings=[],
                agent="claude",
                model="sonnet",
                effort="high",
                started_at="2026-07-17T00:00:01Z",
                finished_at="2026-07-17T00:00:02Z",
                source=SourceDescriptor(type="user"),
            )
        ],
    )
    store.create_session(session)
    rounds = store.rounds_dir(session.id)
    (rounds / "round-01.prompt.md").write_text("What is **safe**?")
    (rounds / "round-01.md").write_text("A **rendered** answer. <script>bad()</script>")

    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.id}/sessions/{session.id}")
    assert response.status_code == 200
    assert "Researcher" in response.text
    assert "<strong>rendered</strong>" in response.text
    assert "<script>bad()" not in response.text
    assert "Claude CLI not found" in response.text
