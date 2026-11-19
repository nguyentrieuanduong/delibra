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


def test_unexpected_positionals_fail() -> None:
    claude = v.check_argv("claude", ["claude", "-p", "stray"], {"claude": CLAUDE})
    assert [(u.flag, u.problem) for u in claude] == [("-p", None), ("stray", "unexpected argument `stray` for `claude`")]
    ok = v.check_argv("codex", ["codex", "exec", "resume", "t-1", "-"], CODEX_SCOPES)
    assert ok == []
    bad = v.check_argv("codex", ["codex", "exec", "resume", "t-1", "-", "extra"], CODEX_SCOPES)
    assert [(u.scope, u.flag, u.problem) for u in bad] == [
        ("codex exec resume", "extra", "unexpected argument `extra` for `codex exec resume`")]
    assert [u.flag for u in v.check_argv("codex", ["codex", "exec", "hello"], CODEX_SCOPES)] == ["hello"]


def test_the_cheapest_codex_model_is_the_listed_affordable_one() -> None:
    catalog = {"models": [
        {"slug": "big", "visibility": "list", "priority": 1, "description": "Frontier intelligence."},
        {"slug": "small-old", "visibility": "list", "priority": 9, "description": "Older fast and efficient model."},
        {"slug": "small", "visibility": "list", "priority": 4, "description": "Fast and affordable model."},
        {"slug": "hidden", "visibility": "hide", "priority": 0, "description": "Cheap and affordable."},
    ]}
    assert v.cheapest_codex_model(catalog) == "small"
    assert v.cheapest_codex_model({"models": [catalog["models"][1]]}) == "small-old"
    assert v.cheapest_codex_model({"models": [catalog["models"][0]]}) is None
    # Whole words only: "unaffordable" and "fastest" are not "affordable" and "fast".
    near = [{"slug": "x", "visibility": "list", "priority": 1, "description": "Unaffordable, fastest."}]
    assert v.cheapest_codex_model({"models": near}) is None


@pytest.mark.parametrize("value, status", [
    ("PASS", "PASS"), ("FAIL (rc=1)", "FAIL"), ("INCONCLUSIVE (control unreachable)", "INCONCLUSIVE"),
    ("RECORDED (rc=22)", "RECORDED"), ("weird output", "INCONCLUSIVE"), ("PASSED?", "INCONCLUSIVE"),
])
def test_spike_rows_are_never_pass_unless_the_spike_said_pass(value: str, status: str) -> None:
    assert v._spike_row("codex", "sandbox x", value).status == status


@pytest.mark.parametrize("text, status", [
    ("4817 TAG-BRAVO", "PASS"), ("4817 TAG-ALPHA", "KNOWN-ISSUE"), ("4817", "INCONCLUSIVE"),
])
def test_the_resume_system_prompt_check_has_three_outcomes(text: str, status: str) -> None:
    from app.agents.base import AgentEvent

    turn = v.Turn([AgentEvent("init", cli_session_id="s"), AgentEvent("result", text)], 0, "", [])
    checks = {c.name: c.status for c in v._resume_checks("codex", turn, "s")}
    assert checks["resume: changed system prompt applies"] == status
    assert checks["resume: same session"] == "PASS"


def test_rollout_developer_messages_are_read_per_thread(tmp_path: Path) -> None:
    import json

    folder = tmp_path / "sessions" / "2026" / "11" / "19"
    folder.mkdir(parents=True)
    records = [
        {"type": "response_item", "payload": {"role": "developer", "content": [{"text": "tag TAG-ALPHA"}]}},
        {"type": "response_item", "payload": {"role": "user", "content": [{"text": "TAG-BRAVO?"}]}},
    ]
    (folder / "rollout-2026-11-19T00-00-00-t-1.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    assert v.rollout_developer_tags(tmp_path, "t-1", ("TAG-ALPHA", "TAG-BRAVO")) == {"TAG-ALPHA": 1, "TAG-BRAVO": 0}


def test_the_lan_override_restores_the_spike_settings() -> None:
    import spike.spike_build_sandbox as spike

    before = (spike.LAN_IP, spike.LAN_PORT, spike.LAN_NAME, spike.ALLOWLIST)
    with v.lan_override(spike) as hits:
        assert spike.LAN_IP != "" and spike.LAN_NAME in spike.ALLOWLIST and hits == []
    assert (spike.LAN_IP, spike.LAN_PORT, spike.LAN_NAME, spike.ALLOWLIST) == before


def test_only_the_last_three_runs_are_kept(tmp_path: Path) -> None:
    for name in ("tier1-20261101T000000", "tier2-20261102T000000", "tier1-20261103T000000",
                 "tier1-20261104T000000", "other"):
        (tmp_path / name).mkdir()
    v.prune_runs(tmp_path, keep=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "other", "tier1-20261103T000000", "tier1-20261104T000000", "tier2-20261102T000000"]


def test_the_resume_known_issue_does_not_gate() -> None:
    checks = [v.Check("codex", "flags", "PASS"),
              v.Check("codex", "resume: changed system prompt applies", "KNOWN-ISSUE", "TAG-ALPHA")]
    assert v.verdict(checks, "codex") == "PASS"


def test_the_codex_tier2_model_is_never_a_silent_account_default() -> None:
    affordable = {"models": [{"slug": "small", "visibility": "list", "priority": 2,
                              "description": "Fast and affordable."}]}
    assert v.resolve_codex_model("chosen", lambda: pytest.fail("no catalog needed")) == "chosen"
    assert v.resolve_codex_model(None, lambda: affordable) == "small"

    def broken():
        raise ValueError("Expecting value")

    with pytest.raises(v.ModelChoiceError, match="could not read `codex debug models --bundled`"):
        v.resolve_codex_model(None, broken)
    with pytest.raises(v.ModelChoiceError, match="no listed Codex model is described as affordable, fast or efficient"):
        v.resolve_codex_model(None, lambda: {"models": []})
