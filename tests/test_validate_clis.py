"""Tier 1 logic of scripts/validate_clis.py against recorded help texts (no CLI runs)."""

from __future__ import annotations

from pathlib import Path
import random

import pytest

from app.agents.base import RunContext
from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import SessionConfig
import scripts.validate_clis as v

HELP = Path(__file__).with_name("fixtures") / "cli_help"


def text(name: str) -> str:
    return (HELP / name).read_text(encoding="utf-8")


CLAUDE = v.parse_help(text("claude-2.1.322-help.txt"))
CODEX_SCOPES = {
    "codex": v.parse_help(text("codex-0.169.0-help.txt")),
    "codex exec": v.parse_help(text("codex-0.169.0-exec-help.txt")),
    "codex exec resume": v.parse_help(text("codex-0.169.0-exec-resume-help.txt")),
}


def session(agent: str) -> SessionConfig:
    return SessionConfig(
        id="c" * 32, name="Check", agent=agent, model="m", effort="low",
        role_instructions="Be brief.", cli_session_id=None, status="idle",
        created_at="2026-11-19T00:00:00Z",
    )


def chat_context(resume: str | None = None) -> RunContext:
    return RunContext(
        user_prompt="Hi", resume_id=resume, resume_strategy="native", staged_history=[],
        staged_source=None, workspace=Path("/w/workspace"), writable_roots=(Path("/w/out"),),
        project_root=Path("/w/project"),
    )


def test_commander_help_gives_names_arity_and_choices() -> None:
    assert CLAUDE["--add-dir"].arity == "many"
    assert CLAUDE["--allowedTools"] == CLAUDE["--allowed-tools"]
    assert CLAUDE["--resume"].arity == "optional" and CLAUDE["-r"] == CLAUDE["--resume"]
    assert CLAUDE["-p"].arity == "none"
    assert CLAUDE["--output-format"].choices == ("text", "json", "stream-json")
    assert "dontAsk" in CLAUDE["--permission-mode"].choices
    assert CLAUDE["--permission-prompts"].choices == ("host", "none")
    assert v.listed_levels(CLAUDE["--effort"]) == ("low", "medium", "high", "xhigh", "max")


def test_clap_help_gives_names_arity_and_both_choice_styles() -> None:
    top = CODEX_SCOPES["codex"]
    assert top["-c"].arity == "one" and top["--config"] == top["-c"]
    assert top["--image"].arity == "many"
    assert top["--strict-config"].arity == "none"
    assert top["--sandbox"].choices == ("read-only", "workspace-write", "danger-full-access")
    assert top["--ask-for-approval"].choices == ("on-request", "never")
    assert CODEX_SCOPES["codex exec"]["--color"].choices == ("always", "never", "auto")
    assert v.parse_subcommands(text("codex-0.169.0-help.txt")) >= {"exec", "sandbox", "app-server", "features"}


def test_delibra_chat_argv_passes_and_an_unknown_flag_fails() -> None:
    argv = ClaudeAdapter().build_command(session("claude"), chat_context("abc")).argv
    found = v.check_argv("claude", argv, {"claude": CLAUDE})
    assert found and all(item.problem is None for item in found)
    assert {item.flag for item in found} >= {"--append-system-prompt", "--resume", "--safe-mode"}

    bad = v.check_argv("claude", ["claude", "-p", "--bogus-flag", "--permission-mode", "wild"], {"claude": CLAUDE})
    problems = {item.flag: item.problem for item in bad if item.problem}
    assert problems == {
        "--bogus-flag": "not an option of `claude`",
        "--permission-mode": "value 'wild' is not one of acceptEdits, auto, bypassPermissions, manual, dontAsk, plan",
    }


def test_codex_options_are_checked_in_their_own_subcommand_scope() -> None:
    argv = CodexAdapter().build_command(session("codex"), chat_context("t-1")).argv
    found = v.check_argv("codex", argv, CODEX_SCOPES)
    assert found and all(item.problem is None for item in found)
    scopes = {(item.scope, item.flag) for item in found}
    assert ("codex", "--sandbox") in scopes and ("codex exec resume", "--json") in scopes

    misplaced = v.check_argv("codex", ["codex", "--json", "exec", "--search", "-"], CODEX_SCOPES)
    assert [(item.scope, item.flag, item.problem) for item in misplaced] == [
        ("codex", "--json", "not an option of `codex`"),
        ("codex exec", "--search", "not an option of `codex exec`"),
    ]


def test_config_overrides_are_listed_by_key() -> None:
    argv = ["codex", "-c", 'a.b="x=y"', "--config", "c={d=1}", "exec", "-"]
    assert v.config_keys(argv) == ["a.b", "c"]


def test_feature_stages_gate_the_disabled_features() -> None:
    table = v.parse_features(text("codex-0.169.0-features-list.txt"))
    assert table["multi_agent"] == ("deprecated", True)
    assert table["guardianv2.thread_context"] == ("removed", False)
    checks = {c.name: c for c in v.check_features("codex", ["hooks", "multi_agent", "nope", "search_tool"], table)}
    assert checks["feature hooks"].status == "PASS"
    assert (checks["feature multi_agent"].status, checks["feature multi_agent"].detail) == ("PASS", "deprecated")
    assert checks["feature nope"].status == "FAIL"
    assert checks["feature search_tool"].status == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "returncode, output, status",
    [
        (1, "Error loading config.toml: unknown configuration field `zzzz_delibra_probe` in -c/--config override",
         "PASS"),
        (1, "Error loading config.toml: unknown configuration field `project_doc_max_bytes` in -c/--config override",
         "FAIL"),
        (2, "error: unexpected argument '--bogus' found", "FAIL"),
        (0, "", "INCONCLUSIVE"),
    ],
)
def test_dry_parse_passes_only_when_the_probe_key_is_the_first_unknown_field(
    returncode: int, output: str, status: str
) -> None:
    assert v.dry_parse_status(returncode, output)[0] == status


def test_effort_levels_missing_from_every_model_warn() -> None:
    catalog = {"models": [
        {"slug": "a", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "b", "visibility": "hide", "supported_reasoning_levels": [{"effort": "minimal"}]},
    ]}
    check = v.codex_effort_check(["minimal", "low", "high"], catalog)
    assert (check.status, check.detail) == ("WARN", "no listed model supports: minimal")


def test_verdict_and_table() -> None:
    checks = [
        v.Check("claude", "flag --model", "PASS"),
        v.Check("claude", "settings keys", "UNCHECKED", "Tier 2 sandbox turn"),
        v.Check("codex", "dry parse", "INCONCLUSIVE", "x | y"),
        v.Check("codex", "effort", "WARN", "minimal"),
    ]
    assert v.verdict(checks, "claude") == "PASS"
    assert v.verdict(checks, "codex") == "INCONCLUSIVE"
    assert v.verdict([*checks, v.Check("codex", "z", "FAIL")], "codex") == "FAIL"
    table = v.render_table(checks)
    assert table.splitlines()[0] == "| Provider | Check | Status | Detail |"
    assert "| codex | dry parse | INCONCLUSIVE | x \\| y |" in table


def test_help_parsing_never_raises_on_mangled_help() -> None:
    rng = random.Random(20261119)
    lines = text("claude-2.1.322-help.txt").splitlines() + text("codex-0.169.0-help.txt").splitlines()
    alphabet = " -<>[]().,\"'=|abcXYZ\t"
    for _ in range(300):
        sample = rng.sample(lines, k=rng.randint(0, 40))
        mangled = [
            "".join(rng.choice(alphabet) if rng.random() < 0.05 else ch for ch in line) for line in sample
        ]
        options = v.parse_help("\n".join(mangled))
        assert all(name.startswith("-") for name in options)
        assert all(spec.arity in ("none", "one", "optional", "many") for spec in options.values())
