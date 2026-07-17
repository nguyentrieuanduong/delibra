"""Provider-neutral command and normalized event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.models import SessionConfig


EventKind = Literal["init", "text_delta", "progress", "result", "warning", "error"]


@dataclass(frozen=True)
class AgentEvent:
    """Allowlisted normalized data only; provider payloads never enter runtime events."""

    kind: EventKind
    text: str = ""
    cli_session_id: str | None = None


@dataclass(frozen=True)
class RunContext:
    user_prompt: str
    resume_id: str | None
    resume_strategy: Literal["native", "stateless"]
    staged_history: list[Path]
    staged_source: Path | None


@dataclass(frozen=True)
class Command:
    argv: list[str]
    stdin: str


class AgentAdapter(Protocol):
    EFFORT_LEVELS: list[str]

    def build_command(self, config: SessionConfig, context: RunContext) -> Command: ...

    def parse_line(self, line: str) -> list[AgentEvent]: ...

    def final_text(self) -> str: ...
