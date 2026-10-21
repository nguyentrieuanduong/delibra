import pytest

from app.models import AutoBuildGit, AutoHandover, AutoRunRecord, AutoUnavailable

BASE = {
    "format": "delibra-auto/1",
    "id": "c" * 32,
    "project_id": "d" * 32,
    "number": 1,
    "status": "converged",
    "agreement_policy": "first_agree",
    "preparation_enabled": False,
    "max_cycles": 2,
    "current_cycle": 1,
    "next_participant": 1,
    "participants": [
        {"agent": "fake", "effort": "low", "model": "success", "name": "A", "session_id": "a" * 32},
        {"agent": "fake", "effort": "low", "model": "success", "name": "B", "session_id": "b" * 32},
    ],
    "topic": {"path": "topic.md", "sha256": "0" * 64},
    "baseline": {"path": "baseline.md", "sha256": "0" * 64},
    "future_turn_timeout_seconds": 60,
    "stop_requested": False,
    "created_at": "2026-10-15T00:00:00Z",
}


def test_legacy_record_loads_as_discussion_and_serializes_unchanged() -> None:
    record = AutoRunRecord.from_dict(dict(BASE))

    assert record.kind == "discussion"
    assert record.unavailable == []
    assert record.handovers == []
    assert record.git is None
    payload = record.to_dict()
    for key in ("kind", "unavailable", "handovers", "git"):
        assert key not in payload


def test_build_record_round_trips() -> None:
    data = dict(
        BASE,
        kind="build",
        unavailable=[{"session_id": "a" * 32, "reason": "quota", "round_n": 3, "at": "2026-10-15T00:01:00Z"}],
        handovers=[{"from_session_id": "a" * 32, "to_session_id": "b" * 32,
                    "reason": "quota", "cycle": 1, "round_n": 3,
                    "started_epoch": 12.5, "discussion_len": 2}],
        git={"enabled": True, "reason": None, "branch": "main", "head": "abc123"},
    )

    record = AutoRunRecord.from_dict(data)

    assert record.unavailable == [AutoUnavailable("a" * 32, "quota", 3, "2026-10-15T00:01:00Z")]
    assert record.handovers == [AutoHandover("a" * 32, "b" * 32, "quota", 1, 3, 12.5, 2)]
    assert record.git == AutoBuildGit(True, None, "main", "abc123")
    assert AutoRunRecord.from_dict(record.to_dict()) == record
    assert record.to_dict()["kind"] == "build"


def test_pre_turn_handover_has_no_round_or_target() -> None:
    data = dict(
        BASE,
        kind="build",
        unavailable=[{"session_id": "a" * 32, "reason": "quota", "round_n": None, "at": "t"}],
        handovers=[{"from_session_id": "a" * 32, "to_session_id": None,
                    "reason": "quota", "cycle": 1, "round_n": None}],
    )

    record = AutoRunRecord.from_dict(data)

    assert record.handovers[0].to_session_id is None
    assert record.unavailable[0].round_n is None


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        AutoRunRecord.from_dict(dict(BASE, kind="other"))


@pytest.mark.parametrize("value", ["1.5", True])
def test_handover_epoch_must_be_a_number(value) -> None:
    data = {
        "from_session_id": "a" * 32, "to_session_id": None, "reason": "quota",
        "cycle": 1, "round_n": None, "started_epoch": value,
    }
    with pytest.raises(TypeError):
        AutoHandover.from_dict(data)


def test_cli_notes_round_trip_and_stay_off_old_records() -> None:
    record = AutoRunRecord.from_dict(dict(BASE, kind="build"))
    assert record.cli_notes == []
    assert "cli_notes" not in record.to_dict()
    record.cli_notes = ["codex CLI codex-cli 0.163.0 is newer than the spike-verified codex-cli 0.162.1"]
    assert AutoRunRecord.from_dict(record.to_dict()).cli_notes == record.cli_notes
