"""The account-wide Codex quota read (Phase 8b).

Phase 8's measurement proved `account/rateLimits/read` reports the *account*,
not the local `CODEX_HOME`: the same figures came back from two homes whose own
rollouts read 3/18 and 20/37. These tests pin the parser to that measured shape
and pin the reader's one non-negotiable property -- every failure is silent,
because quota state is advisory and no read of it may fail a round or a page.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from app.codex_quota import parse_account_rate_limits, read_codex_account_rate_limits


FIXTURE = Path("spike/fixtures/m8/codex_app_server_rate_limits.json")


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text("utf-8"))


def _bucket(**overrides: Any) -> dict[str, Any]:
    bucket = {
        "primary": {
            "resetsAt": 1788794060,
            "usedPercent": 20,
            "windowDurationMins": 300,
        },
        "secondary": {
            "resetsAt": 1789269494,
            "usedPercent": 37,
            "windowDurationMins": 10080,
        },
    }
    bucket.update(overrides)
    return {"rateLimits": bucket}


def test_the_measured_response_parses_to_both_windows() -> None:
    readings = {
        reading.window: reading for reading in parse_account_rate_limits(_fixture())
    }

    assert set(readings) == {"five_hour", "seven_day"}
    assert readings["five_hour"].used_percent == 20.0
    assert readings["seven_day"].used_percent == 37.0
    # The exact instants the operator's own rollout reported for the same
    # account, to the second -- which is what corroborates the units.
    assert readings["five_hour"].resets_at == datetime(
        2026, 9, 7, 15, 14, 20, tzinfo=UTC
    )
    assert readings["seven_day"].resets_at == datetime(
        2026, 9, 13, 3, 18, 14, tzinfo=UTC
    )
    assert {reading.source for reading in readings.values()} == {"codex_app_server"}
    # Phase 0 proved no Codex status, and this method reports none either.
    assert {reading.status for reading in readings.values()} == {"unknown"}


def test_the_window_comes_from_its_duration_not_the_slot_name() -> None:
    """`primary` is not a synonym for five-hour; only the duration decides."""

    swapped = _bucket(
        primary={"resetsAt": 1789269494, "usedPercent": 37, "windowDurationMins": 10080},
        secondary={"resetsAt": 1788794060, "usedPercent": 20, "windowDurationMins": 300},
    )

    readings = {
        reading.window: reading.used_percent
        for reading in parse_account_rate_limits(swapped)
    }

    assert readings == {"seven_day": 37.0, "five_hour": 20.0}


def test_by_limit_id_is_preferred_over_the_flat_bucket() -> None:
    payload = {
        "rateLimits": {
            "primary": {
                "resetsAt": 1788794060,
                "usedPercent": 1,
                "windowDurationMins": 300,
            }
        },
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "resetsAt": 1788794060,
                    "usedPercent": 20,
                    "windowDurationMins": 300,
                }
            }
        },
    }

    [reading] = parse_account_rate_limits(payload)

    assert reading.used_percent == 20.0


def test_several_limit_ids_reduce_to_the_highest_used_percent() -> None:
    """5b's merge would let a later reset replace a higher figure outright.

    So the reduction happens here, inside one read, where both readings
    describe the same instant in the same account.
    """

    payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "resetsAt": 1788794060,
                    "usedPercent": 20,
                    "windowDurationMins": 300,
                }
            },
            "other": {
                "primary": {
                    "resetsAt": 1788999999,
                    "usedPercent": 91,
                    "windowDurationMins": 300,
                }
            },
        }
    }

    [reading] = parse_account_rate_limits(payload)

    assert reading.used_percent == 91.0
    assert reading.resets_at == datetime.fromtimestamp(1788999999, UTC)


@pytest.mark.parametrize(
    "window",
    [
        {"resetsAt": 1788794060, "usedPercent": 20, "windowDurationMins": 60},
        {"resetsAt": 1788794060, "usedPercent": 101, "windowDurationMins": 300},
        {"resetsAt": 1788794060, "usedPercent": -1, "windowDurationMins": 300},
        {"resetsAt": 1788794060, "usedPercent": "20", "windowDurationMins": 300},
        {"resetsAt": 1788794060, "usedPercent": True, "windowDurationMins": 300},
        {"resetsAt": 1788794060, "windowDurationMins": 300},
        {"resetsAt": 0, "usedPercent": 20, "windowDurationMins": 300},
        {"usedPercent": 20, "windowDurationMins": 300},
        {"resetsAt": 1788794060, "usedPercent": 20, "windowDurationMins": "300"},
        "not a mapping",
    ],
)
def test_an_unusable_window_is_dropped_rather_than_guessed_at(window: Any) -> None:
    assert parse_account_rate_limits(_bucket(primary=window, secondary=None)) == []


@pytest.mark.parametrize("payload", [None, [], "text", {}, {"rateLimits": None}])
def test_an_unusable_payload_yields_nothing(payload: Any) -> None:
    assert parse_account_rate_limits(payload) == []


def test_one_unusable_window_does_not_discard_its_sibling() -> None:
    [reading] = parse_account_rate_limits(
        _bucket(primary={"usedPercent": 20, "windowDurationMins": 300})
    )

    assert reading.window == "seven_day"


@pytest.mark.asyncio
async def test_a_missing_executable_reports_nothing_and_never_raises(
    tmp_path: Path,
) -> None:
    readings = await read_codex_account_rate_limits(
        tmp_path,
        executable=str(tmp_path / "no-such-codex"),
        timeout_seconds=5,
    )

    assert readings == []


@pytest.mark.asyncio
async def test_a_server_that_never_replies_times_out_quietly(tmp_path: Path) -> None:
    script = tmp_path / "hanging-codex"
    script.write_text("#!/bin/sh\nexec cat >/dev/null\n", encoding="utf-8")
    script.chmod(0o755)

    readings = await read_codex_account_rate_limits(
        tmp_path, executable=str(script), timeout_seconds=1
    )

    assert readings == []


@pytest.mark.asyncio
async def test_a_protocol_error_reply_reports_nothing(tmp_path: Path) -> None:
    script = tmp_path / "erroring-codex"
    script.write_text(
        "#!/bin/sh\n"
        'echo \'{"id":1,"result":{}}\'\n'
        'echo \'{"id":2,"error":{"code":-32601,"message":"no such method"}}\'\n'
        "exec cat >/dev/null\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    readings = await read_codex_account_rate_limits(
        tmp_path, executable=str(script), timeout_seconds=10
    )

    assert readings == []


@pytest.mark.asyncio
async def test_the_handshake_reaches_the_rate_limits_method(tmp_path: Path) -> None:
    """A stand-in server replies only after both handshake messages arrive."""

    transcript = tmp_path / "transcript.jsonl"
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/bin/sh\n"
        "while IFS= read -r line; do\n"
        f'  printf \'%s\\n\' "$line" >> "{transcript}"\n'
        "  case \"$line\" in\n"
        "    *rateLimits/read*)\n"
        # A notification between the request and its reply: the reader must
        # match on the request id rather than taking the next line it sees.
        '      echo \'{"method":"someNotification","params":{}}\'\n'
        '      echo \'{"id":2,"result":{"rateLimits":{"primary":'
        '{"resetsAt":1788794060,"usedPercent":20,"windowDurationMins":300}}}}\'\n'
        "      ;;\n"
        '    *\\"initialized\\"*) ;;\n'
        '    *\\"initialize\\"*) echo \'{"id":1,"result":{"userAgent":"fake"}}\' ;;\n'
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    readings = await read_codex_account_rate_limits(
        tmp_path, executable=str(script), timeout_seconds=10
    )

    assert [(item.window, item.used_percent) for item in readings] == [
        ("five_hour", 20.0)
    ]
    sent = [json.loads(line) for line in transcript.read_text("utf-8").splitlines()]
    assert [message.get("method") for message in sent] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]


@pytest.mark.asyncio
async def test_the_read_runs_against_delibras_own_codex_home(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    script = tmp_path / "home-reporting-codex"
    script.write_text(
        "#!/bin/sh\n"
        f'printf %s "$CODEX_HOME" > "{tmp_path}/seen-home"\n'
        'echo \'{"id":1,"result":{}}\'\n'
        'echo \'{"id":2,"result":{"rateLimits":{}}}\'\n'
        "sleep 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    await read_codex_account_rate_limits(
        home, executable=str(script), timeout_seconds=10
    )

    assert (tmp_path / "seen-home").read_text("utf-8") == str(home)
