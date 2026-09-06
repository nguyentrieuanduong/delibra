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

    def __post_init__(self) -> None:
        if self.max_run_timeout < self.run_timeout:
            raise ValueError(
                "DELIBRA_MAX_RUN_TIMEOUT must be at least DELIBRA_RUN_TIMEOUT"
            )

    @property
    def codex_home(self) -> Path:
        return self.home / "codex-home"

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
        )


settings = Settings.from_env()
