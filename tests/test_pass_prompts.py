import pytest

from app.pass_prompts import (
    PassPromptTemplateError,
    render_pass_prompt,
    validate_pass_prompt_template,
)


@pytest.mark.parametrize(
    "value",
    [None, 7, "", " \n ", "no source token", "{source_path} twice {source_path}", "x\x00{source_path}"],
)
def test_pass_prompt_template_rejects_invalid_values(value: object) -> None:
    with pytest.raises(PassPromptTemplateError):
        validate_pass_prompt_template(value)


def test_pass_prompt_template_enforces_10000_character_limit() -> None:
    assert len(validate_pass_prompt_template("x" * 9_987 + "{source_path}")) == 10_000
    with pytest.raises(PassPromptTemplateError, match="10,000"):
        validate_pass_prompt_template("x" * 9_988 + "{source_path}")


def test_render_pass_prompt_replaces_supported_tokens_once_and_keeps_unknown_braces() -> None:
    rendered = render_pass_prompt(
        "{source_session}|{source_session}|{source_round}|{unknown}|{source_path}",
        source_path="inputs/round-03/source.md",
        source_session="literal {source_round}",
        source_round=7,
    )
    assert rendered == (
        "literal {source_round}|literal {source_round}|7|{unknown}|"
        "inputs/round-03/source.md"
    )


def test_render_pass_prompt_rejects_duplicate_rendered_path_and_oversize_result() -> None:
    path = "inputs/round-03/source.md"
    with pytest.raises(PassPromptTemplateError, match="exactly once"):
        render_pass_prompt(
            "{source_session}\n{source_path}",
            source_path=path,
            source_session=path,
            source_round=1,
        )
    with pytest.raises(PassPromptTemplateError, match="100,000"):
        render_pass_prompt(
            "{source_session}" * 624 + "{source_path}",
            source_path=path,
            source_session="s" * 200,
            source_round=1,
        )
