from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import random

import pytest

import app.models as models
import app.storage as storage
from app.storage import (
    ConflictError,
    OwnershipError,
    ProjectStore,
    RegistryStore,
    StorageError,
)


def legacy_round_record() -> dict[str, object]:
    return {
        "n": 1,
        "status": "complete",
        "error": None,
        "warnings": [],
        "agent": "fake",
        "model": "success",
        "effort": "low",
        "started_at": "2026-07-19T00:00:00Z",
        "finished_at": "2026-07-19T00:00:01Z",
        "source": {"type": "user"},
    }


def auto_project_store(tmp_path: Path) -> ProjectStore:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(tmp_path / "home").register("Auto", project_dir)
    return ProjectStore(project)


def auto_record_fixture(project_id: str) -> models.AutoRunRecord:
    topic = b"Original topic"
    baseline = b""
    return models.AutoRunRecord(
        id="a" * 32,
        project_id=project_id,
        number=None,
        status="preparing",
        agreement_policy="all_agree",
        preparation_enabled=True,
        max_cycles=3,
        current_cycle=0,
        next_participant=0,
        participants=[
            models.AutoParticipant(
                session_id="b" * 32,
                name="Alpha",
                agent="fake",
                model="success",
                effort="low",
            ),
            models.AutoParticipant(
                session_id="c" * 32,
                name="Beta",
                agent="fake",
                model="success",
                effort="low",
            ),
        ],
        topic=models.AutoArtifact("topic.md", sha256(topic).hexdigest()),
        baseline=models.AutoArtifact("baseline.md", sha256(baseline).hexdigest()),
        baseline_entries=[],
        shared_context=None,
        shared_context_source=None,
        preparations=[],
        discussion=[],
        active_key=None,
        future_turn_timeout_seconds=900,
        active_timeout=None,
        stop_requested=False,
        created_at="2026-07-19T00:00:00Z",
        started_at=None,
        finished_at=None,
        terminal_reason=None,
    )


def write_auto_run_directory(
    store: ProjectStore,
    record: models.AutoRunRecord,
    child: str,
) -> None:
    run_dir = store.auto_runs_root / child
    run_dir.mkdir(mode=0o700)
    (run_dir / "preparations").mkdir(mode=0o700)
    (run_dir / "topic.md").write_bytes(b"Original topic")
    (run_dir / "baseline.md").write_bytes(b"")
    encoded = record.to_dict()
    if record.number is None:
        encoded.pop("number")
    (run_dir / "config.json").write_text(
        json.dumps(encoded),
        encoding="utf-8",
    )


def create_legacy_auto_run(
    store: ProjectStore,
    *,
    auto_id: str,
    created_at: str = "2026-01-01T00:00:00Z",
) -> models.AutoRunRecord:
    record = auto_record_fixture(store.project.id)
    record.id = auto_id
    record.number = None
    record.created_at = created_at
    write_auto_run_directory(store, record, auto_id)
    return record


def create_numeric_auto_run(
    store: ProjectStore,
    *,
    number: int,
    auto_id: str,
    created_at: str = "2026-01-01T00:00:00Z",
) -> models.AutoRunRecord:
    record = auto_record_fixture(store.project.id)
    record.id = auto_id
    record.number = number
    record.created_at = created_at
    write_auto_run_directory(store, record, str(number))
    return record


def test_auto_scanner_aggregates_issues_and_keeps_unrelated_records(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    valid = create_legacy_auto_run(store, auto_id="a" * 32)
    malformed = store.auto_runs_root / ("b" * 32)
    malformed.mkdir()
    (malformed / "config.json").write_text("{", encoding="utf-8")

    status = store.auto_migration_status()

    assert status.complete is False
    assert [record.id for record in status.readable_records] == [valid.id]
    assert [(issue.child, issue.message) for issue in status.issues] == [
        ("b" * 32, "Auto run config is invalid")
    ]
    with pytest.raises(StorageError):
        store.list_auto_runs()


def test_auto_scanner_excludes_both_sides_of_global_ambiguity(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    create_numeric_auto_run(store, number=1, auto_id="a" * 32)
    create_numeric_auto_run(store, number=2, auto_id="a" * 32)

    status = store.auto_migration_status()

    assert status.readable_records == ()
    assert {issue.child for issue in status.issues} == {"1", "2"}
    assert all("UUID is stored more than once" in issue.message for issue in status.issues)


def test_auto_scanner_excludes_both_sides_of_duplicate_number(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    create_numeric_auto_run(store, number=1, auto_id="a" * 32)
    duplicate = auto_record_fixture(store.project.id)
    duplicate.id = "b" * 32
    duplicate.number = 1
    write_auto_run_directory(store, duplicate, duplicate.id)

    status = store.auto_migration_status()

    assert status.readable_records == ()
    assert {issue.child for issue in status.issues} == {"1", duplicate.id}
    assert all("number is stored more than once" in issue.message for issue in status.issues)


def test_auto_scanner_skips_dot_prefixed_entries(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    valid = create_legacy_auto_run(store, auto_id="a" * 32)
    (store.auto_runs_root / ".index.json").write_text("{", encoding="utf-8")
    hidden = store.auto_runs_root / ".creating-broken"
    hidden.mkdir()
    (hidden / "config.json").write_text("{", encoding="utf-8")

    status = store.auto_migration_status()

    assert status.issues == ()
    assert status.legacy_ids == (valid.id,)
    assert status.readable_records == (valid,)


@pytest.mark.parametrize(
    ("failure", "reason_terms"),
    [
        ("symlink", ("symlink",)),
        ("malformed_json_and_backup", ("config", "json")),
        ("wrong_project", ("project identity",)),
        ("invalid_artifact", ("artifact digest",)),
        ("numeric_mismatch", ("number", "numeric")),
    ],
)
def test_auto_scanner_matches_strict_validation_failure(
    tmp_path: Path,
    failure: str,
    reason_terms: tuple[str, ...],
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    child = record.id

    if failure == "symlink":
        write_auto_run_directory(store, record, child)
        outside = tmp_path / "outside-auto"
        os.replace(store.auto_runs_root / child, outside)
        (store.auto_runs_root / child).symlink_to(outside, target_is_directory=True)
    elif failure == "malformed_json_and_backup":
        run_dir = store.auto_runs_root / child
        run_dir.mkdir()
        (run_dir / "config.json").write_text("{", encoding="utf-8")
        (run_dir / "config.json.bak").write_text("[", encoding="utf-8")
    elif failure == "wrong_project":
        record.project_id = "f" * 32
        write_auto_run_directory(store, record, child)
    elif failure == "invalid_artifact":
        record.topic = models.AutoArtifact(path="topic.md", sha256="invalid")
        write_auto_run_directory(store, record, child)
    else:
        record.number = 2
        child = "1"
        write_auto_run_directory(store, record, child)

    with pytest.raises((OwnershipError, StorageError)) as strict_error:
        store.load_auto_run(record.id)
    status = store.auto_migration_status()

    assert status.readable_records == ()
    assert len(status.issues) == 1
    assert status.issues[0].child == child
    strict_reason = str(strict_error.value).casefold()
    scan_reason = status.issues[0].message.casefold()
    assert any(term in strict_reason for term in reason_terms)
    assert any(term in scan_reason for term in reason_terms)


def test_auto_uuid_fallback_scans_once_per_project_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = auto_project_store(tmp_path)
    record = create_numeric_auto_run(store, number=1, auto_id="a" * 32)
    store.auto_index_path.unlink(missing_ok=True)
    store.auto_index_path.with_name(".index.json.bak").unlink(missing_ok=True)
    scans = 0
    original = store._scan_auto_directories

    def counted_scan():
        nonlocal scans
        scans += 1
        return original()

    monkeypatch.setattr(store, "_scan_auto_directories", counted_scan)
    monkeypatch.setattr(
        store,
        "_publish_rebuilt_auto_index",
        lambda *, recovery_pair: (_ for _ in ()).throw(
            StorageError("index is read only")
        ),
    )

    assert store.load_auto_run(record.id).number == 1
    assert store.load_auto_run(record.id).number == 1
    assert scans == 1


def test_auto_migration_resumes_mixed_layout_in_created_order(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    later = create_legacy_auto_run(
        store,
        auto_id="b" * 32,
        created_at="2026-01-02T00:00:00Z",
    )
    earlier = create_legacy_auto_run(
        store,
        auto_id="a" * 32,
        created_at="2026-01-01T00:00:00Z",
    )

    first = store.migrate_auto_run_directories()
    second = store.migrate_auto_run_directories()

    assert first.complete is True
    assert second.complete is True
    assert store.load_auto_run(earlier.id).number == 1
    assert store.load_auto_run(later.id).number == 2
    assert [item.number for item in store.list_auto_runs()] == [1, 2]


def test_auto_migration_preserves_numbered_gap(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    status = store.migrate_auto_run_directories()

    assert status.complete is True
    assert not (store.auto_runs_root / "1").exists()
    assert store.load_auto_run(record.id).number == 2
    assert store.reserve_auto_run_number() == 3


def test_auto_migration_removes_abandoned_reserved_creation(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    abandoned = store.auto_runs_root / f".creating-{'c' * 32}-1"
    abandoned.mkdir()
    (abandoned / "partial").write_bytes(b"incomplete")

    status = store.migrate_auto_run_directories()

    assert status.complete is True
    assert not abandoned.exists()
    assert store.reserve_auto_run_number() == 2


def test_auto_migration_unlinks_abandoned_creation_symlink_without_following(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    outside = tmp_path / "outside"
    outside.mkdir()
    abandoned = store.auto_runs_root / f".creating-{'d' * 32}-1"
    abandoned.symlink_to(outside, target_is_directory=True)

    status = store.migrate_auto_run_directories()

    assert status.complete is True
    assert not abandoned.exists()
    assert outside.is_dir()


def test_auto_migration_sweeps_abandoned_creation_while_layout_is_blocked(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    abandoned = store.auto_runs_root / f".creating-{'e' * 32}-1"
    abandoned.mkdir()
    corrupt = store.auto_runs_root / ("f" * 32)
    corrupt.mkdir()
    (corrupt / "config.json").write_text("{", encoding="utf-8")

    status = store.migrate_auto_run_directories()

    assert status.complete is False
    assert status.issues
    assert not abandoned.exists()


def test_auto_migration_retains_abandoned_creation_without_durable_high_water(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    abandoned = store.auto_runs_root / f".creating-{'e' * 32}-1"
    abandoned.mkdir()
    store.auto_index_path.unlink()
    store.auto_index_path.with_name(".index.json.bak").unlink()

    status = store.migrate_auto_run_directories()

    assert status.complete is True
    assert abandoned.is_dir()


def test_auto_interrupted_migration_resumes_with_stable_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = auto_project_store(tmp_path)
    earlier = create_legacy_auto_run(
        store,
        auto_id="a" * 32,
        created_at="2026-01-01T00:00:00Z",
    )
    later = create_legacy_auto_run(
        store,
        auto_id="b" * 32,
        created_at="2026-01-02T00:00:00Z",
    )
    original_replace = os.replace

    def interrupt(source: Path, destination: Path, *args, **kwargs) -> None:
        if source == store.auto_runs_root / later.id:
            raise OSError("migration interrupted")
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(OSError, match="migration interrupted"):
        store.migrate_auto_run_directories()
    monkeypatch.setattr(os, "replace", original_replace)

    migrated_config = store.auto_runs_root / "1" / "config.json"
    migrated_config.write_text("{", encoding="utf-8")
    migrated_backup = migrated_config.with_name("config.json.bak")
    assert json.loads(migrated_backup.read_text(encoding="utf-8"))["number"] == 1

    reopened = ProjectStore(store.project)
    status = reopened.migrate_auto_run_directories()

    assert status.complete is True
    assert reopened.load_auto_run(earlier.id).number == 1
    assert reopened.load_auto_run(later.id).number == 2
    assert (reopened.auto_runs_root / "1" / "topic.md").read_bytes() == b"Original topic"
    assert (reopened.auto_runs_root / "2" / "baseline.md").read_bytes() == b""


def test_auto_number_is_additive_strict_and_legacy_compatible(
    tmp_path: Path,
) -> None:
    record = auto_record_fixture(auto_project_store(tmp_path).project.id)
    encoded = record.to_dict()
    encoded.pop("number")
    assert models.AutoRunRecord.from_dict(encoded).number is None

    for bad in (True, False, 0, -1, 1.0, "1"):
        candidate = dict(record.to_dict())
        candidate["number"] = bad
        with pytest.raises((TypeError, ValueError)):
            models.AutoRunRecord.from_dict(candidate)


def test_auto_number_reservation_survives_backup_recovery(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    assert store.reserve_auto_run_number() == 1
    index = store.auto_runs_root / ".index.json"
    index.write_text("{", encoding="utf-8")
    reopened = ProjectStore(store.project)
    assert reopened.reserve_auto_run_number() == 2


def test_auto_creation_publishes_complete_numeric_directory(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    assert store.auto_runs_root.joinpath("1").is_dir()
    assert not store.auto_runs_root.joinpath(record.id).exists()
    assert store.load_auto_run(record.id).number == 1
    assert json.loads(
        (store.auto_runs_root / ".index.json").read_text(encoding="utf-8")
    )["runs"] == {record.id: 1}


def test_interrupted_auto_publication_consumes_number_without_visible_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    original_replace = os.replace

    def interrupt(source: Path, destination: Path) -> None:
        if destination == store.auto_runs_root / "1":
            raise OSError("publication interrupted")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", interrupt)

    with pytest.raises(OSError, match="publication interrupted"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    assert not (store.auto_runs_root / "1").exists()
    assert store.reserve_auto_run_number() == 2


def test_auto_index_parser_rejects_generated_invalid_shapes() -> None:
    valid = {
        "format": "delibra-auto-index/1",
        "next_number": 3,
        "runs": {"a" * 32: 1, "b" * 32: 2},
    }
    assert storage._parse_auto_index(valid) == valid

    generator = random.Random(20260731)
    for _ in range(500):
        candidate = {
            "format": valid["format"],
            "next_number": valid["next_number"],
            "runs": dict(valid["runs"]),
        }
        corruption = generator.choice(
            ("format", "next_number", "runs", "key", "value", "duplicate")
        )
        if corruption == "format":
            candidate["format"] = generator.choice((None, True, 1, "wrong"))
        elif corruption == "next_number":
            candidate["next_number"] = generator.choice(
                (None, True, False, 0, -1, 1.0, "3", [])
            )
        elif corruption == "runs":
            candidate["runs"] = generator.choice((None, True, 1, [], "runs"))
        elif corruption == "key":
            candidate["runs"] = {
                generator.choice(("bad", "g" * 32, "A" * 32, 1)): 1
            }
        elif corruption == "value":
            candidate["runs"] = {
                "a" * 32: generator.choice(
                    (None, True, False, 0, -1, 1.0, "1", [])
                )
            }
        else:
            candidate["runs"] = {"a" * 32: 1, "b" * 32: 1}

        with pytest.raises(StorageError):
            storage._parse_auto_index(candidate)


def test_auto_number_reservation_rejects_legacy_layout(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    legacy = store.auto_runs_root / ("a" * 32)
    legacy.mkdir(mode=0o700)

    with pytest.raises(ConflictError, match="migration"):
        store.reserve_auto_run_number()


def test_auto_record_loads_legacy_token_and_omits_it_on_write(
    tmp_path: Path,
) -> None:
    project_id = auto_project_store(tmp_path).project.id
    legacy = auto_record_fixture(project_id).to_dict()
    legacy.pop("number")
    legacy["active_turn_token"] = "T" * 43

    restored = models.AutoRunRecord.from_dict(legacy)

    assert "active_turn_token" not in restored.to_dict()


def test_auto_record_loads_legacy_preparation_default_and_writes_it(
    tmp_path: Path,
) -> None:
    project_id = auto_project_store(tmp_path).project.id
    legacy = auto_record_fixture(project_id).to_dict()
    legacy.pop("number")
    legacy.pop("preparation_enabled", None)

    restored = models.AutoRunRecord.from_dict(legacy)

    assert restored.preparation_enabled is True
    assert restored.to_dict()["preparation_enabled"] is True


def test_round_record_loads_legacy_json_and_roundtrips_auto_timeout() -> None:
    legacy = models.RoundRecord.from_dict(legacy_round_record())
    assert legacy.auto is None
    assert legacy.timeout is None

    auto = models.AutoRoundDescriptor(
        auto_id="a" * 32,
        phase="discussion",
        cycle=2,
        position=1,
        context_file="inputs/round-02/auto-context.md",
        context_sha256="b" * 64,
        verdict="agree",
    )
    timeout = models.TimeoutRecord(
        initial_seconds=900,
        effective_seconds=1_200,
        hard_cap_seconds=14_400,
        deadline_at="2026-07-19T00:20:00Z",
        version=1,
        extensions=[
            models.TimeoutExtensionRecord(
                added_seconds=300,
                scope="current",
                extended_at="2026-07-19T00:05:00Z",
            )
        ],
    )
    record = models.RoundRecord(
        n=2,
        status="complete",
        error=None,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at="2026-07-19T00:00:00Z",
        finished_at="2026-07-19T00:10:00Z",
        source=models.SourceDescriptor(type="auto"),
        auto=auto,
        timeout=timeout,
    )

    decoded = models.RoundRecord.from_dict(record.to_dict())

    assert decoded.auto == auto
    assert decoded.timeout == timeout


def test_auto_store_publishes_and_clears_exact_manifest_reservation(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    assert store.active_auto_run_id() is None

    store.publish_auto_reservation(record.id)

    assert store.active_auto_run_id() == record.id
    with pytest.raises(ConflictError, match="active Auto"):
        store.publish_auto_reservation("b" * 32)
    with pytest.raises(ConflictError, match="changed"):
        store.clear_auto_reservation("b" * 32)
    store.clear_auto_reservation(record.id)
    assert store.active_auto_run_id() is None


def test_auto_store_reservation_guards_require_inactive_or_exact_active_owner(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    store.require_auto_inactive()
    with pytest.raises(ConflictError, match="does not own"):
        store.require_auto_owner(record.id)

    store.publish_auto_reservation(record.id)

    with pytest.raises(ConflictError, match="active Auto"):
        store.require_auto_inactive()
    assert store.require_auto_owner(record.id).id == record.id
    with pytest.raises(ConflictError, match="does not own"):
        store.require_auto_owner("d" * 32)


def test_auto_store_rejects_digest_and_symlink_tampering(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    assert store.load_auto_artifact(record.id, record.topic, 100_000) == b"Original topic"

    topic_path = store.auto_run_dir(record.id) / "topic.md"
    topic_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(OwnershipError, match="digest"):
        store.load_auto_artifact(record.id, record.topic, 100_000)

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    topic_path.unlink()
    topic_path.symlink_to(outside)
    with pytest.raises(OwnershipError, match="symlink"):
        store.load_auto_artifact(record.id, record.topic, 100_000)


def test_auto_record_decoder_rejects_generated_values_with_wrong_json_types(
    tmp_path: Path,
) -> None:
    encoded = auto_record_fixture(auto_project_store(tmp_path).project.id).to_dict()
    generator = random.Random(20260719)
    integer_fields = (
        "max_cycles",
        "current_cycle",
        "next_participant",
        "future_turn_timeout_seconds",
    )
    invalid_integers: tuple[object, ...] = (True, False, None, [], {}, "1.5")
    invalid_booleans: tuple[object, ...] = (0, 1, None, [], {}, "false")
    string_fields = ("id", "project_id", "status", "agreement_policy", "created_at")
    invalid_strings: tuple[object, ...] = (None, [], {}, 1, True)

    for _ in range(2_000):
        candidate = dict(encoded)
        kind = generator.choice(("integer", "boolean", "string"))
        if kind == "integer":
            candidate[generator.choice(integer_fields)] = generator.choice(
                invalid_integers
            )
        elif kind == "boolean":
            candidate["stop_requested"] = generator.choice(invalid_booleans)
        else:
            candidate[generator.choice(string_fields)] = generator.choice(
                invalid_strings
            )
        with pytest.raises((TypeError, ValueError)):
            models.AutoRunRecord.from_dict(candidate)


def test_auto_store_copies_and_verifies_preparation_and_ephemeral_context(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "round.md"
    source.write_bytes(b"Prepared analysis")

    digest = store.copy_auto_preparation(
        record.id,
        record.participants[0].session_id,
        source,
        source_root,
    )
    context_path, context_digest = store.write_auto_context(
        record.id,
        b"Turn context",
    )

    assert store.load_auto_preparation(
        record.id,
        record.participants[0].session_id,
        digest,
        100,
    ) == b"Prepared analysis"
    assert context_digest == sha256(b"Turn context").hexdigest()
    assert context_path.read_bytes() == b"Turn context"
    store.remove_auto_context(record.id, context_path)
    assert not context_path.exists()


def test_auto_store_rejects_preparation_tampering_and_bounded_round_reads(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    participant = record.participants[0]
    store.create_session(
        models.SessionConfig(
            id=participant.session_id,
            name=participant.name,
            agent=participant.agent,
            model=participant.model,
            effort=participant.effort,
            role_instructions="Test",
            cli_session_id=None,
            status="idle",
            created_at="2026-07-19T00:00:00Z",
            rounds=[],
        )
    )
    rounds = store.rounds_dir(participant.session_id)
    output = rounds / "round-01.md"
    output.write_bytes(b"12345")
    digest = store.copy_auto_preparation(
        record.id,
        participant.session_id,
        output,
        rounds,
    )
    preparation = (
        store.auto_run_dir(record.id) / "preparations" / f"{participant.session_id}.md"
    )
    preparation.write_text("tampered")

    with pytest.raises(OwnershipError, match="digest"):
        store.load_auto_preparation(record.id, participant.session_id, digest, 100)
    with pytest.raises(StorageError, match="byte limit"):
        store.load_round_artifact(participant.session_id, 1, "output", 4)

    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    output.unlink()
    output.symlink_to(outside)
    with pytest.raises(OwnershipError, match="symlink"):
        store.load_round_artifact(participant.session_id, 1, "output", 100)
