"""Fixed inputs for the stateless-continuation golden cases (editable-prompts plan, Task 1)."""

from __future__ import annotations

from pathlib import Path

from app.agents.base import RunContext
from app.models import SessionConfig


def _config(agent: str) -> SessionConfig:
    return SessionConfig(
        id="b" * 32, name="Alpha", agent=agent, model="m", effort="high",
        role_instructions="Be the critic.", cli_session_id=None, status="idle",
        created_at="2026-11-15T00:00:00Z", rounds=[],
    )


def _context(source: bool, history: bool) -> RunContext:
    return RunContext(
        user_prompt="What next?", resume_id=None, resume_strategy="stateless",
        staged_history=[Path("inputs/round-03/history-01.md")] if history else [],
        staged_source=Path("inputs/round-03/source.md") if source else None,
        workspace=Path("/w/sessions/alpha/workspace"),
        global_instruction="Answer in English.",
        system_note="Skills you may read are listed in /w/index.md.",
    )


def stateless_cases(claude, codex) -> dict[str, str]:
    out = {}
    for source in (False, True):
        for history in (False, True):
            suffix = f"{'source' if source else 'nosource'}_{'history' if history else 'nohistory'}"
            out[f"stateless_claude/{suffix}"] = claude.build_command(_config("claude"), _context(source, history)).stdin
            out[f"stateless_codex/{suffix}"] = codex.build_command(_config("codex"), _context(source, history)).stdin
    return out
