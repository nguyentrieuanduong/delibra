"""Environment-driven application configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


MIB = 1024 * 1024


def _integer(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    home: Path
    run_timeout: int = 900
    max_run_timeout: int = 14_400
    stdout_line_limit: int = 12 * MIB
    captured_output_limit: int = 10 * MIB
    stderr_tail_limit: int = 8 * 1024
    replay_limit: int = 5 * MIB
    stateless_history_limit: int = 2 * MIB
    stateless_round_limit: int = 20
    request_body_limit: int = 2 * MIB
    file_view_limit: int = 512 * 1024
    auto_resume_drain_seconds: int = 30
    auto_turn_retries: int = 2
    auto_retry_backoff_seconds: int = 5
    # Quota thresholds are stated as *remaining* capacity, matching
    # modifications.md:4; policy compares them strictly.
    usage_warn_remaining_percent: int = 20
    usage_pause_remaining_percent: int = 8
    usage_weekly_pause_remaining_percent: int = 3
    # The badge's idle interval, and the tighter one it switches to while any
    # live window sits below the warning threshold.
    usage_poll_seconds: int = 1800
    usage_low_quota_poll_seconds: int = 300
    usage_staleness_seconds: int = 1800
    codex_rollout_scan_limit: int = 200
    codex_rollout_read_limit: int = 4 * MIB
    # Compaction reads every unretired round at once, and its summary is
    # mandatory in every later prompt, so both ends are bounded explicitly.
    compact_input_limit: int = 2 * MIB
    compact_output_limit: int = 256 * 1024
    # Auto compaction has its own budgets: it summarizes the Auto record's own
    # material into a prompt section, not a session's staged history.
    auto_compact_input_limit: int = 2 * MIB
    auto_compact_output_limit: int = 256 * 1024
    auto_compact_min_output: int = 4 * 1024
    auto_compact_max_failures: int = 3
    # A percentage of the Auto prompt's *byte* budget, never of a provider
    # token window; a per-run policy overrides it.
    auto_context_trigger_percent: int = 70

    def __post_init__(self) -> None:
        if self.max_run_timeout < self.run_timeout:
            raise ValueError(
                "DELIBRA_MAX_RUN_TIMEOUT must be at least DELIBRA_RUN_TIMEOUT"
            )
        if self.usage_pause_remaining_percent >= self.usage_warn_remaining_percent:
            raise ValueError(
                "DELIBRA_USAGE_PAUSE_REMAINING_PERCENT must be below "
                "DELIBRA_USAGE_WARN_REMAINING_PERCENT"
            )
        if self.usage_low_quota_poll_seconds > self.usage_poll_seconds:
            raise ValueError(
                "DELIBRA_USAGE_LOW_QUOTA_POLL_SECONDS must not exceed "
                "DELIBRA_USAGE_POLL_SECONDS"
            )

    @property
    def codex_home(self) -> Path:
        return self.home / "codex-home"

    @property
    def effective_compact_output_limit(self) -> int:
        """The largest summary that can still be staged into every later turn.

        The summary is charged before any round and is never dropped, so it may
        claim at most half the stateless budget. Clamped rather than validated
        on construction: lowering only ``stateless_history_limit`` is a
        reasonable thing to do, and it should not refuse to start.
        """

        return max(1, min(self.compact_output_limit, self.stateless_history_limit // 2))

    @property
    def effective_auto_compact_output_limit(self) -> int:
        """The largest Auto summary that can still be a mandatory prompt section.

        Clamped for the same reason as ``effective_compact_output_limit``: the
        cross-field invariant is real, but lowering only the prompt limit must
        not refuse to start the app. The per-attempt ``max_summary`` in 7e is
        tighter still, because it also charges topic, preparations and framing.
        """

        return max(
            1,
            min(self.auto_compact_output_limit, self.stateless_history_limit // 2),
        )

    @property
    def effective_auto_compact_min_output(self) -> int:
        """The smallest summary worth paying a provider for.

        Never above the budget it is compared against: that combination would
        skip every compaction while reporting "no headroom", blaming the run's
        content for a configuration mistake.
        """

        return min(
            self.auto_compact_min_output,
            self.effective_auto_compact_output_limit,
        )

    @property
    def effective_auto_compact_input_limit(self) -> int:
        """How much unretired material one compaction may read.

        Bounded by the prompt limit: reading more than a prompt could ever hold
        cannot help, since the summary must fit that same budget afterwards.
        """

        return max(
            1,
            min(self.auto_compact_input_limit, self.stateless_history_limit),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(
            os.environ.get("DELIBRA_HOME", str(Path.home() / ".delibra"))
        ).expanduser()
        return cls(
            home=home,
            run_timeout=_integer("DELIBRA_RUN_TIMEOUT", 900),
            max_run_timeout=_integer("DELIBRA_MAX_RUN_TIMEOUT", 14_400),
            stdout_line_limit=_integer("DELIBRA_STDOUT_LINE_LIMIT", 12 * MIB),
            captured_output_limit=_integer("DELIBRA_OUTPUT_LIMIT", 10 * MIB),
            stderr_tail_limit=_integer("DELIBRA_STDERR_TAIL_LIMIT", 8 * 1024),
            replay_limit=_integer("DELIBRA_REPLAY_LIMIT", 5 * MIB),
            stateless_history_limit=_integer(
                "DELIBRA_STATELESS_HISTORY_LIMIT", 2 * MIB
            ),
            stateless_round_limit=_integer("DELIBRA_STATELESS_ROUND_LIMIT", 20),
            request_body_limit=_integer("DELIBRA_REQUEST_BODY_LIMIT", 2 * MIB),
            file_view_limit=_integer("DELIBRA_FILE_VIEW_LIMIT", 512 * 1024),
            auto_resume_drain_seconds=_integer(
                "DELIBRA_AUTO_RESUME_DRAIN_SECONDS",
                30,
                minimum=1,
                maximum=300,
            ),
            # Zero disables retry, so this is the one setting whose minimum is 0.
            auto_turn_retries=_integer(
                "DELIBRA_AUTO_TURN_RETRIES",
                2,
                minimum=0,
                maximum=10,
            ),
            auto_retry_backoff_seconds=_integer(
                "DELIBRA_AUTO_RETRY_BACKOFF_SECONDS",
                5,
                minimum=1,
                maximum=300,
            ),
            usage_warn_remaining_percent=_integer(
                "DELIBRA_USAGE_WARN_REMAINING_PERCENT",
                20,
                minimum=0,
                maximum=100,
            ),
            usage_pause_remaining_percent=_integer(
                "DELIBRA_USAGE_PAUSE_REMAINING_PERCENT",
                8,
                minimum=0,
                maximum=100,
            ),
            usage_weekly_pause_remaining_percent=_integer(
                "DELIBRA_USAGE_WEEKLY_PAUSE_REMAINING_PERCENT",
                3,
                minimum=0,
                maximum=100,
            ),
            usage_poll_seconds=_integer(
                "DELIBRA_USAGE_POLL_SECONDS",
                1_800,
                minimum=10,
                maximum=3_600,
            ),
            usage_low_quota_poll_seconds=_integer(
                "DELIBRA_USAGE_LOW_QUOTA_POLL_SECONDS",
                300,
                minimum=10,
                maximum=3_600,
            ),
            usage_staleness_seconds=_integer(
                "DELIBRA_USAGE_STALENESS_SECONDS",
                1800,
                minimum=60,
                maximum=86_400,
            ),
            codex_rollout_scan_limit=_integer(
                "DELIBRA_CODEX_ROLLOUT_SCAN_LIMIT",
                200,
                minimum=1,
                maximum=5_000,
            ),
            codex_rollout_read_limit=_integer(
                "DELIBRA_CODEX_ROLLOUT_READ_LIMIT",
                4 * MIB,
                minimum=64 * 1024,
                maximum=64 * MIB,
            ),
            compact_input_limit=_integer(
                "DELIBRA_COMPACT_INPUT_LIMIT",
                2 * MIB,
                minimum=1,
                maximum=64 * MIB,
            ),
            compact_output_limit=_integer(
                "DELIBRA_COMPACT_OUTPUT_LIMIT",
                256 * 1024,
                minimum=1,
                maximum=64 * MIB,
            ),
            auto_compact_input_limit=_integer(
                "DELIBRA_AUTO_COMPACT_INPUT_LIMIT",
                2 * MIB,
                minimum=1,
                maximum=64 * MIB,
            ),
            auto_compact_output_limit=_integer(
                "DELIBRA_AUTO_COMPACT_OUTPUT_LIMIT",
                256 * 1024,
                minimum=1,
                maximum=64 * MIB,
            ),
            auto_compact_min_output=_integer(
                "DELIBRA_AUTO_COMPACT_MIN_OUTPUT",
                4 * 1024,
                minimum=1,
                maximum=64 * MIB,
            ),
            auto_compact_max_failures=_integer(
                "DELIBRA_AUTO_COMPACT_MAX_FAILURES",
                3,
                minimum=1,
                maximum=10,
            ),
            auto_context_trigger_percent=_integer(
                "DELIBRA_AUTO_CONTEXT_TRIGGER_PERCENT",
                70,
                minimum=10,
                maximum=95,
            ),
        )


settings = Settings.from_env()
