from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from app.agents.base import RunContext
from app.agents.codex import CodexAdapter
from app.models import SessionConfig


FIXTURES = Path(__file__).resolve().parents[1] / "spike" / "fixtures"


def config() -> SessionConfig:
    return SessionConfig(
        id="b" * 32,
        name="critic",
        agent="codex",
        model="gpt-5.4",
        effort="high",
        role_instructions="Begin with ROLE-CODEX-OK.",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )


def developer_instructions(command) -> str:
    values = [
        command.argv[index + 1]
        for index, token in enumerate(command.argv[:-1])
        if token == "--config"
        and command.argv[index + 1].startswith("developer_instructions=")
    ]
    assert len(values) == 1
    return tomllib.loads(values[0])["developer_instructions"]


def parse_fixture(name: str) -> tuple[CodexAdapter, list]:
    adapter = CodexAdapter(executable="/opt/homebrew/bin/codex")
    events = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        events.extend(adapter.parse_line(line))
    return adapter, events


def context(
    prompt: str,
    *,
    resume_id: str | None = None,
    strategy: str = "native",
    staged_shared_context: Path | None = None,
    writable_roots: tuple[Path, ...] = (),
) -> RunContext:
    return RunContext(
        user_prompt=prompt,
        resume_id=resume_id,
        resume_strategy=strategy,
        staged_history=[],
        staged_source=None,
        workspace=Path("/session/workspace"),
        staged_shared_context=staged_shared_context,
        writable_roots=writable_roots,
    )


@pytest.mark.parametrize(
    "strategy,resume_id",
    [("native", None), ("native", "thread"), ("stateless", None)],
)
def test_shared_context_is_present_in_every_codex_prompt_mode(
    strategy: str,
    resume_id: str | None,
) -> None:
    run_context = context(
        "Investigate.",
        strategy=strategy,
        resume_id=resume_id,
        staged_shared_context=Path("inputs/round-02/shared-context.md"),
    )
    command = CodexAdapter().build_command(config(), run_context)
    assert command.stdin.count("inputs/round-02/shared-context.md") == 1
    assert "standing requirements" in command.stdin


def test_fixture_extracts_thread_fixed_progress_and_final_message() -> None:
    adapter, events = parse_fixture("codex_first.jsonl")
    init = [event for event in events if event.kind == "init"]
    assert len(init) == 1
    assert init[0].cli_session_id == "00000000-0000-4000-8000-000000000002"

    assert not [event for event in events if event.kind == "text_delta"]
    progress = [event.text for event in events if event.kind == "progress"]
    assert "Running a command" in progress
    assert "Searching the web" in progress
    assert "Codex is working" in progress
    assert all("curl" not in label and "/" not in label for label in progress)

    results = [event for event in events if event.kind == "result"]
    assert len(results) == 1
    assert results[0].text == adapter.final_text()
    assert adapter.final_text().startswith("ROLE-CODEX-OK")


def test_error_and_turn_failed_fixture_are_terminal_errors() -> None:
    _, events = parse_fixture("codex_error.jsonl")
    errors = [event.text for event in events if event.kind == "error"]
    assert errors
    assert any("not supported" in message for message in errors)
    assert not [event for event in events if event.kind == "result"]


def test_non_json_reasoning_and_unknown_provider_payloads_are_safe() -> None:
    adapter = CodexAdapter()
    assert adapter.parse_line("  ") == []
    assert [(event.kind, event.text) for event in adapter.parse_line("bad-json")] == [
        ("error", "Codex emitted malformed JSONL")
    ]

    canary = "PRIVATE-CODEX-REASONING-DO-NOT-LEAK"
    assert adapter.parse_line(
        json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": canary}})
    ) == []
    assert canary not in repr(adapter)


def test_cumulative_agent_text_is_converted_to_append_only_deltas() -> None:
    adapter = CodexAdapter()
    first = adapter.parse_line(
        json.dumps(
            {"type": "item.updated", "item": {"type": "agent_message", "text": "Hello"}}
        )
    )
    second = adapter.parse_line(
        json.dumps(
            {
                "type": "item.updated",
                "item": {"type": "agent_message", "text": "Hello world"},
            }
        )
    )
    assert [event.text for event in [*first, *second]] == ["Hello", " world"]


def test_build_command_first_turn_matches_proven_spike_and_prepends_role() -> None:
    command = CodexAdapter(executable="codex").build_command(
        config(), context("Investigate this.")
    )
    assert command.argv == [
        "codex",
        "--model",
        "gpt-5.4",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--search",
        "--cd",
        "/session/workspace",
        "--config",
        'model_reasoning_effort="high"',
        "--config",
        "project_root_markers=[]",
        "--config",
        "project_doc_max_bytes=0",
        "--config",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "--config",
        "sandbox_workspace_write.exclude_tmpdir_env_var=false",
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        'shell_environment_policy.inherit="all"',
        "--config",
        'developer_instructions="Begin with ROLE-CODEX-OK."',
        "--disable",
        "hooks",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "memories",
        "--disable",
        "goals",
        "--disable",
        "multi_agent",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-",
    ]
    assert command.stdin == "Investigate this."


def test_build_command_native_resume_retains_policy() -> None:
    command = CodexAdapter(executable="codex").build_command(
        config(), context("Continue.", resume_id="thread-id")
    )
    exec_index = command.argv.index("exec")
    assert command.argv[exec_index:] == [
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "thread-id",
        "-",
    ]
    assert command.stdin == "Continue."
    assert "--sandbox" in command.argv[:exec_index]
    assert "--search" in command.argv[:exec_index]


@pytest.mark.parametrize("resume_id", [None, "thread-id"])
def test_codex_writable_roots_use_repeated_global_flags(
    resume_id: str | None,
) -> None:
    command = CodexAdapter(executable="codex").build_command(
        config(),
        context(
            "Investigate.",
            resume_id=resume_id,
            writable_roots=(Path("/project/zeta"), Path("/project/alpha")),
        ),
    )
    exec_index = command.argv.index("exec")

    assert [
        command.argv[index + 1]
        for index, value in enumerate(command.argv[:exec_index])
        if value == "--add-dir"
    ] == ["/project/alpha", "/project/zeta"]
    assert "--add-dir" not in command.argv[exec_index:]


@pytest.mark.parametrize(
    "strategy,resume_id",
    [("native", None), ("native", "thread-id"), ("stateless", None)],
)
def test_every_codex_strategy_carries_developer_role_once(
    strategy: str,
    resume_id: str | None,
) -> None:
    """The role is developer-authority argv, not a user-message duplicate.

    This deliberately overturns the old
    ``..._without_repeating_role`` contract. spike/FINDINGS.md:98 measured the
    role block *persisting* across a native resume on Codex 0.144.5, so the
    role was never lost -- this is about authority and recency, not repair.
    """

    adapter = CodexAdapter(executable="codex")
    command = adapter.build_command(
        config(),
        context("Go.", strategy=strategy, resume_id=resume_id),
    )

    assert developer_instructions(command) == "Begin with ROLE-CODEX-OK."
    assert "ROLE-CODEX-OK" not in command.stdin
    assert "<role_instructions>" not in command.stdin


def test_codex_role_override_is_toml_safe_and_one_argv_element() -> None:
    session = config()
    session.role_instructions = 'Quote "one", newline\nbackslash\\, DEL \x7f and ü.'

    command = CodexAdapter(executable="codex").build_command(session, context("Go."))

    assert developer_instructions(command) == session.role_instructions


def test_build_command_stateless_reapplies_role_history_and_source() -> None:
    run_context = context("Continue with evidence.", strategy="stateless")
    run_context = RunContext(
        user_prompt=run_context.user_prompt,
        resume_id=None,
        resume_strategy="stateless",
        staged_history=[
            Path("inputs/round-03/history/round-01.prompt.md"),
            Path("inputs/round-03/history/round-01.md"),
        ],
        staged_source=Path("inputs/round-03/source.md"),
        workspace=run_context.workspace,
    )
    command = CodexAdapter(executable="codex").build_command(config(), run_context)
    assert developer_instructions(command) == "Begin with ROLE-CODEX-OK."
    assert "<role_instructions>" not in command.stdin
    assert "round-01.prompt.md" in command.stdin
    assert "round-01.md" in command.stdin
    assert "inputs/round-03/source.md" in command.stdin
    assert command.stdin.endswith("Continue with evidence.")


def test_effort_levels_match_installed_codex() -> None:
    assert CodexAdapter.EFFORT_LEVELS == ["minimal", "low", "medium", "high", "xhigh"]


def test_native_resume_after_config_change_matches_real_m4_gate() -> None:
    assert CodexAdapter.RESUME_AFTER_CONFIG_CHANGE is True


def usage_events(name: str) -> tuple[list, list]:
    """Split one m5 fixture's events into billing and occupancy reports.

    The m5 fixtures append the app-owned rollout records after stdout, so the
    adapter sees lines it will never see in production too; parsing them must
    not manufacture a second reading.
    """

    _, events = parse_fixture(f"m5/{name}")
    return (
        [event for event in events if event.kind == "turn_usage"],
        [event for event in events if event.kind == "context_usage"],
    )


def test_codex_bills_a_round_from_turn_completed() -> None:
    billing, _ = usage_events("codex_c_small_resume.jsonl")

    assert len(billing) == 1
    usage = billing[0].usage
    # Measured, spike/fixtures/m5/codex_c_small_resume.jsonl.
    assert usage.input_tokens == 32018
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 31872
    assert usage.cache_creation_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.total_cost_usd is None


@pytest.mark.parametrize(
    "name",
    [
        "codex_a_small_fresh.jsonl",
        "codex_b_large_resume.jsonl",
        "codex_c_small_resume.jsonl",
        "codex_d_small_fresh.jsonl",
        "codex_e_model_change.jsonl",
    ],
)
def test_codex_stdout_occupancy_equals_the_rollout_last_token_usage(name: str) -> None:
    """`last_token_usage` is the field Phase 0 proved, and it lives only in the
    rollout. This asserts the stdout sum Delibra reads is the same number, so
    the `codex_last_token_usage` label stays honest before Phase 5b's rollout
    reader exists."""

    _, occupancy = usage_events(name)
    rollout_total = None
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "token_count":
            rollout_total = payload["info"]["last_token_usage"]["total_tokens"]

    assert rollout_total is not None
    assert occupancy[0].context.used_tokens == rollout_total
    assert occupancy[0].context.numerator_source == "codex_last_token_usage"


def test_codex_denominator_is_unknown_until_the_rollout_is_read() -> None:
    """`model_context_window` is absent from `exec --json` stdout; Phase 5b's
    rollout reader supplies it. Reporting `unknown` beats guessing."""

    _, occupancy = usage_events("codex_c_small_resume.jsonl")

    assert occupancy[0].context.context_window is None
    assert occupancy[0].context.resolved_model is None


def mutate_turn_completed(name: str, mutate) -> tuple[list, list]:
    adapter = CodexAdapter()
    events = []
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "turn.completed":
            mutate(payload)
            line = json.dumps(payload)
        events.extend(adapter.parse_line(line))
    return (
        [event for event in events if event.kind == "turn_usage"],
        [event for event in events if event.kind == "context_usage"],
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("usage"),
        lambda payload: payload.update({"usage": {"input_tokens": "many"}}),
        lambda payload: payload.update({"usage": {"input_tokens": -3}}),
        lambda payload: payload.update({"usage": {"output_tokens": 5}}),
    ],
)
def test_codex_occupancy_degrades_to_unknown_rather_than_guessing(mutate) -> None:
    """A partial sum is not occupancy: both components must be numeric."""

    _, occupancy = mutate_turn_completed("codex_c_small_resume.jsonl", mutate)

    for event in occupancy:
        assert event.context.used_tokens is None


def test_codex_reports_no_usage_when_the_provider_reported_none() -> None:
    billing, occupancy = mutate_turn_completed(
        "codex_c_small_resume.jsonl",
        lambda payload: payload.pop("usage"),
    )

    assert billing == []
