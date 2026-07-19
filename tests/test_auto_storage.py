from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import random

import pytest

import app.models as models
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
        status="preparing",
        agreement_policy="all_agree",
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


def test_auto_record_loads_legacy_token_and_omits_it_on_write(
    tmp_path: Path,
) -> None:
    project_id = auto_project_store(tmp_path).project.id
    legacy = auto_record_fixture(project_id).to_dict()
    legacy["active_turn_token"] = "T" * 43

    restored = models.AutoRunRecord.from_dict(legacy)

    assert "active_turn_token" not in restored.to_dict()


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
