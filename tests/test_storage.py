from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

import pytest

from app.models import Project, RoundRecord, SessionConfig, SourceDescriptor
from app.storage import (
    ConflictError,
    LockCoordinator,
    OwnershipError,
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    ProjectStore,
    RegistryStore,
    StorageError,
    atomic_write_json,
    load_json_recover,
    safe_copy_file,
    sanitize_name,
)


def make_session(session_id: str = "a" * 32) -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name="researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="Be rigorous.",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )


def test_registry_crud_manifest_import_and_resolved_path_uniqueness(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(home)

    project = registry.register("Alpha", project_dir)
    assert project.path == str(project_dir.resolve())
    assert registry.get(project.id) == project
    assert stat.S_IMODE(home.stat().st_mode) == 0o700

    manifest = json.loads((project_dir / ".delibra" / "manifest.json").read_text())
    assert manifest["format"] == "delibra/1"
    assert manifest["id"] == project.id

    registry.rename(project.id, "Renamed")
    assert registry.get(project.id).name == "Renamed"
    with pytest.raises(ConflictError):
        registry.register("Duplicate", project_dir / ".")

    registry.unregister(project.id)
    assert not registry.list_projects()
    assert (project_dir / ".delibra").is_dir()

    imported = registry.register("Imported", project_dir)
    assert imported.id == project.id


def test_invalid_existing_manifest_is_rejected(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    metadata = project_dir / ".delibra"
    metadata.mkdir(parents=True)
    (metadata / "manifest.json").write_text('{"format":"other"}', encoding="utf-8")

    with pytest.raises(OwnershipError):
        RegistryStore(tmp_path / "home").register("Bad", project_dir)


def test_shared_markdown_selection_persists_and_rejects_reserved_roots(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    (project_dir / "docs").mkdir(parents=True)
    brief = project_dir / "docs" / "brief.MD"
    brief.write_text("Project rules\n", encoding="utf-8")
    registry = RegistryStore(tmp_path / "home")
    project = registry.register("Alpha", project_dir)
    store = ProjectStore(project)

    selected = store.select_shared_markdown("docs/brief.MD", 512 * 1024)

    assert selected.relative_path == "docs/brief.MD"
    assert selected.text == "Project rules\n"
    assert selected.sha256 == sha256(b"Project rules\n").hexdigest()
    assert store.selected_shared_markdown_path() == "docs/brief.MD"
    registry.unregister(project.id)
    imported = registry.register("Imported", project_dir)
    assert ProjectStore(imported).selected_shared_markdown_path() == "docs/brief.MD"

    for reserved in (".delibra/x.md", ".GIT/x.md", ".hg/x.md", ".svn/x.md"):
        with pytest.raises(ProjectFileSecurityError):
            ProjectStore(imported).select_shared_markdown(reserved, 512 * 1024)


def test_shared_markdown_read_is_strict_bounded_and_clearable(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    source = project_dir / "brief.md"
    source.write_text("123456789", encoding="utf-8")

    with pytest.raises(ProjectFileDisplayError, match="limit"):
        store.select_shared_markdown("brief.md", 8)
    source.write_bytes(b"invalid-\xff")
    with pytest.raises(ProjectFileDisplayError, match="UTF-8"):
        store.select_shared_markdown("brief.md", 64)
    source.write_bytes(b"nul\x00byte")
    with pytest.raises(ProjectFileDisplayError, match="NUL"):
        store.select_shared_markdown("brief.md", 64)

    source.write_bytes(b"")
    assert store.select_shared_markdown("brief.md", 64).text == ""
    store.clear_shared_markdown()
    assert store.selected_shared_markdown_path() is None
    assert store.read_selected_shared_markdown(64) is None


def test_shared_markdown_save_rejects_stale_or_symlinked_target(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    source = project_dir / "brief.md"
    source.write_text("version one", encoding="utf-8")
    source.chmod(0o666)
    selected = store.select_shared_markdown("brief.md", 1024)

    source.write_text("external version", encoding="utf-8")
    with pytest.raises(ConflictError, match="changed"):
        store.save_shared_markdown("brief.md", selected.sha256, "Delibra edit", 1024)
    assert source.read_text() == "external version"

    current = store.read_selected_shared_markdown(1024)
    assert current is not None
    previous_umask = os.umask(0o077)
    try:
        saved = store.save_shared_markdown(
            "brief.md",
            current.sha256,
            "saved",
            1024,
        )
    finally:
        os.umask(previous_umask)
    assert saved.text == "saved"
    assert stat.S_IMODE(source.stat().st_mode) == 0o666

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ProjectFileSecurityError):
        store.save_shared_markdown("brief.md", saved.sha256, "escape", 1024)
    assert outside.read_text() == "outside"


def test_shared_markdown_save_requires_current_selected_path(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    for name in ("one.md", "two.md"):
        (project_dir / name).write_text(name, encoding="utf-8")
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    first = store.select_shared_markdown("one.md", 1024)
    store.select_shared_markdown("two.md", 1024)

    with pytest.raises(ConflictError, match="selection changed"):
        store.save_shared_markdown("one.md", first.sha256, "stale", 1024)
    assert (project_dir / "one.md").read_text() == "one.md"


def test_shared_markdown_save_rejects_symlinked_parent(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    docs = project_dir / "docs"
    docs.mkdir(parents=True)
    source = docs / "brief.md"
    source.write_text("inside", encoding="utf-8")
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    selected = store.select_shared_markdown("docs/brief.md", 1024)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "brief.md").write_text("outside", encoding="utf-8")
    docs.rename(project_dir / "original-docs")
    docs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectFileSecurityError):
        store.save_shared_markdown(
            "docs/brief.md",
            selected.sha256,
            "escape",
            1024,
        )

    assert (outside / "brief.md").read_text(encoding="utf-8") == "outside"


def test_project_store_round_trip_allocation_exact_scans_and_orphans(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    session = make_session()

    store.create_session(session)
    assert store.load_session(session.id) == session
    assert store.allocate_round(session.id) == 1

    rounds = store.rounds_dir(session.id)
    for number in (1, 2, 99, 100):
        (rounds / f"round-{number:02d}.md").write_text(str(number), encoding="utf-8")
    (rounds / "round-03.prompt.md").write_text("prompt", encoding="utf-8")
    (rounds / "round-04.partial.md").write_text("partial", encoding="utf-8")
    (rounds / "round-5.md").write_text("wrong width", encoding="utf-8")
    (rounds / "round-06.md.tmp").write_text("tmp", encoding="utf-8")

    scanned = store.scan_round_files(session.id)
    assert [item.n for item in scanned.outputs] == [1, 2, 99, 100]
    assert [item.n for item in scanned.prompts] == [3]
    assert [item.n for item in scanned.partials] == [4]
    assert store.allocate_round(session.id) == 101
    assert {item.n for item in scanned.orphans} == {1, 2, 3, 4, 99, 100}


def test_config_backup_rotation_recovery_and_corrupt_current_preserves_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    atomic_write_json(path, {"version": 1})
    atomic_write_json(path, {"version": 2})
    assert json.loads(path.read_text()) == {"version": 2}
    assert json.loads(path.with_name("config.json.bak").read_text()) == {"version": 1}

    path.write_text("not json", encoding="utf-8")
    assert load_json_recover(path) == {"version": 1}
    atomic_write_json(path, {"version": 3})
    assert load_json_recover(path) == {"version": 3}
    assert json.loads(path.with_name("config.json.bak").read_text()) == {"version": 1}

    path.write_text("bad current", encoding="utf-8")
    path.with_name("config.json.bak").write_text("bad backup", encoding="utf-8")
    with pytest.raises(StorageError):
        load_json_recover(path)


def test_containment_symlink_rejection_safe_copy_hash_and_exclusive_destination(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    destination_root = tmp_path / "destination"
    allowed.mkdir()
    destination_root.mkdir()
    source = allowed / "source.md"
    source.write_bytes(b"trusted bytes\n")
    destination = destination_root / "copy.md"

    digest = safe_copy_file(source, allowed, destination, destination_root)
    assert destination.read_bytes() == b"trusted bytes\n"
    assert digest == "aab1d48d935f08af8cf75ba624a976de862f162e0aa2ec66dbe56b7d96bf61bf"
    with pytest.raises(ConflictError):
        safe_copy_file(source, allowed, destination, destination_root)

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    symlink = allowed / "link.md"
    symlink.symlink_to(outside)
    with pytest.raises(OwnershipError):
        safe_copy_file(symlink, allowed, destination_root / "link-copy.md", destination_root)

    linked_dir = destination_root / "linked"
    linked_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OwnershipError):
        safe_copy_file(source, allowed, linked_dir / "escape.md", destination_root)


def test_safe_copy_fails_cleanly_when_source_vanishes(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    destination_root = tmp_path / "destination"
    allowed.mkdir()
    destination_root.mkdir()
    source = allowed / "source.md"
    source.write_text("contents", encoding="utf-8")
    source.unlink()
    destination = destination_root / "copy.md"

    with pytest.raises(StorageError):
        safe_copy_file(source, allowed, destination, destination_root)
    assert not destination.exists()


def test_safe_copy_removes_destination_when_source_is_replaced_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    destination_root = tmp_path / "destination"
    allowed.mkdir()
    destination_root.mkdir()
    source = allowed / "source.md"
    source.write_bytes(b"original bytes")
    destination = destination_root / "copy.md"
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        block = original_read(descriptor, size)
        if block and not replaced:
            replaced = True
            source.rename(allowed / "original-unlinked.md")
            source.write_bytes(b"replacement bytes")
        return block

    monkeypatch.setattr(os, "read", replacing_read)
    with pytest.raises(StorageError, match="replaced"):
        safe_copy_file(source, allowed, destination, destination_root)
    assert not destination.exists()


def test_reconcile_interrupted_round_promotes_partial_and_session_remains_runnable(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    session = make_session()
    session.status = "running"
    session.rounds.append(
        RoundRecord(
            n=1,
            status="running",
            error=None,
            warnings=[],
            agent="claude",
            model="sonnet",
            effort="high",
            started_at="2026-07-17T00:01:00Z",
            finished_at=None,
            source=SourceDescriptor(type="user"),
        )
    )
    store.create_session(session)
    rounds = store.rounds_dir(session.id)
    (rounds / "round-01.prompt.md").write_text("question", encoding="utf-8")
    (rounds / "round-01.partial.md").write_text("partial answer", encoding="utf-8")

    reconciled = store.reconcile_session(session.id)
    assert reconciled.status == "error"
    assert reconciled.rounds[0].status == "error"
    assert "interrupted by restart" in (reconciled.rounds[0].error or "")
    assert (rounds / "round-01.md").read_text() == "partial answer"
    assert not (rounds / "round-01.partial.md").exists()


def test_reconcile_keeps_existing_final_output_and_discards_stale_partial(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    session = make_session()
    session.status = "running"
    session.rounds.append(
        RoundRecord(
            n=1,
            status="running",
            error=None,
            warnings=[],
            agent="claude",
            model="sonnet",
            effort="high",
            started_at="2026-07-17T00:01:00Z",
            finished_at=None,
            source=SourceDescriptor(type="user"),
        )
    )
    store.create_session(session)
    rounds = store.rounds_dir(session.id)
    (rounds / "round-01.md").write_text("authoritative final", encoding="utf-8")
    partial = rounds / "round-01.partial.md"
    partial.write_text("stale partial", encoding="utf-8")

    reconciled = store.reconcile_session(session.id)

    assert reconciled.rounds[0].status == "error"
    assert reconciled.rounds[0].error == "interrupted by restart"
    assert (rounds / "round-01.md").read_text() == "authoritative final"
    assert not partial.exists()


def test_delete_requires_matching_owned_identity(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    session = make_session()
    store.create_session(session)
    config_path = store.session_dir(session.id) / "config.json"
    data = json.loads(config_path.read_text())
    data["id"] = "b" * 32
    atomic_write_json(config_path, data)

    with pytest.raises(OwnershipError):
        store.delete_session(session.id)
    assert store.session_dir(session.id).exists()


def test_delete_refuses_symlinked_session_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    session = make_session()
    store.create_session(session)
    original = store.sessions_root / "original-session"
    store.session_dir(session.id).rename(original)
    store.session_dir(session.id).symlink_to(original, target_is_directory=True)

    with pytest.raises(OwnershipError, match="symlink"):
        store.delete_session(session.id)
    assert original.is_dir()


def test_project_store_rejects_symlinked_sessions_root(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    store = ProjectStore(project)
    store.sessions_root.rmdir()
    outside = tmp_path / "outside-sessions"
    outside.mkdir()
    store.sessions_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OwnershipError, match="symlink"):
        ProjectStore(project)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("bad", ["line\nbreak", "control\x00char", "\t"])
def test_name_sanitization_rejects_controls(bad: str) -> None:
    with pytest.raises(ValueError):
        sanitize_name(bad)


def test_name_sanitization_trims_and_never_drives_paths() -> None:
    assert sanitize_name("  Research / Critic  ") == "Research / Critic"


@pytest.mark.asyncio
async def test_lock_coordinator_order_is_stable_and_mixed_contention_has_no_deadlock() -> None:
    locks = LockCoordinator()
    project_id = "p" * 32
    session_a = "a" * 32
    session_b = "b" * 32
    order: list[str] = []

    async def registry_project(label: str) -> None:
        async with locks.registry_project_sessions(project_id, [session_b, session_a]):
            order.append(label)
            await asyncio.sleep(0)

    async def project_session(label: str) -> None:
        async with locks.project_sessions(project_id, [session_a]):
            order.append(label)
            await asyncio.sleep(0)

    async def sessions_only(label: str) -> None:
        async with locks.sessions(project_id, [session_b]):
            order.append(label)
            await asyncio.sleep(0)

    await asyncio.wait_for(
        asyncio.gather(
            *(registry_project(f"r{i}") for i in range(10)),
            *(project_session(f"p{i}") for i in range(10)),
            *(sessions_only(f"s{i}") for i in range(10)),
        ),
        timeout=2,
    )
    assert len(order) == 30
    assert locks.project_lock(project_id) is locks.project_lock(project_id)
    assert locks.session_lock(project_id, session_a) is locks.session_lock(
        project_id, session_a
    )
