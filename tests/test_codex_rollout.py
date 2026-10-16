from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.models import TurnUsage
from app.storage import read_codex_rollout_state, read_latest_codex_rollout_state


THREAD = "01a0412b-8a1d-7fe0-b58d-d6db6ca38dcc"
RESET_PRIMARY = 1788756684
RESET_SECONDARY = 1789269494


def token_count(
    *,
    primary_used: object = 7.0,
    secondary_used: object = 16.0,
    primary_window: object = 300,
    secondary_window: object = 10080,
    primary_reset: object = RESET_PRIMARY,
    info: object = None,
) -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-07T05:40:34.458Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": info,
                "rate_limits": {
                    "primary": {
                        "used_percent": primary_used,
                        "window_minutes": primary_window,
                        "resets_at": primary_reset,
                    },
                    "secondary": {
                        "used_percent": secondary_used,
                        "window_minutes": secondary_window,
                        "resets_at": RESET_SECONDARY,
                    },
                },
            },
        }
    )


def rollout(home: Path, *lines: str, thread: str = THREAD, day: str = "07") -> Path:
    directory = home / "sessions" / "2026" / "09" / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-{day}T10-02-47-{thread}.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


FIXTURES = Path(__file__).parent.parent / "spike" / "fixtures"


def event(payload: dict) -> str:
    return json.dumps({"timestamp": "2026-10-16T14:00:00Z", "type": "event_msg", "payload": payload})


def usage(input_tokens: int, cached: int, output: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "cache_write_input_tokens": 0,
        "total_tokens": input_tokens + output,
    }


def counted(last: dict, total: dict) -> str:
    return event({"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total}, "rate_limits": None})


STARTED = event({"type": "task_started", "turn_id": "t"})


ROUND_START = datetime(2026, 10, 16, 13, 59, tzinfo=timezone.utc)


def state(home: Path, read_limit: int = 1 << 20, since: datetime = ROUND_START):
    return read_codex_rollout_state(
        home, THREAD, scan_limit=200, read_limit=read_limit, turn_since=since
    )


def read(home: Path, *, thread: str = THREAD, scan_limit: int = 200, read_limit: int = 4096):
    return read_codex_rollout_state(
        home, thread, scan_limit=scan_limit, read_limit=read_limit
    ).rate_limits


def test_both_windows_come_from_the_newest_token_count(tmp_path: Path) -> None:
    rollout(
        tmp_path,
        token_count(primary_used=2.0, secondary_used=3.0),
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
        token_count(),
    )

    readings = {reading.window: reading for reading in read(tmp_path)}

    assert readings["five_hour"].used_percent == pytest.approx(7.0)
    assert readings["five_hour"].resets_at == datetime.fromtimestamp(
        RESET_PRIMARY, timezone.utc
    )
    assert readings["seven_day"].used_percent == pytest.approx(16.0)
    assert readings["seven_day"].source == "codex_rollout_token_count"


def test_codex_reports_no_status_because_phase_0_proved_none(tmp_path: Path) -> None:
    rollout(tmp_path, token_count())

    assert {reading.status for reading in read(tmp_path)} == {"unknown"}


def test_a_rollout_being_appended_to_is_tolerated(tmp_path: Path) -> None:
    complete = token_count()
    rollout(tmp_path, complete, complete[: len(complete) // 2])

    assert len(read(tmp_path)) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"primary_used": "high"},
        {"primary_used": 150.0},
        {"primary_used": None},
        {"primary_window": 60},
        {"primary_window": "300"},
        {"primary_reset": None},
        {"primary_reset": "soon"},
        {"primary_reset": -1},
        {"primary_reset": float("inf")},
        {"primary_reset": 10**100},
    ],
)
def test_an_unusable_window_is_dropped_rather_than_guessed(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    rollout(tmp_path, token_count(**kwargs))

    assert [reading.window for reading in read(tmp_path)] == ["seven_day"]


def test_the_read_is_bounded_to_the_tail_so_the_newest_record_still_lands(
    tmp_path: Path,
) -> None:
    filler = json.dumps({"type": "response_item", "payload": {"text": "x" * 900}})
    rollout(tmp_path, *([filler] * 40), token_count())

    # 40 KiB of prompts and responses against a 4 KiB budget: reading the head
    # would bound the cost and miss every rate limit the rollout ever carried.
    readings = read(tmp_path, read_limit=4096)

    assert {reading.window for reading in readings} == {"five_hour", "seven_day"}


def test_a_symlinked_rollout_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(token_count() + "\n", encoding="utf-8")
    directory = tmp_path / "home" / "sessions" / "2026" / "09" / "07"
    directory.mkdir(parents=True)
    (directory / f"rollout-2026-09-07T10-02-47-{THREAD}.jsonl").symlink_to(outside)

    assert read(tmp_path / "home") == []


@pytest.mark.parametrize("thread", ["../../etc", "*", "a/b", "", "x" * 200])
def test_a_thread_id_is_never_interpolated_into_a_pattern_unchecked(
    tmp_path: Path, thread: str
) -> None:
    rollout(tmp_path, token_count())

    assert read(tmp_path, thread=thread) == []


def test_an_unobserved_thread_reports_nothing(tmp_path: Path) -> None:
    assert read(tmp_path) == []


def test_discovery_is_bounded_by_the_scan_limit(tmp_path: Path) -> None:
    rollout(tmp_path, token_count(), day="05")
    rollout(tmp_path, token_count(), day="06")
    rollout(tmp_path, json.dumps({"type": "session_meta"}), day="07")

    assert read(tmp_path, scan_limit=1) == []
    assert len(read(tmp_path, scan_limit=3)) == 2


def test_latest_quota_discovery_does_not_require_a_thread_id(tmp_path: Path) -> None:
    rollout(tmp_path, token_count(primary_used=63.0, secondary_used=27.0))

    readings = read_latest_codex_rollout_state(
        tmp_path,
        scan_limit=200,
        read_limit=4096,
    ).rate_limits

    assert {reading.window: reading.used_percent for reading in readings} == {
        "five_hour": 63.0,
        "seven_day": 27.0,
    }


def test_the_context_window_comes_from_the_same_bounded_rollout_read(
    tmp_path: Path,
) -> None:
    # `exec --json` never reports it, so this one read carries both the quota
    # percentages and the occupancy denominator.
    rollout(tmp_path, token_count(info={"model_context_window": 258400}))

    state = read_codex_rollout_state(
        tmp_path, THREAD, scan_limit=200, read_limit=4096
    )

    assert state.context_window == 258400
    assert {reading.window for reading in state.rate_limits} == {
        "five_hour",
        "seven_day",
    }


@pytest.mark.parametrize("window", [0, -1, "258400", 1.5, None, True])
def test_an_unusable_context_window_stays_unknown(tmp_path: Path, window) -> None:
    rollout(tmp_path, token_count(info={"model_context_window": window}))

    state = read_codex_rollout_state(
        tmp_path, THREAD, scan_limit=200, read_limit=4096
    )

    assert state.context_window is None


def test_a_resumed_turn_is_billed_from_its_own_rollout_segment(tmp_path: Path) -> None:
    # The measured A -> B -> C sequence from spike/fixtures/m5/codex_*.jsonl.
    rollout(
        tmp_path,
        STARTED, counted(usage(12260, 0, 5), usage(12260, 0, 5)),
        STARTED, counted(usage(32759, 12032, 5), usage(45019, 12032, 10)),
        STARTED, counted(usage(32777, 32640, 5), usage(77796, 44672, 15)),
    )

    read = state(tmp_path)

    assert read.turn_usage == TurnUsage(
        input_tokens=32777, output_tokens=5, cache_read_tokens=32640,
        cache_creation_tokens=0, reasoning_tokens=0,
    )
    assert read.last_tokens == 32782


def test_a_repeated_token_count_is_billed_once(tmp_path: Path) -> None:
    first = counted(usage(100, 0, 10), usage(1100, 0, 60))
    rollout(
        tmp_path,
        STARTED, first, first,
        counted(usage(200, 100, 20), usage(1300, 100, 80)),
    )

    read = state(tmp_path)

    assert (read.turn_usage.input_tokens, read.turn_usage.output_tokens) == (300, 30)
    assert read.last_tokens == 220


def test_billing_is_unknown_when_the_turn_start_is_outside_the_tail(tmp_path: Path) -> None:
    rollout(tmp_path, counted(usage(32777, 32640, 5), usage(77796, 44672, 15)))

    read = state(tmp_path)

    assert read.turn_usage is None
    assert read.last_tokens == 32782


def test_a_turn_without_token_counts_never_borrows_the_previous_turn(tmp_path: Path) -> None:
    rollout(tmp_path, STARTED, counted(usage(12260, 0, 5), usage(12260, 0, 5)), STARTED)

    read = state(tmp_path)

    assert (read.turn_usage, read.last_tokens) == (None, None)


def test_a_run_that_wrote_no_segment_never_takes_the_previous_one(tmp_path: Path) -> None:
    # Codex review (High): a resumed process that exits before task_started.
    rollout(tmp_path, STARTED, counted(usage(32777, 32640, 5), usage(77796, 44672, 15)))

    read = state(tmp_path, since=datetime(2026, 10, 16, 14, 0, 1, tzinfo=timezone.utc))

    assert (read.turn_usage, read.last_tokens) == (None, None)


def test_a_record_without_a_timestamp_proves_nothing(tmp_path: Path) -> None:
    bare = json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
    rollout(tmp_path, bare, counted(usage(100, 0, 10), usage(100, 0, 10)))

    read = state(tmp_path)

    assert read.turn_usage is None
    assert read.last_tokens == 110


def test_the_measured_resumed_turn_is_billed_as_one_round(tmp_path: Path) -> None:
    # Real 0.162.1 output: the fixture flattens rollout records, so re-wrap them.
    lines = []
    for line in (FIXTURES / "m5" / "codex_c_small_resume.jsonl").read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") in {"task_started", "token_count", "task_complete"}:
            lines.append(event(payload))
            if payload["type"] == "token_count":
                last = payload["info"]["last_token_usage"]
    rollout(tmp_path, *lines)

    read = state(tmp_path)

    assert read.turn_usage.input_tokens == last["input_tokens"]
    assert read.turn_usage.cache_read_tokens == last["cached_input_tokens"]
    assert read.turn_usage.output_tokens == last["output_tokens"]
    assert read.last_tokens == last["total_tokens"]
