"""Editable prompt templates: validation, rendering, catalogue, storage and resolution."""

from __future__ import annotations

import pytest

from app.prompts import Placeholder, PromptError, PromptSpec, render_template, validate_template

SPEC = PromptSpec(
    id="demo", title="Demo prompt", group="Chat", used_in="tests",
    default="Read {context}. {rules}",
    placeholders=(
        Placeholder("context", "the staged file"),
        Placeholder("rules", "verdict rules", locked=True, sample="Say Converged."),
        Placeholder("extra", "optional note", required=False),
    ),
)


@pytest.mark.parametrize(
    "text, message",
    [
        ("   ", "Demo prompt must not be empty"),
        ("x" * 10_001, "Demo prompt must be at most 10,000 characters"),
        ("{context} {rules}\x00", "Demo prompt contains invalid characters"),
        ("{context} {rules} {contxt}", "Demo prompt has an unknown placeholder {contxt}"),
        ("{rules}", "Demo prompt must contain {context} exactly once"),
        ("{context} {context} {rules}", "Demo prompt must contain {context} exactly once"),
        ("{context} {rules} {extra}{extra}", "Demo prompt may contain {extra} at most once"),
    ],
)
def test_validation_messages(text: str, message: str) -> None:
    with pytest.raises(PromptError) as error:
        validate_template(SPEC, text)
    assert str(error.value) == message


def test_a_non_string_template_is_refused() -> None:
    with pytest.raises(PromptError, match="Demo prompt must not be empty"):
        validate_template(SPEC, 7)


def test_rendering_is_one_pass_and_keeps_other_braces() -> None:
    text = 'Keep {"json": 1}. Read {context}. {rules}'
    assert validate_template(SPEC, text) == text
    rendered = render_template(SPEC, text, {"context": "{rules}", "rules": "R", "extra": ""})
    assert rendered == 'Keep {"json": 1}. Read {rules}. R'


def test_rendering_needs_every_declared_value() -> None:
    with pytest.raises(KeyError):
        render_template(SPEC, SPEC.default, {"context": "c"})


def test_validation_on_generated_text_returns_it_or_raises_prompt_error() -> None:
    import random

    rng = random.Random(20261119)
    pieces = ["{context}", "{rules}", "{extra}", "{x}", "{", "}", "{{", "a", " ", "\x00", "{Context}", "é"]
    for _ in range(2000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        try:
            assert validate_template(SPEC, text) == text
        except PromptError:
            continue
        assert render_template(SPEC, text, {"context": "C", "rules": "R", "extra": ""}).count("R") >= 1


IDS = (
    "pass", "compact", "stateless_claude", "stateless_codex", "chat_skills", "auto_preparation",
    "auto_discussion", "auto_compaction", "build_turn", "docker_lima", "docker_local",
    "build_handover", "skills_turn", "guidance_gate", "disagreement", "questions_read",
    "questions_ask", "notes", "notes_reading_live", "notes_reading_copy", "downloads",
    "downloads_manual", "downloads_reread", "convert", "convert_reread", "model_check", "global_header",
)


def test_the_catalogue_has_every_prompt_once_and_valid_defaults() -> None:
    from app.prompts import CATALOGUE

    assert tuple(CATALOGUE) == IDS
    for spec in CATALOGUE.values():
        assert validate_template(spec, spec.default) == spec.default
        assert all(p.sample or not p.locked for p in spec.placeholders), spec.id

