from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.storage import read_codex_rate_limits


THREAD = "01a0412b-8a1d-7fe0-b58d-d6db6ca38dcc"
RESET_PRIMARY = 1788756684
RESET_SECONDARY = 1789269494


def token_count(
    *,
    primary_used: object = 7.0,
    secondary_used: object = 16.0,
    primary_window: object = 300,
    secondary_window: object = 10080,
) -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-07T05:40:34.458Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {
                    "primary": {
                        "used_percent": primary_used,
                        "window_minutes": primary_window,
                        "resets_at": RESET_PRIMARY,
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


def read(home: Path, *, thread: str = THREAD, scan_limit: int = 200, read_limit: int = 4096):
    return read_codex_rate_limits(
        home, thread, scan_limit=scan_limit, read_limit=read_limit
    )


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
