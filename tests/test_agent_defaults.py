from __future__ import annotations

import json
import random

import pytest

from app.agent_defaults import (
    BUILT_IN_AGENT_DEFAULTS, AgentDefault, normalize_agent_model, read_agent_defaults, save_agent_defaults,
)


def test_without_a_saved_value_the_built_in_defaults_apply(tmp_path) -> None:
    assert read_agent_defaults(tmp_path) == BUILT_IN_AGENT_DEFAULTS
    assert BUILT_IN_AGENT_DEFAULTS == {"claude": AgentDefault("opus", "high"), "codex": AgentDefault("", "high")}


def test_saved_defaults_round_trip_and_keep_other_settings(tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"allowed_hosts": ["a.test"]}))
    save_agent_defaults(tmp_path, {"claude": AgentDefault("sonnet", "max"), "codex": AgentDefault("gpt-5.5", "low")})
    assert read_agent_defaults(tmp_path) == {"claude": AgentDefault("sonnet", "max"), "codex": AgentDefault("gpt-5.5", "low")}
    assert json.loads((tmp_path / "config.json").read_text())["allowed_hosts"] == ["a.test"]


@pytest.mark.parametrize("stored", [
    {"claude": {"model": "sonnet", "effort": "minimal"}},  # not a Claude effort
    {"claude": {"model": "-x", "effort": "high"}},
    {"claude": "sonnet"},
    "not an object",
])
def test_an_old_or_broken_saved_value_falls_back_per_type(tmp_path, stored) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"agent_defaults": stored}))
    assert read_agent_defaults(tmp_path)["claude"] == BUILT_IN_AGENT_DEFAULTS["claude"]


def test_saving_refuses_an_effort_the_agent_type_does_not_have(tmp_path) -> None:
    with pytest.raises(ValueError, match="Effort for codex must be one of"):
        save_agent_defaults(tmp_path, {"claude": AgentDefault("", "high"), "codex": AgentDefault("", "max")})
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("model", ["", "opus", "gpt-5.5-codex", "x" * 200])
def test_valid_models_including_the_cli_default(model) -> None:
    assert normalize_agent_model(model) == model


@pytest.mark.parametrize("model", [" opus", "a b", "-x", "x" * 201, "a\tb", "a\x00"])
def test_invalid_models_are_refused(model) -> None:
    with pytest.raises(ValueError, match="Model"):
        normalize_agent_model(model)


def test_any_text_is_either_a_safe_model_or_refused() -> None:
    rng = random.Random(11)
    alphabet = "ab-_.:/ \t\n\x00\x7fé5"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        try:
            model = normalize_agent_model(text)
        except ValueError:
            continue
        assert model == text and not model.startswith("-") and not any(ch.isspace() or ord(ch) < 32 for ch in model)
