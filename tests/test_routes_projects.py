from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.storage import NotFoundError, ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class SleepingAdapter:
    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", "sleep"],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        return []

    def final_text(self) -> str:
        return ""


def project_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    return create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    ), settings


def test_project_crud_path_validation_canonicalization_import_and_invalid_metadata(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    real = tmp_path / "real-project"
    real.mkdir()
    linked = tmp_path / "linked-project"
    linked.symlink_to(real, target_is_directory=True)
    missing = tmp_path / "missing"
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("not a directory", encoding="utf-8")
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(
            "/projects", data={"name": "Relative", "path": "relative/path"}
        ).status_code == 422
        assert client.post(
            "/projects", data={"name": "Missing", "path": str(missing)}
        ).status_code == 422
        assert client.post(
            "/projects", data={"name": "File", "path": str(regular_file)}
        ).status_code == 422

        registered = client.post(
            "/projects",
            data={"name": "Canonical", "path": str(linked)},
            follow_redirects=False,
        )
        assert registered.status_code == 303
        assert registered.headers["location"].endswith("/chat")
        project_id = registered.headers["location"].split("/")[-2]
        registry = RegistryStore(settings.home)
        assert registry.get(project_id).path == str(real.resolve())
        assert client.post(
            "/projects", data={"name": "Duplicate", "path": str(real)}
        ).status_code == 409

        renamed = client.post(
            f"/projects/{project_id}/rename",
            data={"name": "Renamed"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert registry.get(project_id).name == "Renamed"

        index = client.get("/")
        assert "Register project" in index.text
        assert "Renamed" in index.text
        unregistered = client.post(
            f"/projects/{project_id}/unregister", follow_redirects=False
        )
        assert unregistered.status_code == 303
        assert not registry.list_projects()
        assert (real / ".delibra").is_dir()

        imported = client.post(
            "/projects",
            data={"name": "Imported", "path": str(real)},
            follow_redirects=False,
        )
        assert imported.status_code == 303
        assert imported.headers["location"] == f"/projects/{project_id}/chat"

        invalid = tmp_path / "invalid-project"
        (invalid / ".delibra").mkdir(parents=True)
        (invalid / ".delibra" / "manifest.json").write_text(
            json.dumps({"format": "not-delibra", "id": "x"}), encoding="utf-8"
        )
        rejected = client.post(
            "/projects", data={"name": "Invalid", "path": str(invalid)}
        )
        assert rejected.status_code == 422
    assert [item.id for item in RegistryStore(settings.home).list_projects()] == [
        project_id
    ]


@pytest.mark.asyncio
async def test_rename_unregister_and_run_start_are_serialized_without_stranding(
    tmp_path: Path,
) -> None:
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Race", project_path)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Runner",
        agent="fake",
        model="sleep",
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
        adapter_factory_override=lambda config: SleepingAdapter(),
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as client:
            start, renamed = await asyncio.wait_for(
                asyncio.gather(
                    app.state.manager.start(project.id, session.id, "Race rename"),
                    client.post(
                        f"/projects/{project.id}/rename",
                        data={"name": "Race renamed"},
                        follow_redirects=False,
                    ),
                ),
                timeout=3,
            )
            assert renamed.status_code in {303, 409}
            assert app.state.manager.active_key(project.id, session.id) == start
            await app.state.manager.cancel(start)

            start_result, unregistered = await asyncio.wait_for(
                asyncio.gather(
                    app.state.manager.start(project.id, session.id, "Race unregister"),
                    client.post(
                        f"/projects/{project.id}/unregister",
                        follow_redirects=False,
                    ),
                    return_exceptions=True,
                ),
                timeout=3,
            )
            assert not isinstance(unregistered, BaseException)
            assert unregistered.status_code in {303, 409}
            if unregistered.status_code == 303:
                assert isinstance(start_result, NotFoundError)
                assert app.state.manager.active_key(project.id, session.id) is None
            else:
                assert not isinstance(start_result, BaseException)
                await app.state.manager.cancel(start_result)
                removed = await client.post(
                    f"/projects/{project.id}/unregister", follow_redirects=False
                )
                assert removed.status_code == 303
    assert not RegistryStore(settings.home).list_projects()
    assert (project_path / ".delibra").is_dir()


def test_project_chat_is_primary_and_settings_remains_available(tmp_path: Path) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "primary-project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Primary", project_path)

    with TestClient(app, base_url="http://localhost") as client:
        primary = client.get(f"/projects/{project.id}", follow_redirects=False)
        chat = client.get(f"/projects/{project.id}/chat")
        management = client.get(f"/projects/{project.id}/settings")
        created = client.post(
            f"/projects/{project.id}/sessions",
            data={
                "name": "Claude",
                "agent": "claude",
                "model": "sonnet",
                "effort": "low",
                "role_instructions": "",
            },
            follow_redirects=False,
        )

    session = ProjectStore(project).list_sessions()[0]
    assert primary.status_code == 303
    assert primary.headers["location"] == f"/projects/{project.id}/chat"
    assert chat.status_code == 200
    assert f'href="/projects/{project.id}/settings"' in chat.text
    assert "Manage" in chat.text
    assert management.status_code == 200
    assert "Sessions" in management.text
    assert created.headers["location"] == (
        f"/projects/{project.id}/chat?agent={session.id}"
    )
