"""Provider-neutral command and normalized event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.agents.errors import ProviderErrorInfo
from app.models import ContextReading, SessionConfig, TurnUsage


EventKind = Literal[
    "init",
    "text_delta",
    "progress",
    "result",
    "warning",
    "error",
    "turn_usage",
    "context_usage",
]


@dataclass(frozen=True)
class AgentEvent:
    """Allowlisted normalized data only; provider payloads never enter runtime events."""

    kind: EventKind
    text: str = ""
    cli_session_id: str | None = None
    # Set by adapters on "error" events so the runner can classify a failure from
    # provider structure rather than from prose. ProviderErrorInfo carries only
    # normalized scalars, preserving this class's contract.
    error_info: ProviderErrorInfo | None = None
    # Set on "turn_usage" and "context_usage" events. Both are typed records of
    # allowlisted numbers, so neither reintroduces a provider payload.
    usage: TurnUsage | None = None
    context: ContextReading | None = None


@dataclass(frozen=True)
class RunContext:
    user_prompt: str
    resume_id: str | None
    resume_strategy: Literal["native", "stateless"]
    staged_history: list[Path]
    staged_source: Path | None
    workspace: Path = Path(".")
    staged_shared_context: Path | None = None


def shared_context_section(context: RunContext) -> str:
    path = context.staged_shared_context
    if path is None:
        return ""
    return (
        "Project shared context is staged at:\n"
        f"{path.as_posix()}\n"
        "Read it before answering. Treat it as project background and standing "
        "requirements. Your role instructions still define your role; the current "
        "user prompt may be more specific for this turn.\n\n"
    )


@dataclass(frozen=True)
class Command:
    argv: list[str]
    stdin: str


class AgentAdapter(Protocol):
    EFFORT_LEVELS: list[str]
    RESUME_AFTER_CONFIG_CHANGE: bool

    def build_command(self, config: SessionConfig, context: RunContext) -> Command: ...

    def parse_line(self, line: str) -> list[AgentEvent]: ...

    def final_text(self) -> str: ...
