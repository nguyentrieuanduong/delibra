from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import shutil
import stat
import unicodedata

import pytest

from app.models import (
    ContextObservation,
    ContextSummaryArtifact,
    Project,
    RoundRecord,
    SessionConfig,
    SharedContextDescriptor,
    SourceDescriptor,
    TurnUsage,
    total_turn_usage,
)
from app.pass_prompts import BUILT_IN_PASS_PROMPT_TEMPLATE
from app.storage import (
    ConflictError,
    LockCoordinator,
    OwnershipError,
    ProjectFileDisplayError,
    ProjectFileSecurityError,
    ProjectStore,
    RegistryStore,
    StorageError,
    _agent_directory_name_matches,
    atomic_write_bytes,
    atomic_write_json,
    load_json_recover,
    normalize_agent_name,
    normalize_project_name,
    safe_copy_file,
    sanitize_name,
)


def session_config(session_id: str, name: str) -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=name,
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-29T00:00:00Z",
        rounds=[],
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


@pytest.mark.parametrize(
    "bad",
    [
        ".hidden",
        "trailing.",
        "a/b",
        "a\\b",
        "CON",
        "a" * 32,
        "line\u2028break",
        "bidi\u202ename",
        "control\u0085name",
    ],
)
def test_project_name_rejects_unsafe_url_components(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_project_name(bad)


def test_project_names_are_nfc_casefold_unique(tmp_path: Path) -> None:
    registry = RegistryStore(tmp_path / "home")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry.register("Café", first)
    with pytest.raises(ConflictError, match="project name is already in use"):
        registry.register("CAFE\u0301", second)


def test_project_name_generated_inputs_only_return_safe_components() -> None:
    generator = random.Random(20260731)
    alphabet = (
        "ABCxyz09 .-_/"
        "\\\x00\x1f\x7f\x85"
        "\u0301\u2028\u2029\u202e"
        "é中"
    )
    for _ in range(500):
        value = "".join(
            generator.choice(alphabet)
            for _ in range(generator.randrange(0, 230))
        )
        try:
            normalized = normalize_project_name(value)
        except ValueError:
            continue
        assert normalized == unicodedata.normalize("NFC", normalized)
        assert normalized == normalized.strip()
        assert normalized not in {".", ".."}
        assert not normalized.startswith(".")
        assert not normalized.endswith(".")
        assert "/" not in normalized
        assert "\\" not in normalized
        assert len(normalized.encode("utf-8")) <= 200
        assert not any(
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or character in {"\u2028", "\u2029"}
            or unicodedata.category(character) == "Cf"
            for character in normalized
        )


def test_legacy_registry_names_migrate_once_in_created_id_order(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = home / "registry.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "b" * 32,
                        "name": "bad/name",
                        "path": str(tmp_path / "b"),
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                    {
                        "id": "a" * 32,
                        "name": "bad\\name",
                        "path": str(tmp_path / "a"),
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = RegistryStore(home)
    assert [(item.id, item.name) for item in registry.list_projects()] == [
        ("b" * 32, "bad-name-2"),
        ("a" * 32, "bad-name"),
    ]
    before = path.read_bytes()
    assert registry.migrate_project_names() is False
    assert path.read_bytes() == before


def test_reregister_prefers_manifest_name_and_suffixes_collision(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    original_path = tmp_path / "original"
    claimant_path = tmp_path / "claimant"
    original_path.mkdir()
    claimant_path.mkdir()
    original = registry.register("Research", original_path)
    registry.unregister(original.id)
    registry.register("Research", claimant_path)

    restored = registry.register("Ignored form value", original_path)

    assert restored.id == original.id
    assert restored.name == "Research-2"
    manifest = json.loads(
        (original_path / ".delibra/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "Research-2"


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
    assert manifest["name"] == "Alpha"

    with pytest.raises(ConflictError):
        registry.register("Duplicate", project_dir / ".")

    registry.unregister(project.id)
    assert not registry.list_projects()
    assert (project_dir / ".delibra").is_dir()

    imported = registry.register("Imported", project_dir)
    assert imported.id == project.id
    assert imported.name == "Alpha"


def test_invalid_existing_manifest_is_rejected(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    metadata = project_dir / ".delibra"
    metadata.mkdir(parents=True)
    (metadata / "manifest.json").write_text('{"format":"other"}', encoding="utf-8")

    with pytest.raises(OwnershipError):
        RegistryStore(tmp_path / "home").register("Bad", project_dir)


def test_round_record_shared_context_and_retry_fields_are_backward_compatible() -> None:
    legacy_data = {
        "n": 1,
        "status": "error",
        "error": "failed",
        "warnings": [],
        "agent": "codex",
        "model": "gpt-5.4",
        "effort": "high",
        "started_at": "2026-07-19T00:00:00Z",
        "finished_at": "2026-07-19T00:00:01Z",
        "source": {"type": "user"},
    }

    legacy = RoundRecord.from_dict(legacy_data)
    assert legacy.shared_context is None
    assert legacy.retry_of is None
    assert "shared_context" not in legacy.to_dict()
    assert "retry_of" not in legacy.to_dict()

    legacy.shared_context = SharedContextDescriptor(
        path="brief.md",
        staged_file="inputs/round-01/shared-context.md",
        sha256="a" * 64,
    )
    legacy.retry_of = 1
    restored = RoundRecord.from_dict(legacy.to_dict())
    assert restored.shared_context == legacy.shared_context
    assert restored.retry_of == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_tokens": True},
        {"input_tokens": -1},
        {"output_tokens": 1.0},
        {"cache_read_tokens": -5},
        {"max_output_tokens": False},
        {"total_cost_usd": float("nan")},
        {"total_cost_usd": float("inf")},
        {"total_cost_usd": -0.5},
        {"total_cost_usd": True},
    ],
)
def test_turn_usage_rejects_booleans_negatives_and_non_finite_costs(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        TurnUsage(**kwargs)


def test_turn_usage_leaves_unreported_fields_none_rather_than_zero() -> None:
    usage = TurnUsage(input_tokens=12, output_tokens=3)

    assert usage.total_cost_usd is None
    assert usage.to_dict() == {"input_tokens": 12, "output_tokens": 3}
    assert TurnUsage.from_dict(usage.to_dict()) == usage
    assert not TurnUsage().reported


def test_round_record_usage_is_backward_compatible() -> None:
    legacy_data = {
        "n": 1,
        "status": "complete",
        "error": None,
        "warnings": [],
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "started_at": "2026-09-07T00:00:00Z",
        "finished_at": "2026-09-07T00:00:01Z",
        "source": {"type": "user"},
    }

    legacy = RoundRecord.from_dict(legacy_data)
    assert legacy.usage is None
    assert "usage" not in legacy.to_dict()

    legacy.usage = TurnUsage(
        input_tokens=32018,
        output_tokens=5,
        cache_read_tokens=31872,
        cache_creation_tokens=0,
        reasoning_tokens=0,
        total_cost_usd=0.0130533,
        max_output_tokens=64000,
    )
    assert RoundRecord.from_dict(legacy.to_dict()).usage == legacy.usage


def test_session_config_context_observation_is_backward_compatible() -> None:
    legacy_data = {
        "id": "b" * 32,
        "name": "researcher",
        "agent": "claude",
        "model": "sonnet",
        "effort": "high",
        "role_instructions": "",
        "cli_session_id": None,
        "status": "idle",
        "created_at": "2026-09-07T00:00:00Z",
    }

    legacy = SessionConfig.from_dict(legacy_data)
    assert legacy.context_observation is None

    legacy.context_observation = ContextObservation(
        used_tokens=42970,
        context_window=1_000_000,
        numerator_source="claude_final_assistant",
        resolved_model="claude-sonnet-5",
        round_n=3,
        observed_at="2026-09-07T00:00:02Z",
    )
    restored = SessionConfig.from_dict(legacy.to_dict())
    assert restored.context_observation == legacy.context_observation


def test_session_config_context_boundary_is_backward_compatible() -> None:
    legacy_data = {
        "id": "b" * 32,
        "name": "researcher",
        "agent": "claude",
        "model": "sonnet",
        "effort": "high",
        "role_instructions": "",
        "cli_session_id": None,
        "status": "idle",
        "created_at": "2026-09-07T00:00:00Z",
    }

    legacy = SessionConfig.from_dict(legacy_data)
    assert legacy.context_baseline_round == 0
    assert legacy.context_summary is None

    legacy.context_baseline_round = 4
    legacy.context_summary = ContextSummaryArtifact(
        path="context/summary-04.md",
        sha256="a" * 64,
        source_round=4,
        created_at="2026-09-07T00:00:02Z",
        model="claude-sonnet-5",
    )
    restored = SessionConfig.from_dict(legacy.to_dict())
    assert restored.context_baseline_round == 4
    assert restored.context_summary == legacy.context_summary


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../other/summary.md",
        "context/../../escape.md",
        "",
    ],
)
def test_context_summary_refuses_a_path_it_does_not_own(path: str) -> None:
    # The summary is read back and fed to a provider as context, so a path that
    # leaves the session's own directory must fail on construction -- which is
    # also what refuses it on load.
    with pytest.raises(ValueError):
        ContextSummaryArtifact(
            path=path,
            sha256="a" * 64,
            source_round=1,
            created_at="2026-09-07T00:00:02Z",
            model=None,
        )


def test_context_summary_refuses_a_malformed_digest_or_round() -> None:
    with pytest.raises(ValueError):
        ContextSummaryArtifact(
            path="context/summary-01.md",
            sha256="not-a-digest",
            source_round=1,
            created_at="2026-09-07T00:00:02Z",
            model=None,
        )
    with pytest.raises(ValueError):
        ContextSummaryArtifact(
            path="context/summary-01.md",
            sha256="a" * 64,
            source_round=0,
            created_at="2026-09-07T00:00:02Z",
            model=None,
        )


def test_context_baseline_round_refuses_a_negative_boundary() -> None:
    with pytest.raises(ValueError):
        SessionConfig.from_dict(
            {
                "id": "b" * 32,
                "name": "researcher",
                "agent": "claude",
                "model": "sonnet",
                "effort": "high",
                "role_instructions": "",
                "cli_session_id": None,
                "status": "idle",
                "created_at": "2026-09-07T00:00:00Z",
                "context_baseline_round": -1,
            }
        )


def test_totals_never_claim_a_figure_no_round_reported() -> None:
    total = total_turn_usage(
        [
            None,
            TurnUsage(input_tokens=10, total_cost_usd=0.25, max_output_tokens=64000),
            TurnUsage(input_tokens=5, output_tokens=2),
        ]
    )

    assert total.input_tokens == 15
    assert total.output_tokens == 2
    assert total.total_cost_usd == pytest.approx(0.25)
    # Nothing reported these, so they stay unknown rather than becoming 0.
    assert total.cache_read_tokens is None
    assert total.reasoning_tokens is None
    # A per-turn cap is not a quantity, so it is never summed.
    assert total.max_output_tokens is None
    assert not total_turn_usage([None, TurnUsage()]).reported


def test_context_observation_rejects_an_unattributable_numerator() -> None:
    with pytest.raises(ValueError):
        ContextObservation(
            used_tokens=1,
            context_window=2,
            numerator_source="claude_result",
            resolved_model="claude-sonnet-5",
            round_n=1,
            observed_at="2026-09-07T00:00:00Z",
        )


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


def test_pass_prompt_template_is_backward_compatible_persistent_and_resettable(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(tmp_path / "home")
    project = registry.register("Alpha", project_dir)
    store = ProjectStore(project)
    manifest_path = project_dir / ".delibra" / "manifest.json"
    legacy_bytes = manifest_path.read_bytes()

    assert store.effective_pass_prompt_template() == BUILT_IN_PASS_PROMPT_TEMPLATE
    assert manifest_path.read_bytes() == legacy_bytes

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["shared_markdown_path"] = "brief.md"
    manifest["future_key"] = {"keep": True}
    atomic_write_json(manifest_path, manifest)
    custom = "Use {source_path}; session={source_session}; round={source_round}"
    assert store.set_pass_prompt_template(custom) == custom
    registry.unregister(project.id)
    imported = registry.register("Imported", project_dir)
    imported_store = ProjectStore(imported)
    assert imported_store.effective_pass_prompt_template() == custom

    imported_store.reset_pass_prompt_template()
    reset = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert imported_store.effective_pass_prompt_template() == BUILT_IN_PASS_PROMPT_TEMPLATE
    assert "pass_prompt_template" not in reset
    assert reset["shared_markdown_path"] == "brief.md"
    assert reset["future_key"] == {"keep": True}


@pytest.mark.parametrize("invalid", [None, 42, "missing token"])
def test_project_store_rejects_invalid_manifest_pass_prompt_template(
    tmp_path: Path,
    invalid: object,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Alpha", project_dir)
    manifest_path = project_dir / ".delibra" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pass_prompt_template"] = invalid
    atomic_write_json(manifest_path, manifest)
    with pytest.raises(OwnershipError, match="Pass prompt template"):
        ProjectStore(project)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (".hidden", "must not be hidden"),
        ("../escape", "one directory component"),
        ("nested/name", "one directory component"),
        ("nested\\name", "one directory component"),
        ("Researcher.", "must not end with a dot"),
        ("CON.txt", "reserved filesystem name"),
        ("a" * 32, "must not look like a session UUID"),
        ("A" * 32, "must not look like a session UUID"),
        ("x" * 201, "at most 200 UTF-8 bytes"),
    ],
)
def test_agent_directory_name_validation_rejects_unsafe_names(
    name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_agent_name(name)


def test_agent_name_is_normalized_and_used_as_the_session_directory(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Named sessions", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "  Résearcher  ")

    store.create_session(config)

    assert config.name == "Résearcher"
    assert store.session_dir(config.id) == store.sessions_root / "Résearcher"
    assert store.load_session(config.id).id == config.id
    assert not (store.sessions_root / config.id).exists()


def test_agent_names_are_unique_after_unicode_and_case_normalization(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Unique sessions", project_path)
    store = ProjectStore(project)
    name = "R\u00e9searcher"
    store.create_session(session_config("a" * 32, name))

    with pytest.raises(ConflictError, match="agent name is already in use"):
        store.create_session(session_config("b" * 32, "RÉSEARCHER"))

    assert [child.name for child in store.sessions_root.iterdir()] == [name]


def test_session_scan_ignores_dotfiles_and_non_directory_artifacts(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Artifact tolerance", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    store.create_session(config)
    (store.sessions_root / ".DS_Store").write_bytes(b"finder")
    (store.sessions_root / "README.txt").write_text("notes", encoding="utf-8")
    (store.sessions_root / ".ignored").mkdir()
    reopened = ProjectStore(project)

    assert [item.id for item in reopened.list_sessions()] == [config.id]


def test_non_ascii_agent_directory_uses_canonical_name_equivalence(
    tmp_path: Path,
) -> None:
    composed = "Résearcher"
    decomposed = unicodedata.normalize("NFD", composed)
    assert _agent_directory_name_matches(decomposed, composed)

    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Unicode restart", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, composed)
    store.create_session(config)

    reopened = ProjectStore(project)

    assert reopened.load_session(config.id).name == composed


def test_structural_session_cache_does_not_return_stale_configs(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Fresh configs", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    store.create_session(config)
    assert store.session_dir(config.id).name == "Researcher"
    assert store._session_directory_cache is not None
    assert all(
        not hasattr(location, "config")
        for location in store._session_directory_cache.values()
    )

    writer = ProjectStore(project)
    changed = writer.load_session(config.id)
    changed.model = "opus"
    writer.save_session(changed)

    assert store.load_session(config.id).model == "opus"
    assert store.list_sessions()[0].model == "opus"


def create_legacy_session(store: ProjectStore, config: SessionConfig) -> Path:
    destination = store.sessions_root / config.id
    destination.mkdir(mode=0o700)
    (destination / "rounds").mkdir(mode=0o700)
    workspace = destination / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / ".tmp").mkdir(mode=0o700)
    (workspace / "inputs").mkdir(mode=0o700)
    atomic_write_json(destination / "config.json", config.to_dict())
    store._invalidate_session_directory_cache()
    return destination


def test_legacy_session_directories_migrate_and_keep_uuid_lookup(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Migration", project_path)
    store = ProjectStore(project)
    alpha = session_config("a" * 32, "Alpha")
    beta = session_config("b" * 32, "Beta")
    alpha.cli_session_id = "legacy-alpha-resume"
    beta.cli_session_id = "legacy-beta-resume"
    create_legacy_session(store, alpha)
    create_legacy_session(store, beta)

    result = store.migrate_session_directories()

    assert result.issues == ()
    assert result.legacy_session_ids == ()
    assert store.session_dir(alpha.id) == store.sessions_root / "Alpha"
    assert store.session_dir(beta.id) == store.sessions_root / "Beta"
    assert store.load_session(alpha.id).id == alpha.id
    assert store.load_session(alpha.id).cli_session_id is None
    assert store.load_session(beta.id).cli_session_id is None


def test_migration_resumes_a_mixed_layout_after_interruption(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Resume migration", project_path)
    store = ProjectStore(project)
    alpha = session_config("a" * 32, "Alpha")
    beta = session_config("b" * 32, "Beta")
    first = create_legacy_session(store, alpha)
    create_legacy_session(store, beta)
    first.rename(store.sessions_root / "Alpha")
    store._invalidate_session_directory_cache()

    result = store.migrate_session_directories()

    assert result.complete
    assert store.session_dir(alpha.id).name == "Alpha"
    assert store.session_dir(beta.id).name == "Beta"


def test_invalid_or_duplicate_legacy_names_block_renames_but_stay_loadable(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Blocked migration", project_path)
    store = ProjectStore(project)
    alpha = session_config("a" * 32, "bad/name")
    beta = session_config("b" * 32, "bad\\name")
    beta.cli_session_id = "legacy-beta-resume"
    create_legacy_session(store, alpha)
    create_legacy_session(store, beta)

    blocked = store.migrate_session_directories()

    assert {issue.session_id for issue in blocked.issues} == {alpha.id, beta.id}
    assert store.load_session(alpha.id).name == "bad/name"
    assert store.load_session(beta.id).name == "bad\\name"
    assert (store.sessions_root / alpha.id).is_dir()
    assert (store.sessions_root / beta.id).is_dir()

    resolved = store.set_legacy_session_name(beta.id, "Beta")

    assert not resolved.complete
    assert resolved.legacy_session_ids == (alpha.id,)
    assert {issue.session_id for issue in resolved.issues} == {alpha.id}
    assert store.session_dir(alpha.id).name == alpha.id
    assert store.session_dir(beta.id).name == "Beta"
    assert store.load_session(beta.id).name == "Beta"
    assert store.load_session(beta.id).cli_session_id is None

    completed = store.set_legacy_session_name(alpha.id, "Alpha")

    assert completed.complete
    assert store.session_dir(alpha.id).name == "Alpha"


def test_registry_rebinds_the_same_project_identity_at_a_new_path(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    original = tmp_path / "original"
    original.mkdir()
    project = registry.register("Movable", original)
    moved = tmp_path / "moved"
    original.rename(moved)

    preview = registry.preview_rebind(project.id, moved)
    rebound = registry.rebind(project.id, moved)

    assert preview.path == str(moved.resolve())
    assert rebound.id == project.id
    assert rebound.name == project.name
    assert rebound.created_at == project.created_at
    assert registry.get(project.id).path == str(moved.resolve())


def test_rebind_rejects_wrong_identity_duplicate_and_symlinked_metadata(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = registry.register("First", first_path)
    second = registry.register("Second", second_path)
    before = registry.get(first.id)

    wrong = tmp_path / "wrong"
    shutil.copytree(second_path, wrong)
    with pytest.raises(ConflictError, match="different project identity"):
        registry.rebind(first.id, wrong)
    with pytest.raises(ConflictError, match="already registered"):
        registry.rebind(first.id, second_path)

    copied = tmp_path / "copied"
    shutil.copytree(first_path, copied)
    metadata = copied / ".delibra"
    real_metadata = copied / "real-metadata"
    metadata.rename(real_metadata)
    metadata.symlink_to(real_metadata, target_is_directory=True)
    with pytest.raises(OwnershipError, match="not an owned directory"):
        registry.rebind(first.id, copied)

    assert registry.get(first.id) == before
    assert registry.get(second.id).path == str(second_path.resolve())


def test_rebind_keeps_relative_round_artifacts_readable(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    original = tmp_path / "original"
    original.mkdir()
    project = registry.register("Portable metadata", original)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    store.create_session(config)
    config = store.load_session(config.id)
    config.rounds.append(
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
            source=SourceDescriptor(
                type="pass",
                from_session="b" * 32,
                from_round=1,
                staged_file="inputs/round-01/source.md",
                source_sha256="f" * 64,
            ),
        )
    )
    store.save_session(config)
    rounds = store.rounds_dir(config.id)
    (rounds / "round-01.prompt.md").write_text(
        "Portable prompt",
        encoding="utf-8",
    )
    (rounds / "round-01.md").write_text(
        "Portable output",
        encoding="utf-8",
    )
    moved = tmp_path / "moved"
    original.rename(moved)

    rebound = registry.rebind(project.id, moved)
    rebound_store = ProjectStore(rebound)
    loaded = rebound_store.load_session(config.id)
    staged = loaded.rounds[0].source.staged_file

    assert staged == "inputs/round-01/source.md"
    assert not Path(staged).is_absolute()
    assert rebound_store.load_round_artifact(
        config.id,
        1,
        "prompt",
        1_024,
    ) == b"Portable prompt"
    assert rebound_store.load_round_artifact(
        config.id,
        1,
        "output",
        1_024,
    ) == b"Portable output"


def test_registry_rebind_rejects_the_current_canonical_path(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Already bound", project_path)
    before = registry.get(project.id)

    with pytest.raises(ConflictError, match="already bound to this path"):
        registry.preview_rebind(project.id, project_path / ".")

    assert registry.get(project.id) == before


def test_relocation_clear_cannot_recover_a_stale_native_id(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Relocation recovery", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    config.cli_session_id = "stale-native"
    store.create_session(config)

    assert store.clear_native_session_ids_for_relocation() == 1

    config_path = store.session_dir(config.id) / "config.json"
    backup = json.loads(
        config_path.with_name("config.json.bak").read_text(encoding="utf-8")
    )
    assert backup["cli_session_id"] is None
    config_path.write_text("{", encoding="utf-8")
    recovered = ProjectStore(project).load_session(config.id)
    assert recovered.cli_session_id is None


def test_legacy_migration_cannot_recover_a_stale_native_id(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Migration recovery", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    config.cli_session_id = "stale-native"
    create_legacy_session(store, config)

    assert store.migrate_session_directories().complete

    config_path = store.session_dir(config.id) / "config.json"
    config_path.write_text("{", encoding="utf-8")
    recovered = ProjectStore(project).load_session(config.id)
    assert recovered.name == "Researcher"
    assert recovered.cli_session_id is None


def test_permanent_name_backup_keeps_the_whole_project_loadable(
    tmp_path: Path,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Permanent-name recovery", project_path)
    store = ProjectStore(project)
    healthy = session_config("b" * 32, "Healthy")
    store.create_session(healthy)
    legacy = session_config("a" * 32, "bad/name")
    legacy.cli_session_id = "stale-native"
    create_legacy_session(store, legacy)

    store.set_legacy_session_name(legacy.id, "Archivist")

    config_path = store.session_dir(legacy.id) / "config.json"
    config_path.write_text("{", encoding="utf-8")
    reopened = ProjectStore(project)
    sessions = reopened.list_sessions()
    assert [session.name for session in sessions] == ["Archivist", "Healthy"]
    assert reopened.load_session(legacy.id).cli_session_id is None


def test_recovery_pair_replaces_backup_before_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Write order", project_path)
    store = ProjectStore(project)
    config = session_config("a" * 32, "Researcher")
    config.cli_session_id = "stale-native"
    store.create_session(config)
    observed: list[str] = []
    real_atomic_write_bytes = atomic_write_bytes

    def recording_write(path: Path, contents: bytes, *, mode: int = 0o600) -> None:
        if path.name in {"config.json", "config.json.bak"}:
            observed.append(path.name)
        real_atomic_write_bytes(path, contents, mode=mode)

    monkeypatch.setattr("app.storage.atomic_write_bytes", recording_write)

    store.clear_native_session_ids_for_relocation()

    assert observed[-2:] == ["config.json.bak", "config.json"]
