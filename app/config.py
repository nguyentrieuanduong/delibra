"""Environment-driven application configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


MIB = 1024 * 1024


def _integer(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    home: Path
    run_timeout: int = 900
    stdout_line_limit: int = 12 * MIB
    captured_output_limit: int = 10 * MIB
    stderr_tail_limit: int = 8 * 1024
    replay_limit: int = 5 * MIB
    stateless_history_limit: int = 2 * MIB
    stateless_round_limit: int = 20
    request_body_limit: int = 2 * MIB

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
            stdout_line_limit=_integer("DELIBRA_STDOUT_LINE_LIMIT", 12 * MIB),
            captured_output_limit=_integer("DELIBRA_OUTPUT_LIMIT", 10 * MIB),
            stderr_tail_limit=_integer("DELIBRA_STDERR_TAIL_LIMIT", 8 * 1024),
            replay_limit=_integer("DELIBRA_REPLAY_LIMIT", 5 * MIB),
            stateless_history_limit=_integer(
                "DELIBRA_STATELESS_HISTORY_LIMIT", 2 * MIB
            ),
            stateless_round_limit=_integer("DELIBRA_STATELESS_ROUND_LIMIT", 20),
            request_body_limit=_integer("DELIBRA_REQUEST_BODY_LIMIT", 2 * MIB),
        )


settings = Settings.from_env()
