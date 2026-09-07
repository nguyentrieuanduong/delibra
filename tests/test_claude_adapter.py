from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.agents.base import RunContext
from app.agents.claude import ClaudeAdapter
from app.models import SessionConfig


FIXTURES = Path(__file__).resolve().parents[1] / "spike" / "fixtures"


def config() -> SessionConfig:
    return SessionConfig(
        id="a" * 32,
        name="researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="Begin with ROLE-CLAUDE-OK.",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )


def parse_fixture(name: str) -> tuple[ClaudeAdapter, list]:
    adapter = ClaudeAdapter(executable="/opt/homebrew/bin/claude")
    events = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        events.extend(adapter.parse_line(line))
    return adapter, events


@pytest.mark.parametrize(
    "strategy,resume_id",
    [("native", None), ("native", "thread"), ("stateless", None)],
)
def test_shared_context_is_present_in_every_claude_prompt_mode(
    strategy: str,
    resume_id: str | None,
) -> None:
    run_context = RunContext(
        user_prompt="Investigate.",
        resume_id=resume_id,
        resume_strategy=strategy,
        staged_history=[],
        staged_source=None,
        workspace=Path("/session/workspace"),
        staged_shared_context=Path("inputs/round-02/shared-context.md"),
    )
    command = ClaudeAdapter().build_command(config(), run_context)
    assert command.stdin.count("inputs/round-02/shared-context.md") == 1
    assert "standing requirements" in command.stdin


def test_fixture_extracts_session_deltas_progress_and_final_without_duplication() -> None:
    adapter, events = parse_fixture("claude_first.jsonl")

    init = [event for event in events if event.kind == "init"]
    assert len(init) == 1
    assert init[0].cli_session_id == "00000000-0000-4000-8000-000000000001"

    deltas = [event.text for event in events if event.kind == "text_delta"]
    assert deltas
    assert "".join(deltas) == adapter.final_text()
    assert all(delta for delta in deltas)

    progress = {event.text for event in events if event.kind == "progress"}
    assert "Searching the web" in progress
    assert "Reading a file" in progress
    assert "Updating workspace files" in progress
    assert all("/" not in label for label in progress)

    results = [event for event in events if event.kind == "result"]
    assert len(results) == 1
    assert results[0].text == adapter.final_text()


def test_non_json_and_provider_error_are_classified_deliberately() -> None:
    adapter = ClaudeAdapter()
    assert adapter.parse_line("   ") == []
    malformed = adapter.parse_line("not-json")
    assert [(event.kind, event.text) for event in malformed] == [
        ("error", "Claude emitted malformed JSONL")
    ]

    provider_error = adapter.parse_line(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "authentication failed",
            }
        )
    )
    assert [(event.kind, event.text) for event in provider_error] == [
        ("error", "authentication failed")
    ]


def test_reasoning_fields_never_escape_normalization() -> None:
    adapter = ClaudeAdapter()
    canary = "PRIVATE-CHAIN-OF-THOUGHT-DO-NOT-LEAK"
    lines = [
        {
            "type": "stream_event",
            "event": {"delta": {"type": "thinking_delta", "thinking": canary}},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": canary}]},
        },
    ]
    normalized = [
        event
        for line in lines
        for event in adapter.parse_line(json.dumps(line))
    ]
    assert normalized == []
    assert canary not in repr(normalized)


def test_build_command_first_and_native_resume_match_proven_spike() -> None:
    session = config()
    first = ClaudeAdapter(executable="claude").build_command(
        session,
        RunContext(
            user_prompt="Investigate this.",
            resume_id=None,
            resume_strategy="native",
            staged_history=[],
            staged_source=None,
        ),
    )
    expected = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--append-system-prompt",
        "Begin with ROLE-CLAUDE-OK.",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Write,Edit,WebSearch,WebFetch",
        "--allowedTools",
        "Read(/**),Edit(/**),WebSearch,WebFetch",
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--no-chrome",
    ]
    assert first.argv == expected
    assert first.stdin == "Investigate this."

    resumed = ClaudeAdapter(executable="claude").build_command(
        session,
        RunContext(
            user_prompt="Continue.",
            resume_id="native-session-id",
            resume_strategy="native",
            staged_history=[],
            staged_source=None,
        ),
    )
    assert resumed.argv == [*expected, "--resume", "native-session-id"]
    assert resumed.stdin == "Continue."


def test_build_command_stateless_reapplies_role_and_lists_staged_history() -> None:
    command = ClaudeAdapter(executable="claude").build_command(
        config(),
        RunContext(
            user_prompt="Continue with the evidence.",
            resume_id=None,
            resume_strategy="stateless",
            staged_history=[
                Path("inputs/round-03/history/round-01.prompt.md"),
                Path("inputs/round-03/history/round-01.md"),
            ],
            staged_source=Path("inputs/round-03/source.md"),
        ),
    )
    assert "Role instructions (reapplied):\nBegin with ROLE-CLAUDE-OK." in command.stdin
    assert "round-01.prompt.md" in command.stdin
    assert "round-01.md" in command.stdin
    assert "inputs/round-03/source.md" in command.stdin
    assert command.stdin.endswith("Continue with the evidence.")


def test_effort_levels_match_installed_claude() -> None:
    assert ClaudeAdapter.EFFORT_LEVELS == ["low", "medium", "high", "xhigh", "max"]


def test_native_resume_after_config_change_matches_real_m4_gate() -> None:
    assert ClaudeAdapter.RESUME_AFTER_CONFIG_CHANGE is True


def usage_events(name: str) -> tuple[list, list]:
    """Split one m5 fixture's events into billing and occupancy reports."""

    _, events = parse_fixture(f"m5/{name}")
    return (
        [event for event in events if event.kind == "turn_usage"],
        [event for event in events if event.kind == "context_usage"],
    )


def test_claude_bills_a_round_from_the_result_line() -> None:
    billing, _ = usage_events("claude_c_small_resume.jsonl")

    assert len(billing) == 1
    usage = billing[0].usage
    # Measured, spike/fixtures/m5/claude_c_small_resume.jsonl.
    assert usage.input_tokens == 2
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 42951
    assert usage.cache_creation_tokens == 17
    assert usage.total_cost_usd == pytest.approx(0.0130533)
    assert usage.max_output_tokens == 64000


def test_claude_occupancy_sums_the_three_counters_of_the_final_assistant() -> None:
    _, occupancy = usage_events("claude_c_small_resume.jsonl")

    assert len(occupancy) == 1
    reading = occupancy[0].context
    # 2 + 42951 + 17: no single Claude counter traces a conversation, because a
    # resumed turn reads back as cache_read what the previous turn wrote as
    # cache_creation.
    assert reading.used_tokens == 42970
    assert reading.context_window == 1_000_000
    assert reading.resolved_model == "claude-sonnet-5"
    assert reading.numerator_source == "claude_final_assistant"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("claude_a_small_fresh.jsonl", 8118),
        ("claude_b_large_resume.jsonl", 42952),
        ("claude_c_small_resume.jsonl", 42970),
        ("claude_d_small_fresh.jsonl", 8118),
    ],
)
def test_claude_occupancy_traces_the_phase_0_experiment(
    name: str,
    expected: int,
) -> None:
    """The measured curve: +18 at C against a 34,834-token jump at B, and an
    exact return to A's value on a fresh session at D."""

    _, occupancy = usage_events(name)

    assert occupancy[0].context.used_tokens == expected


def test_claude_context_window_is_keyed_by_the_model_the_turn_resolved() -> None:
    """The experiment interleaves a model change, so a run can carry two
    modelUsage entries; picking the wrong one is a 5x error in the denominator."""

    _, occupancy = usage_events("claude_e_model_change.jsonl")

    assert occupancy[0].context.resolved_model == "claude-haiku-4-5-20251001"
    assert occupancy[0].context.context_window == 200_000


def test_claude_occupancy_is_unknown_when_the_assistant_model_is_unrecognized() -> None:
    """The induced-error run answers as `<synthetic>`, not as the resolved
    model, so nothing describes the real conversation."""

    _, occupancy = usage_events("claude_f_induced_error.jsonl")

    assert occupancy == [] or occupancy[0].context.used_tokens is None


def mutate_result(name: str, mutate) -> tuple[list, list]:
    adapter = ClaudeAdapter()
    events = []
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == "result":
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
        lambda payload: payload.update({"usage": {"input_tokens": True}}),
        lambda payload: payload.update({"total_cost_usd": "free"}),
    ],
)
def test_claude_billing_degrades_to_unknown_rather_than_failing(mutate) -> None:
    billing, _ = mutate_result("claude_c_small_resume.jsonl", mutate)

    for event in billing:
        assert event.usage.reported


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("modelUsage"),
        lambda payload: payload.update({"modelUsage": {"claude-sonnet-5": "wide"}}),
        lambda payload: payload.update(
            {"modelUsage": {"claude-sonnet-5": {"contextWindow": 0}}}
        ),
    ],
)
def test_claude_never_guesses_a_denominator(mutate) -> None:
    _, occupancy = mutate_result("claude_c_small_resume.jsonl", mutate)

    assert occupancy[0].context.context_window is None
    assert occupancy[0].context.used_tokens == 42970


def rate_limit_events(name: str) -> list:
    adapter = ClaudeAdapter()
    events = []
    for line in (FIXTURES / "m5" / name).read_text(encoding="utf-8").splitlines():
        events.extend(adapter.parse_line(line))
    return [event for event in events if event.kind == "rate_limit"]


def rate_limit_from(info: dict) -> list:
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": info})
    return [
        event for event in ClaudeAdapter().parse_line(line) if event.kind == "rate_limit"
    ]


def test_claude_reports_the_window_its_rate_limit_event_names() -> None:
    events = rate_limit_events("claude_a_small_fresh.jsonl")

    assert len(events) == 1
    reading = events[0].rate_limit
    assert reading.window == "five_hour"
    assert reading.status == "healthy"
    # Measured: resetsAt is epoch seconds, spike/fixtures/m5/claude_a_small_fresh.jsonl.
    assert reading.resets_at == datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
    assert reading.source == "claude_rate_limit_event"


def test_claude_percentages_stay_unknown_because_their_unit_is_unproven() -> None:
    # `utilization` was null on all five Phase 0 turns, and 0.8 is either 0.8%
    # or 80%: structural presence does not establish a unit.
    events = rate_limit_from(
        {
            "rateLimitType": "five_hour",
            "status": "allowed",
            "resetsAt": 1788750000,
            "utilization": 0.8,
        }
    )

    assert events[0].rate_limit.used_percent is None


def test_claude_overage_status_never_decides_the_quota_state() -> None:
    # `overageStatus: rejected` means the account declined pay-as-you-go
    # overage, not that anything was refused: it read `rejected` on all five
    # successful Phase 0 turns, so reading it would pause every single run.
    events = rate_limit_from(
        {
            "rateLimitType": "five_hour",
            "status": "allowed",
            "overageStatus": "rejected",
            "resetsAt": 1788750000,
        }
    )

    assert events[0].rate_limit.status == "healthy"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("allowed", "healthy"),
        ("allowed_warning", "warning"),
        ("rejected", "rejected"),
        ("throttled_soon", "unknown"),
        (None, "unknown"),
    ],
)
def test_claude_status_maps_to_the_status_only_policy(status, expected: str) -> None:
    events = rate_limit_from(
        {"rateLimitType": "five_hour", "status": status, "resetsAt": 1788750000}
    )

    assert events[0].rate_limit.status == expected


@pytest.mark.parametrize(
    "info",
    [
        {"status": "allowed", "resetsAt": 1788750000},
        {"rateLimitType": "monthly", "status": "allowed", "resetsAt": 1788750000},
        {"rateLimitType": 5, "status": "allowed"},
    ],
)
def test_an_unlabelled_window_is_attributed_to_neither(info: dict) -> None:
    assert rate_limit_from(info) == []


@pytest.mark.parametrize("resets_at", ["soon", -1, None, True])
def test_an_unusable_reset_instant_still_reports_the_status(resets_at) -> None:
    events = rate_limit_from(
        {"rateLimitType": "seven_day", "status": "rejected", "resetsAt": resets_at}
    )

    assert events[0].rate_limit.resets_at is None
    assert events[0].rate_limit.window == "seven_day"
    assert events[0].rate_limit.status == "rejected"


def test_a_rate_limit_event_is_no_longer_an_unknown_event() -> None:
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": "wrong shape"})

    assert ClaudeAdapter().parse_line(line) == []
