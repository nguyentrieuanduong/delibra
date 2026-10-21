from __future__ import annotations

from dataclasses import replace
import json
import tomllib
from pathlib import Path

import pytest

from app.agents.base import CodingContext, DockerAccess, RunContext, build_command_environment
from app.agents.codex import BUILD_DISABLED_FEATURES, CodexAdapter, codex_build_overrides
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
    billing, _ = usage_events("codex_d_small_fresh.jsonl")

    assert len(billing) == 1
    usage = billing[0].usage
    # Measured, spike/fixtures/m5/codex_d_small_fresh.jsonl (0.162.1). A fresh
    # thread: there, stdout's turn.completed is still exactly one round.
    assert usage.input_tokens == 12260
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 10496
    assert usage.cache_creation_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.total_cost_usd is None


@pytest.mark.parametrize(
    "name",
    [
        "codex_a_small_fresh.jsonl",
        "codex_d_small_fresh.jsonl",
        "codex_e_model_change.jsonl",
    ],
)
def test_codex_stdout_occupancy_equals_last_token_usage_on_a_fresh_thread(name: str) -> None:
    """On a fresh thread, the stdout sum Delibra reads equals the rollout's proven `last_token_usage`."""

    _, occupancy = usage_events(name)
    rollout_total = None
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "token_count":
            rollout_total = payload["info"]["last_token_usage"]["total_tokens"]

    assert rollout_total is not None
    assert occupancy[0].context.used_tokens == rollout_total
    assert occupancy[0].context.numerator_source == "codex_last_token_usage"


@pytest.mark.parametrize("name", ["codex_b_large_resume.jsonl", "codex_c_small_resume.jsonl"])
def test_codex_stdout_reports_the_thread_total_on_a_resumed_turn(name: str) -> None:
    """codex-cli 0.162.1: a resumed turn's stdout is `total_token_usage`, which
    is why the runner bills and sizes Codex rounds from the rollout segment
    (tests/test_codex_rollout.py)."""

    _, occupancy = usage_events(name)
    thread_total = None
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "token_count":
            thread_total = payload["info"]["total_token_usage"]["total_tokens"]

    assert thread_total is not None
    assert occupancy[0].context.used_tokens == thread_total


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


def coding(allowlist: tuple[str, ...] = ("pypi.org",)) -> CodingContext:
    return CodingContext(
        project_root=Path("/project"),
        temp_dir=Path("/home/.delibra/build-tmp/" + "b" * 32),
        env_dir=Path("/project/envs"),
        env_tool="venv",
        network_allowlist=allowlist,
    )


def build_context(allowlist: tuple[str, ...] = ("pypi.org",)) -> RunContext:
    return RunContext(
        user_prompt="Build it.",
        resume_id=None,
        resume_strategy="native",
        staged_history=[],
        staged_source=None,
        workspace=Path("/project/.delibra/sessions/Codex/workspace"),
        coding=coding(allowlist),
    )


def test_build_turn_uses_profile_not_legacy_sandbox() -> None:
    argv = CodexAdapter(executable="codex").build_command(config(), build_context()).argv
    assert "--sandbox" not in argv
    assert "--search" in argv  # research stays (D, Research tools)
    assert "plugins" in {argv[i + 1] for i, item in enumerate(argv) if item == "--disable"}
    assert not any("sandbox_workspace_write" in item for item in argv)
    assert argv[argv.index("--cd") + 1] == "/project"
    assert "--strict-config" in argv
    disabled = {argv[index + 1] for index, item in enumerate(argv) if item == "--disable"}
    assert set(BUILD_DISABLED_FEATURES) <= disabled
    assert BUILD_DISABLED_FEATURES == (
        "system_proxy_fallback",
        "computer_use",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "browser_annotation_api",
        "in_app_browser",
    )
    # Hosted search stays for research; it cannot reach this machine.
    assert "--search" in argv
    for override in codex_build_overrides(coding()):
        assert override in argv


def test_build_overrides_allow_only_listed_hosts_through_the_proxy() -> None:
    overrides = codex_build_overrides(coding(("pypi.org", "github.com")))
    assert 'default_permissions="delibra_build"' in overrides
    assert not any(item.startswith("web_search") for item in overrides)
    assert (
        "features.network_proxy={enabled=true, allow_local_binding=false, "
        "allow_upstream_proxy=false}"
    ) in overrides
    profile = next(item for item in overrides if item.startswith("permissions="))
    assert 'network={enabled=true, domains={"pypi.org"="allow", "github.com"="allow"}}' in profile
    assert '":root"="deny"' in profile
    assert '".delibra"="read"' in profile
    assert '".git"="read"' in profile
    command_env = next(item for item in overrides if item.startswith("shell_environment_policy="))
    assert '"PIP_CACHE_DIR"="/home/.delibra/build-tmp/' in command_env
    assert '"VIRTUAL_ENV"="/project/envs"' in command_env


def test_build_turn_never_resumes_even_with_a_resume_id() -> None:
    resumable = replace(build_context(), resume_id="thread-1")
    argv = CodexAdapter(executable="codex").build_command(config(), resumable).argv
    assert "resume" not in argv
    assert "thread-1" not in argv


def test_empty_allowlist_disables_command_network() -> None:
    overrides = codex_build_overrides(coding(()))
    profile = next(item for item in overrides if item.startswith("permissions="))
    assert "network={enabled=false}" in profile
    assert not any(item.startswith("features.network_proxy") for item in overrides)


SOCKET = "/home/.delibra/lima/delibra-0123456789/sock/docker.sock"


def docker_coding(allowlist: tuple[str, ...] = ("pypi.org",)) -> CodingContext:
    return replace(coding(allowlist), docker=DockerAccess(
        socket=Path(SOCKET),
        cli_read_roots=(Path("/Applications/Docker.app/Contents/Resources/bin"),),
    ))


def test_docker_profile_allows_exactly_the_project_socket() -> None:
    profile = next(
        item for item in codex_build_overrides(docker_coding()) if item.startswith("permissions=")
    )
    assert (
        'network={enabled=true, domains={"pypi.org"="allow"}, '
        f'unix_sockets={{"{SOCKET}"="allow"}}}}'
    ) in profile
    assert '"/Applications/Docker.app/Contents/Resources/bin"="read"' in profile
    assert "dangerously_allow_all_unix_sockets" not in profile


def test_docker_profile_with_an_empty_allowlist_proxies_only_the_socket() -> None:
    # Codex honours unix_sockets only through its network proxy (S4 and a
    # `codex sandbox` probe, 2026-10-21); with no domains the proxy refuses
    # every host (403), so command network stays off.
    overrides = codex_build_overrides(docker_coding(()))
    profile = next(item for item in overrides if item.startswith("permissions="))
    assert f'network={{enabled=true, domains={{}}, unix_sockets={{"{SOCKET}"="allow"}}}}' in profile
    assert (
        "features.network_proxy={enabled=true, allow_local_binding=false, "
        "allow_upstream_proxy=false}"
    ) in overrides
    without_docker = codex_build_overrides(coding(()))
    assert not any(item.startswith("features.network_proxy") for item in without_docker)


def test_docker_command_environment_points_the_cli_at_the_socket() -> None:
    environment = build_command_environment(docker_coding())
    assert environment["DOCKER_HOST"] == f"unix://{SOCKET}"
    assert environment["DOCKER_CONFIG"] == "/home/.delibra/build-tmp/" + "b" * 32 + "/docker"
    assert environment["DOCKER_CLI_HINTS"] == "false"
    assert not any(key.startswith("DOCKER") for key in build_command_environment(coding()))


def test_build_command_environment_fixes_git_and_pip_under_the_sandbox() -> None:
    # S1 (2026-10-15): with HOME unreadable, git dies on ~/.gitconfig (EPERM)
    # and pip's macOS trust-store lookup fails (OSStatus -26276); these keep
    # both working without widening the sandbox.
    environment = build_command_environment(coding())
    assert environment == {
        "VIRTUAL_ENV": "/project/envs",
        "TMPDIR": "/home/.delibra/build-tmp/" + "b" * 32,
        "PIP_CACHE_DIR": "/home/.delibra/build-tmp/" + "b" * 32 + "/pip-cache",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "XDG_CONFIG_HOME": "/home/.delibra/build-tmp/" + "b" * 32 + "/xdg",
        "PIP_USE_DEPRECATED": "legacy-certs",
    }
    command_env = next(
        item for item in codex_build_overrides(coding())
        if item.startswith("shell_environment_policy=")
    )
    for key, value in environment.items():
        assert f'"{key}"="{value}"' in command_env


def test_build_profile_grants_read_only_system_roots() -> None:
    # S2 (2026-10-15): /usr/bin/git is an xcrun shim; with `:root` denied it
    # cannot find the developer tools unless their directory is readable.
    tools = Path("/Library/Developer/CommandLineTools")
    context = CodingContext(
        project_root=Path("/project"),
        temp_dir=Path("/home/.delibra/build-tmp/" + "b" * 32),
        env_dir=Path("/project/envs"),
        env_tool="venv",
        network_allowlist=("pypi.org",),
        read_roots=(tools,),
    )
    profile = next(item for item in codex_build_overrides(context) if item.startswith("permissions="))
    assert '"/Library/Developer/CommandLineTools"="read"' in profile
    assert '"/Library/Developer/CommandLineTools"="write"' not in profile
