"""Shared server-authored view projections and identifiers."""

from __future__ import annotations

from typing import Any

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import SessionConfig
from app.storage import NotFoundError, ProjectStore, validate_id


def round_dom_id(session_id: str, round_n: int) -> str:
    """Return the one DOM id used by live and completed round fragments."""
    validate_id(session_id, "session id")
    if round_n < 1:
        raise ValueError("round number must be positive")
    return f"round-{session_id}-{round_n}"


def round_views(store: ProjectStore, session_id: str) -> list[dict[str, Any]]:
    config = store.load_session(session_id)
    rounds_dir = store.rounds_dir(session_id)
    views: list[dict[str, Any]] = []
    known = {record.n for record in config.rounds}
    for record in sorted(config.rounds, key=lambda item: item.n):
        prompt = rounds_dir / f"round-{record.n:02d}.prompt.md"
        output = rounds_dir / f"round-{record.n:02d}.md"
        partial = rounds_dir / f"round-{record.n:02d}.partial.md"
        views.append(
            {
                "n": record.n,
                "record": record,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": False,
                "dom_id": round_dom_id(session_id, record.n),
            }
        )
    scan = store.scan_round_files(session_id)
    for number in sorted({item.n for item in scan.orphans} - known):
        prompt = rounds_dir / f"round-{number:02d}.prompt.md"
        output = rounds_dir / f"round-{number:02d}.md"
        partial = rounds_dir / f"round-{number:02d}.partial.md"
        views.append(
            {
                "n": number,
                "record": None,
                "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else "",
                "output": (
                    output.read_text(encoding="utf-8")
                    if output.exists()
                    else partial.read_text(encoding="utf-8")
                    if partial.exists()
                    else ""
                ),
                "orphan": True,
                "dom_id": round_dom_id(session_id, number),
            }
        )
    return sorted(views, key=lambda item: item["n"])


def effort_levels() -> dict[str, list[str]]:
    return {
        "claude": ClaudeAdapter.EFFORT_LEVELS,
        "codex": CodexAdapter.EFFORT_LEVELS,
    }


def selected_session(
    sessions: list[SessionConfig],
    requested_id: str | None,
) -> SessionConfig | None:
    ordered = sorted(sessions, key=lambda item: (item.name.casefold(), item.id))
    if requested_id is None:
        return ordered[0] if ordered else None
    validate_id(requested_id, "selected agent id")
    selected = next((item for item in sessions if item.id == requested_id), None)
    if selected is None:
        raise NotFoundError("selected agent does not belong to this project")
    return selected


def _truncate_plain_text(value: str, maximum: int = 240) -> str:
    cleaned = value.strip()
    if len(cleaned) <= maximum:
        return cleaned
    return cleaned[: maximum - 1] + "…"


def agent_views(
    store: ProjectStore,
    sessions: list[SessionConfig],
    selected: SessionConfig | None,
) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for session in sorted(sessions, key=lambda item: (item.name.casefold(), item.id)):
        latest = max(session.rounds, key=lambda item: item.n, default=None)
        if latest is None:
            preview = "No rounds yet."
            preview_status = "empty"
        elif latest.status == "running":
            preview = "Running…"
            preview_status = "running"
        else:
            rounds = store.rounds_dir(session.id)
            output = rounds / f"round-{latest.n:02d}.md"
            partial = rounds / f"round-{latest.n:02d}.partial.md"
            source = output if output.is_file() else partial
            text = source.read_text(encoding="utf-8") if source.is_file() else ""
            if latest.status == "error":
                preview = latest.error or text or "Run failed."
            else:
                preview = text or "No output."
            preview = _truncate_plain_text(preview)
            preview_status = latest.status
        views.append(
            {
                "session": session,
                "selected": selected is not None and session.id == selected.id,
                "preview": preview,
                "preview_status": preview_status,
            }
        )
    return views
