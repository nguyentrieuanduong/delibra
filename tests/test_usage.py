from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.models import RateLimitObservation, RateLimitReading
from app.usage import UsageMonitor, quota_verdict


SETTINGS = Settings(home=Path("/tmp/delibra-usage-test"))
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=2)


def observation(
    *,
    window: str = "five_hour",
    used_percent: float | None = 10.0,
    status: str = "unknown",
    resets_at: datetime | None = RESET,
    observed_at: datetime = NOW,
    provider: str = "codex",
) -> RateLimitObservation:
    return RateLimitObservation(
        provider=provider,
        account_key="default",
        window=window,
        used_percent=used_percent,
        status=status,
        resets_at=resets_at,
        observed_at=observed_at,
        source="codex_rollout_token_count",
    )


def monitor(settings: Settings | None = None, now: datetime = NOW) -> UsageMonitor:
    return UsageMonitor(
        settings=settings or SETTINGS,
        clock=lambda: now,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"used_percent": -0.1},
        {"used_percent": 100.1},
        {"used_percent": float("nan")},
        {"used_percent": float("inf")},
        {"used_percent": True},
        {"window": "monthly"},
        {"status": "throttled"},
        {"resets_at": "2026-09-07T14:00:00Z"},
        {"observed_at": datetime(2026, 9, 7, 12, 0)},
    ],
)
def test_malformed_observations_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        observation(**kwargs)  # type: ignore[arg-type]


def test_remaining_percent_is_derived_and_never_stored() -> None:
    assert observation(used_percent=16.0).remaining_percent == pytest.approx(84.0)
    assert observation(used_percent=None).remaining_percent is None
    assert not hasattr(observation(), "_remaining_percent")


def test_round_trip_preserves_the_utc_instant() -> None:
    original = observation(used_percent=7.0, status="warning")

    assert RateLimitObservation.from_dict(original.to_dict()) == original


def test_a_reading_is_stamped_with_the_account_the_runner_knows() -> None:
    reading = RateLimitReading(
        window="five_hour",
        used_percent=None,
        status="rejected",
        resets_at=RESET,
        source="claude_rate_limit_event",
    )

    stamped = reading.observed(
        provider="claude", account_key="default", observed_at=NOW
    )

    assert stamped.provider == "claude"
    assert stamped.status == "rejected"
    assert stamped.observed_at == NOW


def test_an_older_arrival_cannot_lower_a_recorded_percentage() -> None:
    usage = monitor()
    usage.record(observation(used_percent=60.0, observed_at=NOW))

    usage.record(observation(used_percent=10.0, observed_at=NOW - timedelta(minutes=5)))

    assert usage.report("codex", "five_hour").used_percent == 60.0


def test_a_later_reset_replaces_the_entry_outright() -> None:
    usage = monitor()
    usage.record(observation(used_percent=95.0, status="rejected"))

    usage.record(
        observation(
            used_percent=1.0,
            resets_at=RESET + timedelta(hours=5),
            observed_at=NOW + timedelta(minutes=1),
        )
    )

    current = usage.report("codex", "five_hour")
    assert current.used_percent == 1.0
    assert current.status == "unknown"


def test_an_earlier_reset_is_discarded_however_recently_it_arrived() -> None:
    usage = monitor()
    usage.record(observation(used_percent=40.0))

    usage.record(
        observation(
            used_percent=99.0,
            resets_at=RESET - timedelta(minutes=30),
            observed_at=NOW + timedelta(minutes=10),
        )
    )

    assert usage.report("codex", "five_hour").used_percent == 40.0


def test_a_matching_reset_merges_conservatively() -> None:
    usage = monitor()
    usage.record(observation(used_percent=40.0, status="rejected"))

    usage.record(
        observation(
            used_percent=55.0,
            status="healthy",
            observed_at=NOW + timedelta(minutes=1),
        )
    )

    current = usage.report("codex", "five_hour")
    assert current.used_percent == 55.0
    assert current.status == "rejected"
    assert current.observed_at == NOW + timedelta(minutes=1)


def test_an_observation_without_a_reset_never_rewrites_a_live_expiry() -> None:
    usage = monitor()
    usage.record(observation(used_percent=40.0))

    usage.record(
        observation(
            used_percent=70.0,
            resets_at=None,
            observed_at=NOW + timedelta(minutes=1),
        )
    )

    current = usage.report("codex", "five_hour")
    assert current.resets_at == RESET
    assert current.used_percent == 70.0


def test_a_rejection_after_the_stored_window_closed_starts_a_fresh_entry() -> None:
    # Merging into an already-closed window would report the strongest signal
    # the monitor ever receives as `unknown`, so expiry is applied first.
    usage = monitor(now=RESET + timedelta(minutes=1))
    usage.record(observation(used_percent=40.0))

    usage.record(
        observation(
            used_percent=None,
            status="rejected",
            resets_at=None,
            observed_at=RESET + timedelta(minutes=1),
        )
    )

    current = usage.report("codex", "five_hour")
    assert current.status == "rejected"
    assert current.resets_at is None
    assert current.used_percent is None


def test_an_expired_window_reports_unknown() -> None:
    usage = monitor(now=RESET + timedelta(seconds=1))
    usage.record(observation(observed_at=RESET))

    assert usage.report("codex", "five_hour") is None


def test_a_stale_observation_reports_unknown() -> None:
    usage = monitor(now=NOW + timedelta(seconds=1801))
    usage.record(observation(resets_at=NOW + timedelta(days=2)))

    assert usage.report("codex", "five_hour") is None


def test_an_unobserved_window_reports_unknown() -> None:
    assert monitor().report("codex", "seven_day") is None


def test_windows_and_providers_never_share_an_entry() -> None:
    usage = monitor()
    usage.record(observation(used_percent=90.0, window="five_hour"))
    usage.record(observation(used_percent=10.0, window="seven_day"))
    usage.record(observation(used_percent=1.0, provider="claude"))

    assert usage.report("codex", "five_hour").used_percent == 90.0
    assert usage.report("codex", "seven_day").used_percent == 10.0
    assert usage.report("claude", "five_hour").used_percent == 1.0


@pytest.mark.parametrize(
    ("window", "used_percent", "expected"),
    [
        # modifications.md:4 states the thresholds as remaining capacity, so
        # exactly 20/8/3 remaining is not "less than" and must not fire.
        ("five_hour", 80.0, "ok"),
        ("five_hour", 80.1, "warning"),
        ("five_hour", 92.0, "warning"),
        ("five_hour", 92.1, "pause"),
        ("seven_day", 97.0, "ok"),
        ("seven_day", 97.1, "pause"),
        # The 20% warning is a five-hour rule; the weekly window only pauses.
        ("seven_day", 85.0, "ok"),
    ],
)
def test_percentage_thresholds_fire_strictly_below_the_remaining_limit(
    window: str, used_percent: float, expected: str
) -> None:
    assert quota_verdict(
        observation(window=window, used_percent=used_percent), settings=SETTINGS
    ) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [("rejected", "pause"), ("warning", "warning"), ("healthy", "ok"), ("unknown", "unknown")],
)
def test_a_provider_without_percentages_is_judged_on_status_alone(
    status: str, expected: str
) -> None:
    # Claude reports no percentage of any kind, so its policy is status-only.
    reported = observation(used_percent=None, status=status, provider="claude")

    assert quota_verdict(reported, settings=SETTINGS) == expected


def test_an_unknown_window_never_pauses() -> None:
    assert quota_verdict(None, settings=SETTINGS) == "unknown"
