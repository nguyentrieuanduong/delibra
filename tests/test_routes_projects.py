from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.coding import DEFAULT_NETWORK_ALLOWLIST, CodingEnvState, DockerPolicy
from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.pass_prompts import BUILT_IN_PASS_PROMPT_TEMPLATE
from app.storage import (
    ConflictError,
    LockCoordinator,
    NotFoundError,
    ProjectStore,
    RegistryStore,
    StorageError,
)


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


def project_app(tmp_path: Path, docker_controller=None):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    return create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        docker_controller_override=docker_controller,
    ), settings


def timeout_input(contents: str) -> str:
    for match in re.finditer(r"<input\b[^>]*>", contents):
        if 'name="turn_timeout_seconds"' in match.group(0):
            return match.group(0)
    raise AssertionError("turn_timeout_seconds input not found")


def test_project_turn_timeout_setting_renders_and_saves(tmp_path: Path) -> None:
    app, settings = project_app(tmp_path)
    project_dir = tmp_path / "project-timeout"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("Timeout", project_dir)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        inherited = client.get(f"{prefix}/settings")
        saved = client.post(
            f"{prefix}/turn-timeout",
            data={"turn_timeout_seconds": "7"},
            follow_redirects=False,
        )
        explicit = client.get(f"{prefix}/settings")

    assert 'value="2"' in timeout_input(inherited.text)
    assert "Inherited application default" in inherited.text
    assert saved.status_code == 303
    assert ProjectStore(project).configured_turn_timeout_seconds() == 7
    assert 'value="7"' in timeout_input(explicit.text)
    assert "Saved project default" in explicit.text

    with TestClient(app, base_url="http://localhost") as client:
        for invalid_data in (
            {"turn_timeout_seconds": "0"},
            {"turn_timeout_seconds": str(settings.max_run_timeout + 1)},
            {"turn_timeout_seconds": "seven"},
            {},
        ):
            response = client.post(
                f"{prefix}/turn-timeout",
                data=invalid_data,
            )
            assert response.status_code == 422

    assert ProjectStore(project).configured_turn_timeout_seconds() == 7


def test_settings_page_discloses_a_saved_timeout_above_the_lowered_cap(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project_dir = tmp_path / "project-lowered-cap"
    project_dir.mkdir()
    settings = Settings(
        home=home,
        run_timeout=900,
        max_run_timeout=1_800,
    )
    project = RegistryStore(home).register("Lowered Cap", project_dir)
    store = ProjectStore(project)
    store.set_turn_timeout_seconds(3_600, maximum=14_400)
    app = create_app(
        settings_override=settings,
        provider_commands={
            "claude": "/missing/claude",
            "codex": "/missing/codex",
        },
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{quote(project.name, safe='')}/settings"
        )

    assert response.status_code == 200
    field = timeout_input(response.text)
    assert 'max="1800"' in field
    assert 'value="1800"' in field
    assert "Saved project default: 3600 seconds" in response.text
    assert (
        "current application cap limits new turns to 1800 seconds"
        in response.text
    )


def project_with_corrupt_active_auto(
    settings: Settings,
    tmp_path: Path,
    reserve_auto_run,
):
    project_path = tmp_path / "corrupt-active"
    project_path.mkdir()
    project = RegistryStore(settings.home).register(
        "Corrupt Active",
        project_path,
    )
    store = ProjectStore(project)
    for index, name in enumerate(("Alpha", "Beta"), start=1):
        store.create_session(
            SessionConfig(
                id=str(index) * 32,
                name=name,
                agent="fake",
                model="success",
                effort="low",
                role_instructions="",
                cli_session_id=None,
                status="idle",
                created_at="2026-01-01T00:00:00Z",
                rounds=[],
            )
        )
    older = reserve_auto_run(store, auto_id="a" * 32)
    older.status = "converged"
    older.finished_at = "2026-01-01T00:01:00Z"
    older.terminal_reason = "all agents agreed"
    store.save_auto_run(older)
    store.clear_auto_reservation(older.id)

    corrupt_id = "f" * 32
    corrupt_dir = store.auto_runs_root / corrupt_id
    corrupt_dir.mkdir()
    (corrupt_dir / "config.json").write_text("{", encoding="utf-8")
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["active_auto_run_id"] = corrupt_id
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return project


def test_unreadable_active_auto_owner_is_actionable_409(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, settings = project_app(tmp_path)
    project = project_with_corrupt_active_auto(
        settings,
        tmp_path,
        reserve_auto_run,
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions",
            data={
                "name": "Gamma",
                "agent": "claude",
                "model": "sonnet",
                "effort": "low",
                "role_instructions": "",
            },
        )
        chat = client.get(
            f"/projects/{quote(project.name, safe='')}/chat?auto_setup=true"
        )

    assert response.status_code == 409
    assert "Retry Auto migration in project settings" in response.json()["detail"]
    assert "Auto migration is blocked" in chat.text
    assert "data-auto-setup-dialog" not in chat.text
    assert "Auto 1 · converged" not in chat.text


def test_auto_migration_issue_is_visible_retryable_and_escaped(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "blocked-auto"
    project_path.mkdir()
    project = RegistryStore(settings.home).register(
        "Blocked Auto",
        project_path,
    )
    broken = ProjectStore(project).auto_runs_root / "<broken>"
    broken.mkdir()
    (broken / "config.json").write_text("{", encoding="utf-8")

    with TestClient(app, base_url="http://localhost") as client:
        settings_page = client.get(
            f"/projects/{quote(project.name, safe='')}/settings"
        )
        retry = client.post(
            f"/projects/{quote(project.name, safe='')}/auto-migration/retry",
            follow_redirects=False,
        )

    assert "&lt;broken&gt;" in settings_page.text
    assert "Retry Auto migration" in settings_page.text
    assert retry.status_code == 303
    assert retry.headers["location"].endswith("/settings")


@pytest.mark.parametrize("manager_name", ["manager", "auto_manager"])
def test_auto_migration_retry_rejects_in_memory_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_name: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / manager_name
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Retry Guard", project_path)

    with TestClient(app, base_url="http://localhost") as client:
        manager = getattr(app.state, manager_name)
        monkeypatch.setattr(manager, "has_active_project", lambda _: True)
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/auto-migration/retry",
            follow_redirects=False,
        )

    assert response.status_code == 409


def test_unregister_degraded_storage_checks_in_memory_only(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "missing-after-register"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Escape Hatch", project_path)
    project_path.rename(tmp_path / "moved")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/unregister",
            follow_redirects=False,
        )

    assert response.status_code == 303
    with pytest.raises(NotFoundError):
        RegistryStore(settings.home).get(project.id)


def test_project_settings_reports_selected_and_broken_shared_markdown(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "settings-project"
    project_path.mkdir()
    source = project_path / "brief.md"
    source.write_text("brief", encoding="utf-8")
    project = RegistryStore(settings.home).register("Settings", project_path)
    ProjectStore(project).select_shared_markdown("brief.md", 512 * 1024)

    with TestClient(app, base_url="http://localhost") as client:
        selected = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        source.unlink()
        broken = client.get(f"/projects/{quote(project.name, safe='')}/settings")

    assert "brief.md" in selected.text
    assert "Shared context is available" in selected.text
    assert "Shared context is unavailable" in broken.text


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
        registry = RegistryStore(settings.home)
        project = registry.resolve("Canonical")
        project_id = project.id
        assert registered.headers["location"] == "/projects/Canonical/chat"
        assert registry.get(project_id).path == str(real.resolve())
        assert client.post(
            "/projects", data={"name": "Duplicate", "path": str(real)}
        ).status_code == 409

        renamed = client.post(
            f"/projects/{project_id}/rename",
            data={"name": "Renamed"},
            follow_redirects=False,
        )
        assert renamed.status_code == 404
        assert registry.get(project_id).name == "Canonical"

        index = client.get("/")
        assert "Register project" in index.text
        assert "Canonical" in index.text
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
        assert imported.headers["location"] == "/projects/Canonical/chat"

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


@pytest.mark.parametrize("name", [".hidden", "a/b", "CON", "a" * 32])
def test_project_registration_rejects_unsafe_name(
    tmp_path: Path,
    name: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "unsafe-name"
    project_path.mkdir()

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/projects",
            data={"name": name, "path": str(project_path)},
        )

    assert response.status_code == 422
    assert RegistryStore(settings.home).list_projects() == []


def test_project_name_urls_encode_reserved_characters_and_uuid_get_redirects(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register(
        "50% #?&+ off",
        project_path,
    )
    legacy_project_prefix = "/projects/" + project.id

    with TestClient(app, base_url="http://localhost") as client:
        index = client.get("/")
        legacy = client.get(
            f"{legacy_project_prefix}/chat?agent={'a' * 32}",
            follow_redirects=False,
        )
        canonical = client.get(
            "/projects/50%25%20%23%3F%26%2B%20off/chat"
        )

    assert "/projects/50%25%20%23%3F%26%2B%20off" in index.text
    assert legacy.status_code == 302
    assert legacy.headers["location"] == (
        "/projects/50%25%20%23%3F%26%2B%20off/chat"
        f"?agent={'a' * 32}"
    )
    assert canonical.status_code == 200


@pytest.mark.asyncio
async def test_name_and_uuid_mutations_share_the_project_uuid_lock(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Shared Lock", project_path)
    locks = LockCoordinator()
    entered: list[str] = []
    active = 0
    maximum_active = 0

    async def hold(reference: str) -> None:
        nonlocal active, maximum_active
        resolved = registry.resolve(reference)
        async with locks.project_lock(resolved.id):
            active += 1
            maximum_active = max(maximum_active, active)
            entered.append(reference)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(
        hold(project.name),
        hold(project.id),
    )

    assert set(entered) == {project.name, project.id}
    assert maximum_active == 1


def test_legacy_uuid_pass_prompt_redirects_to_canonical_project_name(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Route Matrix", project_path)
    legacy_project_prefix = "/projects/" + project.id

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"{legacy_project_prefix}/pass-prompt",
            data={"pass_prompt_template": "Review {source_path}"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/projects/Route%20Matrix/settings"


def test_canonical_project_request_resolves_registry_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "once"
    project_path.mkdir()
    RegistryStore(settings.home).register("Resolve Once", project_path)

    with TestClient(app, base_url="http://localhost") as client:
        calls = 0
        original = app.state.registry.resolve

        def counted(reference: str):
            nonlocal calls
            calls += 1
            return original(reference)

        monkeypatch.setattr(app.state.registry, "resolve", counted)
        response = client.get("/projects/Resolve%20Once/chat")

    assert response.status_code == 200
    assert calls == 1


def test_canonicalizer_defers_registry_storage_error_to_422_handler(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    RegistryStore(settings.home).register("Duplicate", first)

    with TestClient(app, base_url="http://localhost") as client:
        registry_path = app.state.registry.path
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        duplicate = dict(data["projects"][0])
        duplicate["id"] = "f" * 32
        duplicate["path"] = str(second)
        data["projects"].append(duplicate)
        registry_path.write_text(json.dumps(data), encoding="utf-8")
        response = client.get("/projects/Duplicate/chat")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "project name is registered more than once"
    }


def test_startup_synchronizes_manifest_name_from_registry(tmp_path: Path) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Registry Name", project_path)
    manifest_path = project_path / ".delibra" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "Stale Name"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with TestClient(app, base_url="http://localhost"):
        pass

    synchronized = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert synchronized["name"] == project.name


def test_startup_reconciles_project_after_manifest_name_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Read Only Carrier", project_path)
    sync_calls: list[tuple[str, str]] = []
    reconciliation_calls: list[str] = []
    original_migrate = ProjectStore.migrate_session_directories

    def fail_sync(store: ProjectStore, name: str) -> None:
        sync_calls.append((store.project.id, name))
        raise StorageError("carrier is read-only")

    def record_reconciliation(store: ProjectStore):
        reconciliation_calls.append(store.project.id)
        return original_migrate(store)

    monkeypatch.setattr(ProjectStore, "sync_manifest_name", fail_sync)
    monkeypatch.setattr(
        ProjectStore,
        "migrate_session_directories",
        record_reconciliation,
    )

    with TestClient(app, base_url="http://localhost"):
        pass

    assert sync_calls == [(project.id, project.name)]
    assert reconciliation_calls == [project.id]


def test_registered_project_name_is_immutable_and_rebind_keeps_it(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Immutable", source)
    shutil.copytree(source, target)
    source.rename(tmp_path / "moved-away")

    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(
            f"/projects/{quote(project.name, safe='')}/rename",
            data={"name": "Changed"},
        ).status_code == 404
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/rebind",
            data={"path": str(target)},
            follow_redirects=False,
        )
        index = client.get("/")

    assert response.status_code == 303
    assert RegistryStore(settings.home).get(project.id).name == "Immutable"
    assert "200 UTF-8 bytes" in index.text
    assert "Rename" not in index.text


def test_active_auto_reservation_blocks_project_unregister(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "reserved-project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Reserved", project_path)
    store = ProjectStore(project)

    with TestClient(app, base_url="http://localhost") as client:
        for index, name in enumerate(("Alpha", "Beta")):
            store.create_session(
                SessionConfig(
                    id=str(index + 1) * 32,
                    name=name,
                    agent="fake",
                    model="success",
                    effort="low",
                    role_instructions="",
                    cli_session_id=None,
                    status="idle",
                    created_at="2026-07-19T00:00:00Z",
                    rounds=[],
                )
            )
        reserve_auto_run(store)
        removed = client.post(
            f"/projects/{quote(project.name, safe='')}/unregister",
            follow_redirects=False,
        )

    assert removed.status_code == 409
    assert RegistryStore(settings.home).get(project.id).name == "Reserved"


@pytest.mark.asyncio
async def test_unregister_and_run_start_are_serialized_without_stranding(
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
            start_result, unregistered = await asyncio.wait_for(
                asyncio.gather(
                    app.state.manager.start(project.id, session.id, "Race unregister"),
                    client.post(
                        f"/projects/{quote(project.name, safe='')}/unregister",
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
                    f"/projects/{quote(project.name, safe='')}/unregister", follow_redirects=False
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
        primary = client.get(f"/projects/{quote(project.name, safe='')}", follow_redirects=False)
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        management = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        created = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions",
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
    assert primary.headers["location"] == f"/projects/{quote(project.name, safe='')}/chat"
    assert chat.status_code == 200
    assert f'href="/projects/{quote(project.name, safe='')}/settings"' in chat.text
    assert "Manage" in chat.text
    assert management.status_code == 200
    assert "Sessions" in management.text
    assert created.headers["location"] == (
        f"/projects/{quote(project.name, safe='')}/chat?agent={session.id}"
    )


def test_project_settings_saves_escapes_and_resets_default_pass_prompt(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "pass-settings"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Pass settings", project_path)
    store = ProjectStore(project)
    for index, name in enumerate(("Alpha", "Beta")):
        store.create_session(
            SessionConfig(
                id=str(index + 1) * 32,
                name=name,
                agent="fake",
                model="success",
                effort="low",
                role_instructions="",
                cli_session_id=None,
                status="idle",
                created_at="2026-07-19T00:00:00Z",
                rounds=[],
            )
        )
    custom = "Review </textarea><script>alert(1)</script> at {source_path}"

    with TestClient(app, base_url="http://localhost") as client:
        reserve_auto_run(store, auto_id="f" * 32)
        initial = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        saved = client.post(
            f"/projects/{quote(project.name, safe='')}/pass-prompt",
            data={"pass_prompt_template": custom},
            follow_redirects=False,
        )
        rendered = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        reset = client.post(
            f"/projects/{quote(project.name, safe='')}/pass-prompt/reset",
            follow_redirects=False,
        )

    assert "Review the following document and give your critique." in initial.text
    assert "{source_path}" in initial.text
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/projects/{quote(project.name, safe='')}/settings"
    assert "&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in rendered.text
    assert "</textarea><script>" not in rendered.text
    assert store.active_auto_run_id() == "f" * 32
    assert reset.status_code == 303
    assert store.effective_pass_prompt_template() == BUILT_IN_PASS_PROMPT_TEMPLATE

    with TestClient(app, base_url="http://localhost") as client:
        after_reset = client.get(f"/projects/{quote(project.name, safe='')}/settings")
    assert "Review the following document" in after_reset.text


@pytest.mark.parametrize(
    "template",
    ["", "missing path", "{source_path} {source_path}", "x" * 10_001],
)
def test_project_pass_prompt_rejects_invalid_update_without_mutation(
    tmp_path: Path,
    template: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "invalid-pass-settings"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Pass settings", project_path)
    store = ProjectStore(project)
    original = "Keep {source_path}"
    store.set_pass_prompt_template(original)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/pass-prompt",
            data={"pass_prompt_template": template},
        )
    assert response.status_code == 422
    assert store.effective_pass_prompt_template() == original


def test_index_renders_a_stale_project_and_rebinds_its_moved_directory(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Movable", original)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="",
        cli_session_id="old-location-native-id",
        status="idle",
        created_at="2026-07-29T00:00:00Z",
        rounds=[
            RoundRecord(
                n=1,
                status="complete",
                error=None,
                warnings=[],
                agent="claude",
                model="sonnet",
                effort="high",
                started_at="2026-07-29T00:00:00Z",
                finished_at="2026-07-29T00:00:01Z",
                source=SourceDescriptor(type="user"),
            )
        ],
    )
    store.create_session(session)
    (store.rounds_dir(session.id) / "round-01.prompt.md").write_text(
        "Initial prompt",
        encoding="utf-8",
    )
    (store.rounds_dir(session.id) / "round-01.md").write_text(
        "Initial output",
        encoding="utf-8",
    )
    moved = tmp_path / "moved"
    original.rename(moved)

    with TestClient(app, base_url="http://localhost") as client:
        index = client.get("/")
        rebound = client.post(
            f"/projects/{quote(project.name, safe='')}/rebind",
            data={"path": str(moved)},
            follow_redirects=False,
        )
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert index.status_code == 200
    assert "Registered path unavailable" in index.text
    assert f'action="/projects/{quote(project.name, safe='')}/rebind"' in index.text
    assert rebound.status_code == 303
    assert rebound.headers["location"] == f"/projects/{quote(project.name, safe='')}/chat"
    assert registry.get(project.id).path == str(moved.resolve())
    rebound_store = ProjectStore(registry.get(project.id))
    assert rebound_store.load_session(session.id).cli_session_id is None
    assert rebound_store.load_round_artifact(
        session.id,
        1,
        "output",
        1_024,
    ) == b"Initial output"
    assert chat.status_code == 200
    assert "Researcher" in chat.text


def test_index_renders_every_stale_project_card(tmp_path: Path) -> None:
    app, settings = project_app(tmp_path)
    registry = RegistryStore(settings.home)
    names = ("First stale", "Second stale")
    for index, name in enumerate(names):
        original = tmp_path / f"original-{index}"
        original.mkdir()
        registry.register(name, original)
        original.rename(tmp_path / f"moved-{index}")

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text.count("Registered path unavailable") == 2
    for name in names:
        assert name in response.text


def test_copy_then_rebind_leaves_the_old_directory_untouched(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    registry = RegistryStore(settings.home)
    original = tmp_path / "original"
    original.mkdir()
    project = registry.register("Copied move", original)
    original_store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="",
        cli_session_id="old-location-native-id",
        status="idle",
        created_at="2026-07-29T00:00:00Z",
        rounds=[],
    )
    original_store.create_session(session)
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)

    with TestClient(app, base_url="http://localhost") as client:
        old_paths = sorted(
            path.relative_to(original).as_posix() for path in original.rglob("*")
        )
        old_files = {
            path.relative_to(original).as_posix(): path.read_bytes()
            for path in original.rglob("*")
            if path.is_file()
        }
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/rebind",
            data={"path": str(copied)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert registry.get(project.id).path == str(copied.resolve())
    assert original.is_dir()
    assert sorted(
        path.relative_to(original).as_posix() for path in original.rglob("*")
    ) == old_paths
    assert {
        path.relative_to(original).as_posix(): path.read_bytes()
        for path in original.rglob("*")
        if path.is_file()
    } == old_files
    rebound = ProjectStore(registry.get(project.id))
    assert rebound.load_session(session.id).cli_session_id is None


def test_failed_rebind_keeps_the_stale_registry_path(tmp_path: Path) -> None:
    app, settings = project_app(tmp_path)
    original = tmp_path / "original"
    wrong = tmp_path / "wrong"
    original.mkdir()
    wrong.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Movable", original)
    registry.register("Wrong", wrong)
    original.rename(tmp_path / "moved")
    before = registry.get(project.id).path

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/rebind",
            data={"path": str(wrong)},
        )

    assert response.status_code == 409
    assert registry.get(project.id).path == before


def test_index_does_not_parse_every_session_for_migration_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Index", project_path)
    ProjectStore(project).create_session(
        SessionConfig(
            id="a" * 32,
            name="Researcher",
            agent="claude",
            model="sonnet",
            effort="high",
            role_instructions="",
            cli_session_id=None,
            status="idle",
            created_at="2026-07-29T00:00:00Z",
            rounds=[],
        )
    )

    with TestClient(app, base_url="http://localhost") as client:
        def forbidden_full_scan(_store: ProjectStore):
            raise AssertionError("index parsed full session configs")

        monkeypatch.setattr(
            ProjectStore,
            "session_migration_status",
            forbidden_full_scan,
        )
        response = client.get("/")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rebind_is_serialized_against_a_running_session(
    tmp_path: Path,
) -> None:
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    original = tmp_path / "original"
    original.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Busy", original)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="fake",
        model="sleep",
        effort="low",
        role_instructions="",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-29T00:00:00Z",
        rounds=[],
    )
    store.create_session(session)
    # Rebind to a copy so the same-path guard cannot mask the active-work guard.
    moved = tmp_path / "moved"
    shutil.copytree(original, moved)
    app = create_app(
        settings_override=settings,
        adapter_factory_override=lambda config: SleepingAdapter(),
    )
    async with app.router.lifespan_context(app):
        key = await app.state.manager.start(project.id, session.id, "Wait")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            response = await client.post(
                f"/projects/{quote(project.name, safe='')}/rebind",
                data={"path": str(moved)},
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "project has active agent work"}
        assert registry.get(project.id).path == str(original.resolve())
        await app.state.manager.cancel(key)


def test_same_path_rebind_rejects_without_clearing_native_resume(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Already bound", project_path)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="",
        cli_session_id="live-native",
        status="idle",
        created_at="2026-07-30T00:00:00Z",
        rounds=[],
    )
    store.create_session(session)
    config_path = store.session_dir(session.id) / "config.json"
    backup_path = config_path.with_name("config.json.bak")
    before_config = config_path.read_bytes()
    before_backup = backup_path.read_bytes() if backup_path.exists() else None

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/rebind",
            data={"path": str(project_path)},
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "project is already bound to this path"}
    assert registry.get(project.id) == project
    assert config_path.read_bytes() == before_config
    assert (
        backup_path.read_bytes() if backup_path.exists() else None
    ) == before_backup
    assert ProjectStore(project).load_session(session.id).cli_session_id == (
        "live-native"
    )


@pytest.mark.parametrize(
    "case",
    ["traversal", "delibra", "git", "missing", "symlink", "unsafe", "too-many"],
)
def test_project_writable_roots_reject_invalid_candidates_atomically(
    tmp_path: Path,
    case: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "writable-project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Writable", project_path)
    store = ProjectStore(project)
    if case == "traversal":
        submitted = "src/../outside"
    elif case == "delibra":
        submitted = ".DELIBRA"
    elif case == "git":
        submitted = ".Git"
    elif case == "missing":
        submitted = "missing"
    elif case == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (project_path / "linked").symlink_to(outside, target_is_directory=True)
        submitted = "linked"
    elif case == "unsafe":
        submitted = "src,unsafe"
    else:
        roots = [f"root-{index:02d}" for index in range(17)]
        for root in roots:
            (project_path / root).mkdir()
        submitted = "\n".join(roots)
    before = store.manifest_path.read_bytes()

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/writable-roots",
            data={"writable_roots": submitted},
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert store.manifest_path.read_bytes() == before


@pytest.mark.parametrize("candidate_kind", ["missing", "symlink"])
def test_project_writable_root_candidate_hidden_by_agent_ancestor_is_rejected(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "writable-project"
    (project_path / "src").mkdir(parents=True)
    project = RegistryStore(settings.home).register("Writable", project_path)
    store = ProjectStore(project)
    config = SessionConfig(
        id="a" * 32,
        name="Agent",
        agent="claude",
        model="sonnet",
        effort="low",
        role_instructions="",
        cli_session_id="native-id",
        status="idle",
        created_at="2026-09-10T00:00:00Z",
        rounds=[],
        writable_roots=["src"],
    )
    store.create_session(config)
    candidate = "src/new"
    if candidate_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (project_path / candidate).symlink_to(outside, target_is_directory=True)
    manifest_before = store.manifest_path.read_bytes()
    config_path = store.session_dir(config.id) / "config.json"
    config_before = config_path.read_bytes()

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/writable-roots",
            data={"writable_roots": candidate},
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert store.manifest_path.read_bytes() == manifest_before
    assert config_path.read_bytes() == config_before


def test_project_writable_roots_union_cap_is_atomic_for_existing_agent(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "writable-project"
    roots = [f"root-{index:02d}" for index in range(17)]
    for root in roots:
        (project_path / root).mkdir(parents=True)
    project = RegistryStore(settings.home).register("Writable", project_path)
    store = ProjectStore(project)
    config = SessionConfig(
        id="a" * 32,
        name="Agent",
        agent="claude",
        model="sonnet",
        effort="low",
        role_instructions="",
        cli_session_id="native-id",
        status="idle",
        created_at="2026-09-10T00:00:00Z",
        rounds=[],
        writable_roots=roots[:16],
    )
    store.create_session(config)
    manifest_before = store.manifest_path.read_bytes()
    config_path = store.session_dir(config.id) / "config.json"
    config_before = config_path.read_bytes()

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/writable-roots",
            data={"writable_roots": roots[16]},
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert store.manifest_path.read_bytes() == manifest_before
    assert config_path.read_bytes() == config_before


@pytest.mark.parametrize("manager_name", ["manager", "auto_manager"])
def test_project_writable_roots_reject_in_memory_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_name: str,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "writable-project"
    (project_path / "src").mkdir(parents=True)
    project = RegistryStore(settings.home).register("Writable", project_path)
    store = ProjectStore(project)
    before = store.manifest_path.read_bytes()

    with TestClient(app, base_url="http://localhost") as client:
        manager = getattr(app.state, manager_name)
        monkeypatch.setattr(manager, "has_active_project", lambda _: True)
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/writable-roots",
            data={"writable_roots": "src"},
            follow_redirects=False,
        )

    assert response.status_code == 409
    assert store.manifest_path.read_bytes() == before


def test_project_writable_roots_update_and_clear_around_stale_agent_addition(
    tmp_path: Path,
) -> None:
    app, settings = project_app(tmp_path)
    project_path = tmp_path / "writable-project"
    stale = project_path / "stale"
    stale.mkdir(parents=True)
    (project_path / "valid").mkdir()
    project = RegistryStore(settings.home).register("Writable", project_path)
    store = ProjectStore(project)
    config = SessionConfig(
        id="a" * 32,
        name="Agent",
        agent="claude",
        model="sonnet",
        effort="low",
        role_instructions="",
        cli_session_id=None,
        status="idle",
        created_at="2026-09-10T00:00:00Z",
        rounds=[],
        writable_roots=["stale"],
    )
    store.create_session(config)
    stale.rmdir()
    route = f"/projects/{quote(project.name, safe='')}/writable-roots"

    with TestClient(app, base_url="http://localhost") as client:
        updated = client.post(
            route,
            data={"writable_roots": " valid \n\n"},
            follow_redirects=False,
        )
        page = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        cleared = client.post(
            route,
            data={"writable_roots": ""},
            follow_redirects=False,
        )

    assert updated.status_code == 303
    assert 'id="project-writable-roots"' in page.text
    assert ">valid</textarea>" in page.text
    assert cleared.status_code == 303
    assert store.effective_writable_roots() == []
    with pytest.raises(StorageError, match="stale"):
        store.resolve_session_writable_roots(config.id)


LOCAL = ("127.0.0.1", 5000)
LAN = ("192.168.1.20", 5000)
VENV_STUB = (
    "#!/bin/sh\n"
    'mkdir -p "$3/bin"\n'
    "printf '#!/bin/sh\\necho Python 3.12.4\\n' > \"$3/bin/python\"\n"
    'chmod +x "$3/bin/python"\n'
)


def coding_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_controller=None):
    app, settings = project_app(tmp_path, docker_controller=docker_controller)
    project_dir = tmp_path / "coded"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("Coded", project_dir)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text(VENV_STUB)
    (bin_dir / "python3").chmod(0o755)
    # The stub must win, and no real conda/python3.12 may be found.
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.delenv("CONDA_EXE", raising=False)
    # Never resolve real DNS in tests.
    monkeypatch.setattr("app.routes.projects.private_resolution_warnings", lambda hosts: [])
    return app, project, project_dir, f"/projects/{quote(project.name, safe='')}"


def wait_env(project, status: str, timeout: float = 5.0) -> CodingEnvState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = ProjectStore(project).coding_policy().env
        if state.status == status:
            return state
        time.sleep(0.05)
    raise AssertionError(f"coding env never reached {status}")


def test_enabling_coding_creates_env_and_persists_allowlist(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            f"{prefix}/coding",
            data={"enabled": "true", "network_allowlist": "pypi.org\nfiles.pythonhosted.org"},
            follow_redirects=False,
        )
        state = wait_env(project, "ready")
        page = client.get(f"{prefix}/settings")

    assert saved.status_code == 303
    policy = ProjectStore(project).coding_policy()
    assert policy.enabled is True
    assert policy.network_allowlist == ["pypi.org", "files.pythonhosted.org"]
    assert (state.tool, state.python, state.created_by_delibra) == ("venv", "3.12.4", True)
    assert 'id="coding-heading"' in page.text
    assert "Environment: ready · venv · Python 3.12.4" in page.text


@pytest.mark.parametrize("allowlist", ["https://pypi.org", "pypi.org\n\nfiles.pythonhosted.org"])
def test_invalid_allowlist_is_rejected_and_nothing_is_stored(tmp_path, monkeypatch, allowlist) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(f"{prefix}/coding", data={"enabled": "true", "network_allowlist": allowlist})

    assert response.status_code == 422
    assert ProjectStore(project).coding_policy().enabled is False


def test_lan_client_cannot_change_coding(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        saved = client.post(f"{prefix}/coding", data={"enabled": "true"})
        retry = client.post(f"{prefix}/coding/env/retry")
        page = client.get(f"{prefix}/settings")

    assert saved.status_code == 403
    assert retry.status_code == 403
    assert ProjectStore(project).coding_policy().enabled is False
    assert "Only this machine can change coding settings" in page.text


def test_coding_save_requires_inactive_auto(tmp_path, monkeypatch) -> None:
    app, _project, _dir, prefix = coding_project(tmp_path, monkeypatch)

    def active(_self) -> None:
        raise ConflictError("an Auto run is active")

    monkeypatch.setattr(ProjectStore, "require_auto_inactive", active)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(f"{prefix}/coding", data={"enabled": "true"})

    assert response.status_code == 409


def test_private_resolution_warning_is_stored_and_shown(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    warning = "intranet.example.com resolves to a private address (10.1.2.3)"
    monkeypatch.setattr(
        "app.routes.projects.private_resolution_warnings", lambda hosts: [warning]
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "intranet.example.com"},
            follow_redirects=False,
        )
        page = client.get(f"{prefix}/settings")

    assert saved.status_code == 303
    assert ProjectStore(project).coding_policy().network_warnings == [warning]
    assert warning in page.text


def test_startup_turns_interrupted_creation_into_retryable_error(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    store = ProjectStore(project)
    store.set_coding_enabled(True)
    store.set_coding_env(CodingEnvState(status="creating", updated_at="2026-10-15T00:00:00Z"))

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{prefix}/settings")

    state = ProjectStore(project).coding_policy().env
    assert state.status == "error"
    assert "interrupted" in (state.error or "")
    # Ownership of a leftover ./envs is unknowable after a crash.
    assert state.created_by_delibra is False
    assert "Retry environment" in page.text


def test_retry_removes_only_a_partial_env_delibra_created(tmp_path, monkeypatch) -> None:
    app, project, project_dir, prefix = coding_project(tmp_path, monkeypatch)
    store = ProjectStore(project)
    store.set_coding_enabled(True)
    (project_dir / "envs").mkdir()
    (project_dir / "envs" / "partial").write_text("half-built\n")
    store.set_coding_env(
        CodingEnvState(status="error", error="boom", created_by_delibra=True, updated_at="t0")
    )

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        retry = client.post(f"{prefix}/coding/env/retry", follow_redirects=False)
        state = wait_env(project, "ready")

    assert retry.status_code == 303
    assert state.tool == "venv"
    assert not (project_dir / "envs" / "partial").exists()


def test_retry_never_deletes_an_env_delibra_did_not_create(tmp_path, monkeypatch) -> None:
    app, project, project_dir, prefix = coding_project(tmp_path, monkeypatch)
    store = ProjectStore(project)
    store.set_coding_enabled(True)
    (project_dir / "envs").mkdir()
    (project_dir / "envs" / "mine").write_text("user data\n")
    store.set_coding_env(
        CodingEnvState(status="error", error="boom", created_by_delibra=False, updated_at="t0")
    )

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        retry = client.post(f"{prefix}/coding/env/retry")

    assert retry.status_code == 422
    assert (project_dir / "envs" / "mine").read_text() == "user data\n"


def test_guidance_files_are_saved_from_the_textarea(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\n")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "pypi.org", "guidance_files": str(guide)},
            follow_redirects=False,
        )
        page = client.get(f"{prefix}/settings")

    assert saved.status_code == 303
    assert ProjectStore(project).coding_policy().guidance_files == [str(guide)]
    assert str(guide) in page.text


def test_code_craft_preset_keeps_only_existing_files(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    present = tmp_path / "present.md"
    present.write_text("# Present\n")
    monkeypatch.setattr(
        "app.routes.projects.CODE_CRAFT_SUGGESTED_GUIDANCE",
        (str(present), str(tmp_path / "absent.md")),
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "pypi.org", "guidance_preset": "code-craft"},
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert ProjectStore(project).coding_policy().guidance_files == [str(present)]


def test_code_craft_preset_without_files_is_refused_and_keeps_guidance(
    tmp_path, monkeypatch
) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\n")
    absent = tmp_path / "absent.md"
    monkeypatch.setattr(
        "app.routes.projects.CODE_CRAFT_SUGGESTED_GUIDANCE",
        (str(absent), str(tmp_path / "also-absent.md")),
    )
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "pypi.org", "guidance_files": str(guide)},
            follow_redirects=False,
        )
        response = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "example.org", "guidance_preset": "code-craft"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"None of the code-craft skill files exist (looked for {absent} and 1 more); "
        "guidance was not changed"
    )
    policy = ProjectStore(project).coding_policy()
    assert policy.guidance_files == [str(guide)]
    assert policy.network_allowlist == ["pypi.org"]


def test_invalid_guidance_path_is_rejected(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "pypi.org", "guidance_files": "relative.md"},
        )

    assert response.status_code == 422
    assert ProjectStore(project).coding_policy().guidance_files == []
    policy = ProjectStore(project).coding_policy()
    assert policy.network_allowlist == list(DEFAULT_NETWORK_ALLOWLIST)
    assert policy.enabled is False


def test_code_craft_preset_button_follows_the_auto_disable(tmp_path, monkeypatch) -> None:
    app, _project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{prefix}/settings").text
    assert 'name="guidance_preset" value="code-craft" data-disable-during-auto' in page


def test_enabling_coding_in_a_git_project_adds_local_excludes(tmp_path, monkeypatch) -> None:
    app, project, project_dir, prefix = coding_project(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", "-b", "main", str(project_dir)], check=True)
    exclude = project_dir / ".git" / "info" / "exclude"
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post(f"{prefix}/coding", data={"network_allowlist": "pypi.org"}, follow_redirects=False)
        assert "/envs/" not in exclude.read_text()
        client.post(
            f"{prefix}/coding",
            data={"enabled": "true", "network_allowlist": "pypi.org"},
            follow_redirects=False,
        )
        wait_env(project, "ready")
        page = client.get(f"{prefix}/settings").text

    text = exclude.read_text()
    assert "/.delibra/\n" in text and "/envs/\n" in text
    assert not (project_dir / ".gitignore").exists()
    assert ".git/info/exclude" in page


def test_git_ignore_list_is_configurable_per_project(tmp_path, monkeypatch) -> None:
    app, project, project_dir, prefix = coding_project(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", "-b", "main", str(project_dir)], check=True)
    exclude = project_dir / ".git" / "info" / "exclude"
    form = {"enabled": "true", "network_allowlist": "pypi.org"}
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post(f"{prefix}/coding", data={**form, "git_exclude": "custom-out/\r\n*.tmp\r\n"}, follow_redirects=False)
        custom, custom_text = ProjectStore(project).coding_policy().git_exclude, exclude.read_text()
        client.post(f"{prefix}/coding", data=form, follow_redirects=False)
        kept = ProjectStore(project).coding_policy().git_exclude
        page = client.get(f"{prefix}/settings").text
        client.post(f"{prefix}/coding", data={**form, "git_exclude_preset": "default"}, follow_redirects=False)
        reset, default_text = ProjectStore(project).coding_policy().git_exclude, exclude.read_text()

    assert custom == kept == ["custom-out/", "*.tmp"]
    assert "/envs/\n" in custom_text and "custom-out/\n" in custom_text
    assert "__pycache__/" not in custom_text
    assert "custom-out/\n*.tmp</textarea>" in page and "(custom)" in page
    assert reset is None
    assert "__pycache__/" in default_text and "custom-out/" not in default_text


def test_submitting_the_default_ignore_list_keeps_following_it(tmp_path, monkeypatch) -> None:
    from app.build_git import default_git_exclude

    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "pypi.org", "git_exclude": "\r\n".join(default_git_exclude())},
            follow_redirects=False,
        )
        page = client.get(f"{prefix}/settings").text

    assert ProjectStore(project).coding_policy().git_exclude is None
    assert "(Delibra&#39;s default)" in page


def test_an_invalid_git_ignore_list_changes_nothing(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(
            f"{prefix}/coding",
            data={"network_allowlist": "example.org", "git_exclude": "ok/\n# >>> Delibra coding mode"},
        )

    assert response.status_code == 422
    policy = ProjectStore(project).coding_policy()
    assert policy.git_exclude is None
    assert policy.network_allowlist == list(DEFAULT_NETWORK_ALLOWLIST)


def test_coding_hint_does_not_claim_the_browser_is_unlimited(tmp_path, monkeypatch) -> None:
    app, _project, _dir, prefix = coding_project(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{prefix}/settings").text
    assert "web fetch, browser) are not limited" not in page
    assert "browser features are off in build turns" in page


class FakeDockerController:
    def __init__(self) -> None:
        self.stopped = 0
        self.lima_available = True
        self.prepared: list[Path] = []
        self.deleted: list[str] = []
        self.local_error: str | None = None
        self.prepare_result: DockerPolicy | None = None
        self.on_adopt = None  # sync callable run inside adopt_local
        self.on_delete = None  # async callable run inside delete
        self.prepare_gate: asyncio.Event | None = None
        self.instance: str | None = None

    async def stop_all(self, *, include_held: bool = False) -> None:
        self.stopped += 1

    async def lima_status(self):
        from app.docker_backend import LimaStatus
        return LimaStatus("/usr/local/bin/limactl", (2, 2, 1), None if self.lima_available else "limactl is not on PATH")

    async def prepare_lima(self, project_id: str, project_root: Path) -> DockerPolicy:
        self.prepared.append(project_root)
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        return self.prepare_result or DockerPolicy(
            enabled=True, backend="lima", status="ready", lima_instance="delibra-0123456789",
            lima_version="2.2.1", host_loopback_closed=False, docker_version="29.0.0", updated_at="t",
        )

    async def adopt_local(self) -> DockerPolicy:
        from app.docker_backend import DockerUnavailable
        if self.local_error is not None:
            raise DockerUnavailable(self.local_error)
        if self.on_adopt is not None:
            self.on_adopt()
        return DockerPolicy(
            enabled=True, backend="local", status="ready",
            local_endpoint="/Users/me/.docker/run/docker.sock", local_acknowledged_at="t",
            docker_version="29.0.0", updated_at="t",
        )

    async def delete(self, instance: str) -> None:
        if self.on_delete is not None:
            await self.on_delete()
        self.deleted.append(instance)

    def existing_instance(self, project_id: str) -> str | None:
        return self.instance


def test_startup_marks_interrupted_docker_preparation_and_stops_stale_vms(tmp_path) -> None:
    controller = FakeDockerController()
    app, settings = project_app(tmp_path, docker_controller=controller)
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("P", project_dir)
    ProjectStore(project).set_coding_docker(DockerPolicy(enabled=True, status="preparing", updated_at="t"))

    with TestClient(app, base_url="http://localhost"):
        pass

    docker = ProjectStore(project).coding_policy().docker
    assert (docker.status, docker.error) == ("error", "Docker preparation was interrupted; retry to continue")
    assert controller.stopped >= 1


def docker_project(tmp_path, monkeypatch, controller: FakeDockerController):
    return coding_project(tmp_path, monkeypatch, docker_controller=controller)


def wait_docker(project, status: str, timeout: float = 5.0) -> DockerPolicy:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = ProjectStore(project).coding_policy().docker
        if state.status == status:
            return state
        time.sleep(0.05)
    raise AssertionError(f"docker never reached {status}")


def enable_coding(client, prefix, project) -> None:
    client.post(f"{prefix}/coding", data={"enabled": "true", "network_allowlist": "pypi.org"})
    wait_env(project, "ready")


def test_enabling_docker_prepares_the_lima_vm(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, project_dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        saved = client.post(f"{prefix}/coding/docker", data={"enabled": "true"}, follow_redirects=False)
        state = wait_docker(project, "ready")
        page = client.get(f"{prefix}/settings")

    assert saved.status_code == 303
    assert controller.prepared == [project_dir.resolve()]
    assert (state.backend, state.lima_instance) == ("lima", "delibra-0123456789")
    assert "Docker: ready · Lima VM 2.2.1 · Not isolated" in page.text


def test_docker_needs_coding_enabled(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, FakeDockerController())
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
    assert response.status_code == 422
    assert ProjectStore(project).coding_policy().docker.enabled is False


@pytest.mark.parametrize("route", ["", "/retry", "/local", "/lima", "/delete"])
def test_lan_client_cannot_change_docker(tmp_path, monkeypatch, route) -> None:
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, FakeDockerController())
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        response = client.post(f"{prefix}/coding/docker{route}", data={"enabled": "true", "acknowledge": "true"})
    assert response.status_code == 403


def test_local_docker_needs_lima_unavailable_and_an_acknowledgement(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        while_lima_works = client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})
        controller.lima_available = False
        unacknowledged = client.post(f"{prefix}/coding/docker/local", data={})
        adopted = client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"}, follow_redirects=False)

    assert while_lima_works.status_code == 409
    assert unacknowledged.status_code == 422
    assert adopted.status_code == 303
    docker = ProjectStore(project).coding_policy().docker
    assert (docker.backend, docker.status, docker.lima_instance) == ("local", "ready", "delibra-0123456789")


def test_saving_coding_settings_keeps_docker_state(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, FakeDockerController())
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        before = wait_docker(project, "ready")
        client.post(f"{prefix}/coding", data={"enabled": "true", "network_allowlist": "github.com"})

    assert ProjectStore(project).coding_policy().docker == before


def test_deleting_the_vm_turns_docker_off(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        deleted = client.post(f"{prefix}/coding/docker/delete", follow_redirects=False)

    assert deleted.status_code == 303
    assert controller.deleted == ["delibra-0123456789"]
    docker = ProjectStore(project).coding_policy().docker
    assert (docker.enabled, docker.status, docker.lima_instance) == (False, "absent", None)


def test_local_adoption_refuses_once_lima_becomes_available(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    controller.lima_available = False
    controller.on_adopt = lambda: setattr(controller, "lima_available", True)
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        response = client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})

    assert response.status_code == 409
    assert ProjectStore(project).coding_policy().docker.backend is None


def test_deleting_a_leftover_vm_keeps_local_docker(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        controller.lima_available = False
        client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})
        deleted = client.post(f"{prefix}/coding/docker/delete", follow_redirects=False)

    assert deleted.status_code == 303
    assert controller.deleted == ["delibra-0123456789"]
    docker = ProjectStore(project).coding_policy().docker
    assert (docker.enabled, docker.backend, docker.status, docker.lima_instance) == (True, "local", "ready", None)


def test_a_failed_leftover_vm_deletion_keeps_local_docker_throughout(tmp_path, monkeypatch) -> None:
    # Codex plan review P2: "deleting" must not drop the local backend.
    from app.docker_backend import DockerUnavailable

    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    seen: dict[str, DockerPolicy] = {}

    async def failing_delete() -> None:
        seen["during"] = ProjectStore(project).coding_policy().docker
        raise DockerUnavailable("limactl delete failed: busy")

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        controller.lima_available = False
        client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})
        controller.on_delete = failing_delete
        client.post(f"{prefix}/coding/docker/delete")

    assert (seen["during"].status, seen["during"].backend) == ("deleting", "local")
    docker = ProjectStore(project).coding_policy().docker
    assert (docker.backend, docker.status, docker.lima_instance) == ("local", "ready", "delibra-0123456789")


def test_startup_restores_local_docker_after_an_interrupted_vm_deletion(tmp_path) -> None:
    controller = FakeDockerController()
    app, settings = project_app(tmp_path, docker_controller=controller)
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("P", project_dir)
    ProjectStore(project).set_coding_docker(DockerPolicy(
        enabled=True, backend="local", status="deleting", lima_instance="delibra-0123456789",
        local_endpoint="/Users/me/.docker/run/docker.sock", local_acknowledged_at="t", updated_at="t",
    ))

    with TestClient(app, base_url="http://localhost"):
        pass

    docker = ProjectStore(project).coding_policy().docker
    assert (docker.enabled, docker.backend, docker.status, docker.lima_instance) == (
        True, "local", "ready", "delibra-0123456789",
    )


def test_settings_never_call_lima_isolated_and_disclose_every_risk(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        controller.lima_available = False
        client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})
        controller.lima_available = True
        page = client.get(f"{prefix}/settings").text

    assert "Switch to a Lima VM</button>" in page
    assert "(isolated)" not in page
    assert ".env" in page and "your LAN" in page


def test_vm_deletion_runs_outside_the_project_lock(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    seen: dict[str, object] = {}

    async def during_delete() -> None:
        seen["status"] = ProjectStore(project).coding_policy().docker.status
        # Deadlocks (and times out) if the route still held the project lock.
        async with asyncio.timeout(2):
            async with app.state.locks.registry_project_sessions(project.id):
                seen["lock"] = True

    controller.on_delete = during_delete
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "ready")
        deleted = client.post(f"{prefix}/coding/docker/delete", follow_redirects=False)

    assert deleted.status_code == 303
    assert seen == {"status": "deleting", "lock": True}


def test_local_adoption_loses_to_a_change_made_while_it_was_checked(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    controller.lima_available = False
    controller.prepare_result = DockerPolicy(
        enabled=True, status="error", error="boom", lima_instance="delibra-0123456789", updated_at="t"
    )
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    lima_ready = DockerPolicy(
        enabled=True, backend="lima", status="ready", lima_instance="delibra-0123456789",
        lima_version="2.2.1", host_loopback_closed=False, updated_at="later",
    )
    # Stands in for a Retry that finished while adoption awaited docker version.
    controller.on_adopt = lambda: ProjectStore(project).set_coding_docker(lima_ready)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "error")
        response = client.post(f"{prefix}/coding/docker/local", data={"acknowledge": "true"})

    assert response.status_code == 409
    assert ProjectStore(project).coding_policy().docker == lima_ready


def test_cancelled_preparation_keeps_the_vm_deletable(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    controller.prepare_gate = asyncio.Event()  # never set: preparation hangs
    controller.instance = "delibra-0123456789"  # limactl create already ran
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "preparing")
    # Leaving the client shuts the app down, which cancels the preparation.

    docker = ProjectStore(project).coding_policy().docker
    assert (docker.status, docker.lima_instance) == ("error", "delibra-0123456789")
    assert "cancelled" in (docker.error or "")


def test_docker_routes_require_inactive_auto(tmp_path, monkeypatch) -> None:
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, FakeDockerController())
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)

        def active(_self) -> None:
            raise ConflictError("an Auto run is active")

        monkeypatch.setattr(ProjectStore, "require_auto_inactive", active)
        response = client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
    assert response.status_code == 409


def test_settings_offer_local_docker_only_with_its_risks_when_lima_failed(tmp_path, monkeypatch) -> None:
    controller = FakeDockerController()
    controller.lima_available = False
    controller.prepare_result = DockerPolicy(enabled=True, status="error",
                                             error="Lima is unavailable: limactl is not on PATH", updated_at="t")
    app, project, _dir, prefix = docker_project(tmp_path, monkeypatch, controller)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        enable_coding(client, prefix, project)
        client.post(f"{prefix}/coding/docker", data={"enabled": "true"})
        wait_docker(project, "error")
        page = client.get(f"{prefix}/settings").text

    assert "Lima is unavailable: limactl is not on PATH" in page
    assert "coding/docker/local" in page
    assert "~/.ssh" in page and "127.0.0.1" in page
    assert 'name="acknowledge"' in page
