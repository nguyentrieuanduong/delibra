"""Provider-neutral command and normalized event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.agents.errors import ProviderErrorInfo
from app.models import ContextReading, RateLimitReading, SessionConfig, TurnUsage


EventKind = Literal[
    "init",
    "text_delta",
    "progress",
    "result",
    "warning",
    "error",
    "turn_usage",
    "context_usage",
    "rate_limit",
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
    # Set on "turn_usage", "context_usage" and "rate_limit" events. All three
    # are typed records of allowlisted scalars, so none reintroduces a provider
    # payload.
    usage: TurnUsage | None = None
    context: ContextReading | None = None
    rate_limit: RateLimitReading | None = None


@dataclass(frozen=True)
class DockerAccess:
    """Build-turn Docker: the one socket the sandbox allows, and the CLI's files."""

    socket: Path
    # Real directories of the docker binary and its plugins, granted read-only.
    cli_read_roots: tuple[Path, ...] = ()
    # Real directories holding docker-compose/docker-buildx (cliPluginsExtraDirs).
    plugin_dirs: tuple[Path, ...] = ()
    # Real directory of the docker binary, appended to PATH: Codex cannot read
    # /usr/local/bin, where Docker Desktop links it (spike S4, 2026-10-21).
    bin_dir: Path | None = None


@dataclass(frozen=True)
class CodingContext:
    """Build-turn capabilities. Present only on Auto build turns."""

    project_root: Path
    temp_dir: Path
    env_dir: Path
    env_tool: Literal["conda", "venv", "existing"]
    network_allowlist: tuple[str, ...]
    # System directories commands must read, granted read-only. Codex denies
    # `:root` reads, which hides the macOS developer tools that /usr/bin/git
    # shims to; Claude's sandbox already reads outside HOME.
    read_roots: tuple[Path, ...] = ()
    # Auto build runs with Docker only (design §6).
    docker: DockerAccess | None = None


def build_command_environment(coding: CodingContext) -> dict[str, str]:
    """Variables every build-turn command gets, for both providers.

    PATH is not here: the runner prepends ``envs/bin`` to the inherited one.
    The git and pip entries come from spike S1: with HOME unreadable, git
    aborts on ``~/.gitconfig`` (EPERM rather than EACCES) and pip's macOS
    trust-store lookup fails, while pip's bundled CA list works.
    """

    temp = coding.temp_dir.as_posix()
    prefix = "CONDA_PREFIX" if coding.env_tool == "conda" else "VIRTUAL_ENV"
    environment = {
        prefix: coding.env_dir.as_posix(),
        "TMPDIR": temp,
        "PIP_CACHE_DIR": f"{temp}/pip-cache",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "XDG_CONFIG_HOME": f"{temp}/xdg",
        "PIP_USE_DEPRECATED": "legacy-certs",
    }
    if coding.docker is not None:
        # HOME is unreadable in the sandbox, so the CLI's config lives in the
        # turn's temp dir, written by the runner.
        environment["DOCKER_HOST"] = f"unix://{coding.docker.socket.as_posix()}"
        environment["DOCKER_CONFIG"] = f"{temp}/docker"
        environment["DOCKER_CLI_HINTS"] = "false"
    return environment


@dataclass(frozen=True)
class RunContext:
    user_prompt: str
    resume_id: str | None
    resume_strategy: Literal["native", "stateless"]
    staged_history: list[Path]
    staged_source: Path | None
    workspace: Path = Path(".")
    staged_shared_context: Path | None = None
    writable_roots: tuple[Path, ...] = ()
    # Registered project directory, when it is safe to express in a provider
    # permission rule. None keeps reads confined to the workspace.
    project_root: Path | None = None
    # Set only on Auto build turns; None keeps chat and discussion argv
    # byte-identical to before coding mode existed.
    coding: CodingContext | None = None


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
