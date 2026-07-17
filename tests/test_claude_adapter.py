from __future__ import annotations

import json
from pathlib import Path

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
