from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.pass_prompts import BUILT_IN_PASS_PROMPT_TEMPLATE
from app.storage import (
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


def project_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    return create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    ), settings


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
