"""Account-level quota state: one merged view per provider, account and window.

Delibra permits runs in different projects at once, so an older turn can finish
after a newer one. Every write therefore goes through a conservative merge that
cannot erase a warning or a pause, and every read applies expiry and staleness
so a closed window can never make Delibra believe it is still over limit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models import RateLimitObservation


# `rejected > warning > healthy > unknown`, the strength order the merge and the
# status-only providers both rank by.
_STATUS_STRENGTH = {"unknown": 0, "healthy": 1, "warning": 2, "rejected": 3}

_VERDICT_STRENGTH = {"unknown": 0, "ok": 1, "warning": 2, "pause": 3}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def merge_rate_limit(
    stored: RateLimitObservation | None, incoming: RateLimitObservation
) -> RateLimitObservation:
    """Combine two live observations of the same window, never losing a signal.

    The caller must have dropped an expired ``stored`` entry first: merging into
    a window that has already closed would preserve its reset instant and then
    report the strongest observation the monitor ever receives as ``unknown``.
    """

    if stored is None:
        return incoming
    if stored.key != incoming.key:
        raise ValueError("rate-limit observations of different windows cannot merge")
    if incoming.observed_at < stored.observed_at:
        return stored
    if stored.resets_at is not None and incoming.resets_at is not None:
        if incoming.resets_at > stored.resets_at:
            return incoming
        if incoming.resets_at < stored.resets_at:
            return stored
    percentages = [
        value
        for value in (stored.used_percent, incoming.used_percent)
        if value is not None
    ]
    strongest = max(
        (stored.status, incoming.status), key=lambda status: _STATUS_STRENGTH[status]
    )
    return RateLimitObservation(
        provider=incoming.provider,
        account_key=incoming.account_key,
        window=incoming.window,
        used_percent=max(percentages) if percentages else None,
        status=strongest,
        # An observation without a reset instant can raise the used figure or
        # strengthen the status, and can never extend or clear a live expiry.
        resets_at=stored.resets_at if incoming.resets_at is None else incoming.resets_at,
        observed_at=incoming.observed_at,
        source=incoming.source,
    )


def quota_verdict(
    observation: RateLimitObservation | None, *, settings: Settings
) -> str:
    """Grade one window as ``ok``, ``warning``, ``pause`` or ``unknown``.

    modifications.md:4 states both thresholds as *remaining* capacity, so the
    comparison is strict: exactly 20 % or 8 % remaining has not fallen below
    the limit and must not fire. The 20 % warning is a five-hour rule; the
    weekly window is stated with a pause threshold only.
    """

    if observation is None:
        return "unknown"
    status_verdict = {
        "rejected": "pause",
        "warning": "warning",
        "healthy": "ok",
        "unknown": "unknown",
    }[observation.status]

    remaining = observation.remaining_percent
    if remaining is None:
        return status_verdict
    if observation.window == "seven_day":
        percent_verdict = (
            "pause"
            if remaining < settings.usage_weekly_pause_remaining_percent
            else "ok"
        )
    elif remaining < settings.usage_pause_remaining_percent:
        percent_verdict = "pause"
    elif remaining < settings.usage_warn_remaining_percent:
        percent_verdict = "warning"
    else:
        percent_verdict = "ok"
    return max(
        (status_verdict, percent_verdict), key=lambda verdict: _VERDICT_STRENGTH[verdict]
    )


class UsageMonitor:
    """The single account-wide quota view, shared by the runner and Auto."""

    def __init__(
        self,
        *,
        settings: Settings,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._entries: dict[tuple[str, str, str], RateLimitObservation] = {}

    def record(self, observation: RateLimitObservation) -> None:
        self._entries[observation.key] = merge_rate_limit(
            self._live(self._entries.get(observation.key)), observation
        )

    def report(
        self, provider: str, window: str, account_key: str = "default"
    ) -> RateLimitObservation | None:
        """The current observation, or ``None`` when quota state is unknown."""

        return self._live(self._entries.get((provider, account_key, window)))

    def verdict(
        self, provider: str, window: str, account_key: str = "default"
    ) -> str:
        return quota_verdict(
            self.report(provider, window, account_key), settings=self._settings
        )

    def _live(
        self, observation: RateLimitObservation | None
    ) -> RateLimitObservation | None:
        if observation is None:
            return None
        now = self._clock()
        if observation.resets_at is not None and now > observation.resets_at:
            return None
        staleness = timedelta(seconds=self._settings.usage_staleness_seconds)
        if now - observation.observed_at > staleness:
            return None
        return observation
