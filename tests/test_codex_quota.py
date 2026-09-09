"""The account-wide Codex quota read (Phase 8b).

Phase 8's measurement proved `account/rateLimits/read` reports the *account*,
not the local `CODEX_HOME`: the same figures came back from two homes whose own
rollouts read 3/18 and 20/37. These tests pin the parser to that measured shape
and pin the reader's one non-negotiable property -- every failure is silent,
because quota state is advisory and no read of it may fail a round or a page.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from app.codex_quota import parse_account_rate_limits, read_codex_account_rate_limits
from app.models import RateLimitReading


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


# --- wiring: which quota source the runner asks, and when -------------------


# The captured fixture's five-hour window reset at 2026-09-07T15:14:20Z.
# UsageMonitor._live drops any observation whose reset instant has passed,
# so a monitor reading this fixture must be held inside that window --
# otherwise these tests pass until the capture expires and then never
# again, which is exactly what happened.
FIXTURE_WINDOW_INSTANT = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)


def _manager(
    tmp_path: Path,
    *,
    clock: Any = None,
    refresh_seconds: int = 300,
) -> Any:
    from app.config import Settings
    from app.runner import RunManager
    from app.storage import LockCoordinator, RegistryStore
    from app.usage import UsageMonitor

    home = tmp_path / "home"
    settings = Settings(home=home, codex_quota_refresh_seconds=refresh_seconds)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["quota_clock"] = clock
    return RunManager(
        registry=RegistryStore(home),
        locks=LockCoordinator(),
        settings=settings,
        # No run is started here; these tests exercise the quota read alone.
        adapter_factory=lambda config: None,
        usage_monitor=UsageMonitor(
            settings=settings,
            clock=lambda: FIXTURE_WINDOW_INSTANT,
        ),
        **kwargs,
    )


def _account_readings() -> list[RateLimitReading]:
    return parse_account_rate_limits(_fixture())


@pytest.mark.asyncio
async def test_the_refresh_prefers_the_account_over_delibras_own_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of Phase 8b: the rollout only sees Delibra's own home."""

    manager = _manager(tmp_path)
    rollout_calls = 0

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        return _account_readings()

    def rollout(*args: Any, **kwargs: Any) -> Any:
        nonlocal rollout_calls
        rollout_calls += 1
        raise AssertionError("the rollout must not be read when the account answers")

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)
    monkeypatch.setattr("app.runner.read_latest_codex_rollout_state", rollout)

    await manager.refresh_codex_quota()

    assert manager.usage.report("codex", "five_hour").used_percent == 20.0
    assert manager.usage.report("codex", "seven_day").used_percent == 37.0
    assert manager.usage.report("codex", "five_hour").source == "codex_app_server"
    assert rollout_calls == 0


@pytest.mark.asyncio
async def test_the_rollout_still_answers_when_the_account_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.storage import CodexRolloutState

    manager = _manager(tmp_path)

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        return []

    def rollout(*args: Any, **kwargs: Any) -> CodexRolloutState:
        return CodexRolloutState(
            [
                RateLimitReading(
                    window="five_hour",
                    used_percent=3.0,
                    status="unknown",
                    resets_at=datetime(2099, 1, 1, tzinfo=UTC),
                    source="codex_rollout_token_count",
                )
            ]
        )

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)
    monkeypatch.setattr("app.runner.read_latest_codex_rollout_state", rollout)

    await manager.refresh_codex_quota()

    observation = manager.usage.report("codex", "five_hour")
    assert observation.used_percent == 3.0
    assert observation.source == "codex_rollout_token_count"


@pytest.mark.asyncio
async def test_a_second_refresh_inside_the_interval_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a badge poll would spawn an app-server process a minute."""

    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    manager = _manager(tmp_path, clock=lambda: now, refresh_seconds=300)
    calls = 0

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        nonlocal calls
        calls += 1
        return _account_readings()

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)
    monkeypatch.setattr(
        "app.runner.read_latest_codex_rollout_state",
        lambda *args, **kwargs: __import__(
            "app.storage", fromlist=["CodexRolloutState"]
        ).CodexRolloutState([]),
    )

    await manager.refresh_codex_quota()
    await manager.refresh_codex_quota()

    assert calls == 1

    now = now.replace(hour=13, minute=6)
    await manager.refresh_codex_quota()

    assert calls == 2


@pytest.mark.asyncio
async def test_a_failed_read_still_starts_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that is down must not become a spawn-per-poll loop."""

    from app.storage import CodexRolloutState

    now = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    manager = _manager(tmp_path, clock=lambda: now)
    calls = 0

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)
    monkeypatch.setattr(
        "app.runner.read_latest_codex_rollout_state",
        lambda *args, **kwargs: CodexRolloutState([]),
    )

    await manager.refresh_codex_quota()
    await manager.refresh_codex_quota()

    assert calls == 1


@pytest.mark.asyncio
async def test_scheduling_a_refresh_never_makes_a_render_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page render returns what the monitor holds; the read lands after."""

    manager = _manager(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        started.set()
        await release.wait()
        return _account_readings()

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)

    manager.schedule_codex_quota_refresh()

    assert manager.usage.report("codex", "five_hour") is None
    await started.wait()
    release.set()
    await manager.drain_codex_quota_refresh()

    assert manager.usage.report("codex", "five_hour").used_percent == 20.0


@pytest.mark.asyncio
async def test_scheduling_twice_runs_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    calls = 0

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _account_readings()

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)

    manager.schedule_codex_quota_refresh()
    manager.schedule_codex_quota_refresh()
    await manager.drain_codex_quota_refresh()

    assert calls == 1


def test_scheduling_outside_an_event_loop_is_a_no_op(tmp_path: Path) -> None:
    """Rendering in a sync test must not raise for want of a running loop."""

    _manager(tmp_path).schedule_codex_quota_refresh()


@pytest.mark.asyncio
async def test_the_pause_check_reads_the_account_before_deciding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's pre-dispatch check is a decision point, so it waits for the read."""

    manager = _manager(tmp_path)

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        return [
            RateLimitReading(
                window="five_hour",
                used_percent=99.0,
                status="unknown",
                resets_at=datetime(2099, 1, 1, tzinfo=UTC),
                source="codex_app_server",
            )
        ]

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)

    observation = await manager.quota_pause_observation("codex")

    assert observation is not None
    assert observation.used_percent == 99.0


@pytest.mark.asyncio
async def test_the_pause_check_leaves_other_providers_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)

    async def account(*args: Any, **kwargs: Any) -> list[RateLimitReading]:
        raise AssertionError("a Claude turn must not read Codex quota")

    monkeypatch.setattr("app.runner.read_codex_account_rate_limits", account)

    assert await manager.quota_pause_observation("claude") is None


def test_fixture_window_instant_precedes_every_captured_reset() -> None:
    """The capture is dated; its test clock must precede every reset.

    Phase 8's value is the exact instants it measured, so the fixture keeps
    them. That makes every assertion on used_percent a time bomb unless the
    monitor reading it is frozen inside the captured window.
    """

    resets = [
        reading.resets_at for reading in _account_readings() if reading.resets_at
    ]

    assert resets, "the capture must carry reset instants"
    assert FIXTURE_WINDOW_INSTANT < min(resets), (
        "FIXTURE_WINDOW_INSTANT must sit inside every captured window"
    )
