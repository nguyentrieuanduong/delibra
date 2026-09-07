from __future__ import annotations

from datetime import datetime, timezone
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


def test_numbered_auto_listing_uses_number_not_wall_clock(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    create_numeric_auto_run(
        store,
        number=1,
        auto_id="a" * 32,
        created_at="2026-01-02T00:00:00Z",
    )
    create_numeric_auto_run(
        store,
        number=2,
        auto_id="b" * 32,
        created_at="2026-01-01T00:00:00Z",
    )
    assert [record.number for record in store.list_auto_runs()] == [1, 2]


def test_auto_reference_parser_accepts_only_canonical_numbers_or_uuids() -> None:
    generator = random.Random(20260801)
    alphabet = "0123456789abcdefABCDEF+-. e_xyz"
    valid = {str(number) for number in range(1, 100)}
    valid.update({"a" * 32, "0123456789abcdef" * 2})
    candidates = set(valid)
    candidates.update(
        "".join(generator.choice(alphabet) for _ in range(generator.randrange(0, 40)))
        for _ in range(1_000)
    )

    for candidate in candidates:
        is_number = (
            candidate.isascii()
            and candidate.isdecimal()
            and not candidate.startswith("0")
        )
        is_uuid = len(candidate) == 32 and all(
            character in "0123456789abcdef" for character in candidate
        )
        if is_number or is_uuid:
            parsed = storage.parse_auto_reference(candidate)
            assert parsed == (int(candidate) if is_number else candidate)
        else:
            with pytest.raises(ValueError, match="Auto run reference is invalid"):
                storage.parse_auto_reference(candidate)


def test_auto_run_reference_loads_number_and_uuid_to_the_same_record(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    expected = create_numeric_auto_run(
        store,
        number=7,
        auto_id="a" * 32,
    )

    assert store.load_auto_run_reference("7").id == expected.id
    assert store.load_auto_run_reference(expected.id).number == 7


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


def test_auto_migration_retains_abandoned_creation_and_its_reserved_number(
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
    assert store.reserve_auto_run_number() == 2


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
    assert models.AutoRunRecord.from_dict(encoded).number is None

    for bad in (True, False, 0, -1, 1.0, "1"):
        candidate = dict(record.to_dict())
        candidate["number"] = bad
        with pytest.raises((TypeError, ValueError)):
            models.AutoRunRecord.from_dict(candidate)


def test_legacy_auto_record_serialization_round_trips_without_null_number(
    tmp_path: Path,
) -> None:
    record = auto_record_fixture(auto_project_store(tmp_path).project.id)

    encoded = record.to_dict()

    assert "number" not in encoded
    assert models.AutoRunRecord.from_dict(encoded) == record


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
    legacy["active_turn_token"] = "T" * 43

    restored = models.AutoRunRecord.from_dict(legacy)

    assert "active_turn_token" not in restored.to_dict()


def test_auto_record_loads_legacy_preparation_default_and_writes_it(
    tmp_path: Path,
) -> None:
    project_id = auto_project_store(tmp_path).project.id
    legacy = auto_record_fixture(project_id).to_dict()
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


def test_auto_record_loads_delibra_auto_1_records_without_resumptions(
    tmp_path: Path,
) -> None:
    # delibra-auto/1 records predate the field entirely and must still load.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    encoded = record.to_dict()
    encoded.pop("resumptions", None)

    decoded = models.AutoRunRecord.from_dict(encoded)

    assert decoded.resumptions == []


def test_auto_record_round_trips_resumptions(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.status = "stopped"
    record.finished_at = "2026-09-06T00:05:00Z"
    record.resumptions = [
        models.AutoResumption(
            resumed_at="2026-09-06T00:06:00Z",
            from_status="stopped",
            max_cycles=5,
            turn_timeout_seconds=120,
        )
    ]
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    reloaded = store.load_auto_run(record.id)

    assert reloaded.resumptions == record.resumptions
    assert models.AutoRunRecord.from_dict(record.to_dict()).resumptions == (
        record.resumptions
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"from_status": "discussing"},
        {"from_status": "not-a-status"},
        {"max_cycles": 0},
        {"max_cycles": 101},
        {"turn_timeout_seconds": 0},
        {"resumed_at": "not-a-timestamp"},
    ],
)
def test_auto_store_shape_checks_every_resumption_entry(
    tmp_path: Path,
    mutation: dict,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    valid = {
        "resumed_at": "2026-09-06T00:06:00Z",
        "from_status": "stopped",
        "max_cycles": 5,
        "turn_timeout_seconds": 120,
    }
    record.resumptions = [models.AutoResumption(**{**valid, **mutation})]

    with pytest.raises(OwnershipError, match="resumption"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


@pytest.mark.parametrize("cycles", [1, 20, 21, 100])
def test_auto_store_accepts_cycle_limits_up_to_the_lifetime_cap(
    tmp_path: Path,
    cycles: int,
) -> None:
    # The creation cap is 20, but a run that ends at cycle 20 reconstructs to 21
    # on resume, so persistence must admit the whole lifetime range.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.max_cycles = cycles
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    assert store.load_auto_run(record.id).max_cycles == cycles


@pytest.mark.parametrize("cycles", [0, -1, 101, 1_000])
def test_auto_store_rejects_cycle_limits_outside_the_lifetime_cap(
    tmp_path: Path,
    cycles: int,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.max_cycles = cycles

    with pytest.raises(OwnershipError, match="cycle limit"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


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


def test_missing_auto_reservation_owner_has_an_actionable_conflict(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["active_auto_run_id"] = "f" * 32
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ConflictError, match="active Auto run is missing") as raised:
        store.require_auto_inactive()

    assert "Retry Auto migration" not in str(raised.value)


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


def quota_pause(**overrides) -> models.AutoQuotaPause:
    fields = {
        "provider": "codex",
        "account_key": "default",
        "window": "five_hour",
        "used_percent": 94.0,
        "status": "unknown",
        "resets_at": datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        "source": "codex_rollout_token_count",
    }
    fields.update(overrides)
    paused_at = fields.pop("paused_at", datetime(2026, 9, 7, 12, 0, 1, tzinfo=timezone.utc))
    return models.AutoQuotaPause(
        observation=models.RateLimitObservation(**fields),
        paused_at=paused_at,
    )


def quota_override(**overrides) -> models.AutoQuotaOverride:
    fields = {
        "provider": "codex",
        "account_key": "default",
        "window": "five_hour",
        "resets_at": datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc),
        "used_percent_at_grant": 94.0,
        "granted_at": datetime(2026, 9, 7, 12, 1, tzinfo=timezone.utc),
        "granted_from_status": "unknown",
    }
    fields.update(overrides)
    return models.AutoQuotaOverride(**fields)


def test_auto_record_loads_delibra_auto_1_records_without_a_quota_pause(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    encoded = record.to_dict()
    encoded.pop("quota_pause", None)

    assert models.AutoRunRecord.from_dict(encoded).quota_pause is None


def test_auto_record_round_trips_the_quota_pause_that_caused_the_stop(
    tmp_path: Path,
) -> None:
    # The Continue disclosure needs the window, the observed figure and the
    # reset instant; recovering those by parsing terminal_reason prose would
    # break the moment the wording changes.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.status = "stopped"
    record.finished_at = "2026-09-07T12:00:01Z"
    record.terminal_reason = "paused: Codex 5-hour limit, 6% remaining"
    record.quota_pause = quota_pause()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    reloaded = store.load_auto_run(record.id)

    assert reloaded.quota_pause == record.quota_pause
    assert reloaded.quota_pause.observation.remaining_percent == pytest.approx(6.0)
    assert reloaded.quota_pause.observation.key == ("codex", "default", "five_hour")


def test_auto_record_round_trips_a_quota_override_and_loads_legacy_none(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    encoded = record.to_dict()
    encoded.pop("quota_override", None)
    assert models.AutoRunRecord.from_dict(encoded).quota_override is None

    record.number = store.reserve_auto_run_number()
    record.quota_override = quota_override()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    assert store.load_auto_run(record.id).quota_override == record.quota_override


@pytest.mark.parametrize(
    "mutation",
    [
        {"window": "monthly"},
        {"used_percent_at_grant": 101.0},
        {"granted_from_status": "stopped"},
        {"provider": ""},
        {"resets_at": datetime(2026, 9, 7, 14, 0)},
        {"granted_at": datetime(2026, 9, 7, 12, 1)},
    ],
)
def test_quota_override_rejects_malformed_grants(mutation: dict) -> None:
    with pytest.raises(ValueError):
        quota_override(**mutation)


def test_quota_override_is_live_only_for_its_exact_window() -> None:
    grant = quota_override()
    observation = quota_pause().observation

    assert grant.is_live_for(
        observation,
        now=datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )
    assert not grant.is_live_for(
        quota_pause(
            resets_at=datetime(2026, 9, 7, 19, 0, tzinfo=timezone.utc)
        ).observation,
        now=datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )
    assert not grant.is_live_for(
        observation,
        now=datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )


def test_quota_override_without_a_reset_expires_by_grant_staleness() -> None:
    grant = quota_override(
        resets_at=None,
        granted_at=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )
    observation = quota_pause(resets_at=None).observation

    assert grant.is_live_for(
        observation,
        now=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )
    assert not grant.is_live_for(
        observation,
        now=datetime(2026, 9, 7, 11, 59, 59, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )
    assert not grant.is_live_for(
        observation,
        now=datetime(2026, 9, 7, 12, 30, 1, tzinfo=timezone.utc),
        staleness_seconds=1800,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"window": "monthly"},
        {"status": "throttled"},
        {"used_percent": 150.0},
        {"provider": ""},
        {"source": "guesswork"},
    ],
)
def test_auto_store_shape_checks_the_quota_pause(
    tmp_path: Path, mutation: dict
) -> None:
    with pytest.raises(ValueError):
        quota_pause(**mutation)


def test_a_quota_pause_needs_an_aware_pause_instant(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        quota_pause(paused_at=datetime(2026, 9, 7, 12, 0, 1))


def test_a_tampered_quota_pause_on_disk_is_refused(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.status = "stopped"
    record.finished_at = "2026-09-07T12:00:01Z"
    record.quota_pause = quota_pause()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    config_path = store.auto_run_dir(record.id) / "config.json"
    encoded = json.loads(config_path.read_text(encoding="utf-8"))
    encoded["quota_pause"]["observation"]["used_percent"] = 150.0
    config_path.write_text(json.dumps(encoded), encoding="utf-8")

    with pytest.raises(StorageError, match="Auto run config is invalid"):
        store.load_auto_run(record.id)


def compaction_policy(**overrides) -> models.AutoContextPolicy:
    return models.AutoContextPolicy(
        **{
            "mode": "compact",
            "unit": "cycles",
            "interval": 3,
            "threshold_percent": 70,
            "summarizer": "next",
            **overrides,
        }
    )


def summary_fixture(**overrides) -> models.AutoSummary:
    body = b"Summary body"
    return models.AutoSummary(
        **{
            "path": "summaries/01.md",
            "sha256": sha256(body).hexdigest(),
            "created_at": "2026-09-07T12:00:00Z",
            "cycle": 2,
            "round_n": 4,
            "session_id": "b" * 32,
            "retired_baseline_count": 0,
            "retired_discussion_count": 2,
            "dropped_entries": (),
            **overrides,
        }
    )


def attempt_fixture(**overrides) -> models.AutoCompactionAttempt:
    fields = {
        "unit": "cycles",
        "cycle": 2,
        "discussion_len": 2,
        "attempted_at": "2026-09-07T12:00:00Z",
        "outcome": "summarized",
        "round_n": 4,
        "session_id": "b" * 32,
        "summary_index": 0,
        "warning": None,
        **overrides,
    }
    fields.setdefault(
        "trigger_key",
        models.auto_trigger_key(
            fields["unit"],
            fields["cycle"],
            fields["discussion_len"],
        ),
    )
    return models.AutoCompactionAttempt(**fields)


def test_auto_record_loads_delibra_auto_1_records_without_a_context_policy(
    tmp_path: Path,
) -> None:
    # An absent policy means off: a run created before Phase 7 must never start
    # retiring its own material on reload.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")
    encoded = record.to_dict()
    for key in (
        "context_policy",
        "retired_baseline_count",
        "retired_discussion_count",
        "summaries",
        "compaction_attempts",
        "compaction_cooldown_until_discussion_len",
        "consecutive_compaction_failures",
        "compaction_disabled_reason",
    ):
        encoded.pop(key, None)

    decoded = models.AutoRunRecord.from_dict(encoded)

    assert decoded.context_policy is None
    assert decoded.effective_context_policy.mode == "off"
    assert decoded.retired_baseline_count == 0
    assert decoded.retired_discussion_count == 0
    assert decoded.summaries == []
    assert decoded.compaction_attempts == []
    assert decoded.compaction_cooldown_until_discussion_len == 0
    assert decoded.consecutive_compaction_failures == 0
    assert decoded.compaction_disabled_reason is None


def test_auto_record_round_trips_the_policy_its_cursors_and_its_attempts(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.status = "discussing"
    record.current_cycle = 2
    record.discussion = [
        models.AutoTurn(
            phase="discussion",
            session_id="b" * 32,
            round_n=index + 1,
            cycle=1,
            position=index,
            output_sha256=sha256(f"out {index}".encode()).hexdigest(),
            verdict="continue",
        )
        for index in range(2)
    ]
    record.context_policy = compaction_policy(summarizer="c" * 32)
    record.retired_discussion_count = 2
    record.summaries = [summary_fixture()]
    record.compaction_attempts = [attempt_fixture()]
    record.compaction_cooldown_until_discussion_len = 4
    record.consecutive_compaction_failures = 1
    record.compaction_disabled_reason = None
    store.create_auto_run(record, topic=b"Original topic", baseline=b"")

    reloaded = store.load_auto_run(record.id)

    assert reloaded.context_policy == record.context_policy
    assert reloaded.effective_context_policy == record.context_policy
    assert reloaded.retired_discussion_count == 2
    assert reloaded.summaries == record.summaries
    assert reloaded.compaction_attempts == record.compaction_attempts
    assert reloaded.compaction_cooldown_until_discussion_len == 4
    assert reloaded.consecutive_compaction_failures == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"mode": "summarise"},
        {"unit": "tokens"},
        {"interval": 0},
        {"interval": 21},
        {"threshold_percent": 9},
        {"threshold_percent": 96},
        {"summarizer": ""},
        {"interval": True},
        {"threshold_percent": 70.0},
    ],
)
def test_auto_context_policy_rejects_values_outside_its_declared_range(
    mutation: dict,
) -> None:
    with pytest.raises(ValueError):
        compaction_policy(**mutation)


def test_auto_store_rejects_a_summarizer_that_is_not_a_participant(
    tmp_path: Path,
) -> None:
    # A named summarizer is the one policy field that cannot be validated by the
    # model alone: only the record knows who is speaking.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.context_policy = compaction_policy(summarizer="d" * 32)

    with pytest.raises(OwnershipError, match="summarizer"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("retired_baseline_count", -1),
        ("retired_baseline_count", 1),
        ("retired_discussion_count", -1),
        ("retired_discussion_count", 3),
        ("compaction_cooldown_until_discussion_len", -1),
        ("consecutive_compaction_failures", -1),
    ],
)
def test_auto_store_rejects_retirement_cursors_outside_their_lists(
    tmp_path: Path,
    field_name: str,
    value: int,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.discussion = [
        models.AutoTurn(
            phase="discussion",
            session_id="b" * 32,
            round_n=index + 1,
            cycle=1,
            position=index,
            output_sha256=sha256(f"out {index}".encode()).hexdigest(),
            verdict="continue",
        )
        for index in range(2)
    ]
    setattr(record, field_name, value)

    with pytest.raises(OwnershipError, match="compaction"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


def test_auto_store_recomputes_every_trigger_key_rather_than_trusting_it(
    tmp_path: Path,
) -> None:
    # The key is the same-boundary duplicate guard, so a forged one would
    # silently suppress a real future boundary -- a failure nothing surfaces.
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.compaction_attempts = [attempt_fixture(trigger_key="cycles:99:99")]

    with pytest.raises(OwnershipError, match="trigger key"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


def test_auto_store_rejects_duplicate_trigger_keys(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.compaction_attempts = [
        attempt_fixture(outcome="failed", summary_index=None, round_n=None),
        attempt_fixture(outcome="failed", summary_index=None, round_n=None),
    ]

    with pytest.raises(OwnershipError, match="trigger key"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


def test_the_validator_bound_sits_above_the_writer_cap_and_is_unreachable(
    tmp_path: Path,
) -> None:
    # A validator bound alone is a trap: reaching it would make a live record
    # unloadable. The writer stops first, so this bound only catches corruption.
    assert storage.AUTO_MAX_COMPACTION_ATTEMPTS < storage.AUTO_MAX_STORED_COMPACTION_ATTEMPTS
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.compaction_attempts = [
        attempt_fixture(cycle=index + 1, summary_index=None, round_n=None)
        for index in range(storage.AUTO_MAX_STORED_COMPACTION_ATTEMPTS + 1)
    ]

    with pytest.raises(OwnershipError, match="compaction attempts"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


@pytest.mark.parametrize(
    "mutation",
    [
        {"path": "../escape.md"},
        {"path": "summaries/01.txt"},
        {"sha256": "nope"},
        {"cycle": 0},
        {"round_n": 0},
        {"created_at": ""},
        {"retired_baseline_count": -1},
        {"retired_discussion_count": -1},
    ],
)
def test_auto_summary_rejects_malformed_references(mutation: dict) -> None:
    with pytest.raises(ValueError):
        summary_fixture(**mutation)


def test_auto_store_rejects_a_summary_from_a_non_participant(tmp_path: Path) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.summaries = [summary_fixture(session_id="d" * 32)]

    with pytest.raises(OwnershipError, match="summary"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


def test_auto_store_rejects_an_attempt_pointing_at_a_missing_summary(
    tmp_path: Path,
) -> None:
    store = auto_project_store(tmp_path)
    record = auto_record_fixture(store.project.id)
    record.number = store.reserve_auto_run_number()
    record.compaction_attempts = [attempt_fixture(summary_index=3)]

    with pytest.raises(OwnershipError, match="summary"):
        store.create_auto_run(record, topic=b"Original topic", baseline=b"")


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "invented"},
        {"cycle": -1},
        {"discussion_len": -1},
        {"attempted_at": ""},
        {"unit": "tokens"},
    ],
)
def test_auto_compaction_attempt_rejects_malformed_entries(mutation: dict) -> None:
    with pytest.raises(ValueError):
        attempt_fixture(**mutation)
