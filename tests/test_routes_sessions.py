from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


def seeded_client(tmp_path: Path):
    settings = Settings(home=tmp_path / "home")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Sessions", project_dir)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
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
