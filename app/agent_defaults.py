"""Default model and effort per agent type for new agents (user decision 2026-11-18).

Stored in $DELIBRA_HOME/config.json under ``agent_defaults``; a missing, old or
broken entry falls back to the built-in default of that type. An empty model
means "the CLI's own default": Delibra then passes no model option.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.config import read_config_file, write_config_file

CONFIG_KEY = "agent_defaults"
MODEL_MAX_CHARS = 200
EFFORT_LEVELS: Mapping[str, list[str]] = {
    "claude": ClaudeAdapter.EFFORT_LEVELS,
    "codex": CodexAdapter.EFFORT_LEVELS,
}


@dataclass(frozen=True)
class AgentDefault:
    model: str
    effort: str


# The one place to change the built-in defaults.
BUILT_IN_AGENT_DEFAULTS: Mapping[str, AgentDefault] = {
    "claude": AgentDefault("opus", "high"),
    "codex": AgentDefault("", "high"),
}


def normalize_agent_model(model: str) -> str:
    """An agent's model: empty (the CLI's default) or one option-safe token."""

    if len(model) > MODEL_MAX_CHARS:
        raise ValueError(f"Model must be at most {MODEL_MAX_CHARS} characters")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in model):
        raise ValueError("Model contains invalid characters")
    if model.startswith("-"):
        raise ValueError("Model must not start with '-'")
    return model


def typed_model(typed: str) -> str:
    """A model as typed in a form: blank is refused, otherwise trimmed and checked;
    empty means the CLI default. Agent forms and Settings → Agents share this."""

    if typed and not typed.strip():
        raise ValueError("Model must be empty or a model name, not blank")
    return normalize_agent_model(typed.strip())


def checked_default(agent: str, model: str, effort: str) -> AgentDefault:
    if agent not in EFFORT_LEVELS:
        raise ValueError(f"Unknown agent type {agent}")
    if effort not in EFFORT_LEVELS[agent]:
        raise ValueError(f"Effort for {agent} must be one of: {', '.join(EFFORT_LEVELS[agent])}")
    return AgentDefault(normalize_agent_model(model), effort)


def read_agent_defaults(home: Path) -> dict[str, AgentDefault]:
    """Each type's saved default, or its built-in one when missing or invalid."""

    try:
        stored = read_config_file(home).get(CONFIG_KEY, {})
    except ValueError:
        stored = {}
    defaults = dict(BUILT_IN_AGENT_DEFAULTS)
    if not isinstance(stored, dict):
        return defaults
    for agent, entry in stored.items():
        if not isinstance(entry, dict):
            continue
        try:
            defaults[agent] = checked_default(agent, str(entry.get("model", "")), str(entry.get("effort", "")))
        except ValueError:
            continue
    return defaults


def save_agent_defaults(home: Path, defaults: Mapping[str, AgentDefault]) -> None:
    """Validate every type, then replace the config.json entry atomically."""

    checked = {agent: checked_default(agent, item.model, item.effort) for agent, item in defaults.items()}
    data = read_config_file(home)
    data[CONFIG_KEY] = {agent: asdict(item) for agent, item in checked.items()}
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_config_file(home, data)


def defaults_json(defaults: Mapping[str, AgentDefault]) -> str:
    """For the create forms' Agent select (app.js applyAgentDefaults)."""

    return json.dumps({agent: asdict(item) for agent, item in defaults.items()})
