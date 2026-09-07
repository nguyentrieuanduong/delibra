"""Shared server-authored view projections and identifiers."""

from __future__ import annotations

from typing import Any

from app.agents.claude import ClaudeAdapter
from app.agents.codex import CodexAdapter
from app.models import (
    Project,
    RoundRecord,
    SessionConfig,
    TurnUsage,
    total_turn_usage,
)
from app.storage import (
    NotFoundError,
    ProjectStore,
    StorageError,
    validate_id,
)
from app.urls import project_url


def project_card(project: Project) -> dict[str, Any]:
    try:
        store = ProjectStore(project)
        has_legacy_sessions = store.has_legacy_session_directories()
        auto_active = store.active_auto_run_id() is not None
    except StorageError as exc:
        return {
            "project": project,
            "available": False,
            "error": str(exc),
            "auto_active": False,
            "has_legacy_sessions": False,
        }
    return {
        "project": project,
        "available": True,
        "error": None,
        "auto_active": auto_active,
        "has_legacy_sessions": has_legacy_sessions,
    }


def round_dom_id(session_id: str, round_n: int) -> str:
    """Return the one DOM id used by live and completed round fragments."""
    validate_id(session_id, "session id")
    if round_n < 1:
        raise ValueError("round number must be positive")
    return f"round-{session_id}-{round_n}"


def quota_notice(record: RoundRecord | None) -> str | None:
    """State a quota refusal as a pause, or ``None`` for any other round.

    The stored round stays an error -- a call really did fail, and every status
    query and the startup reconciliation depend on that -- so the pause exists
    only in presentation, branched on the folded category.
    """

    if record is None or record.error_category != "quota":
        return None
    provider = record.agent.title()
    if record.source.type == "auto":
        return (
            f"Auto paused: {provider} quota limit reached."
            " Continue Auto when you are ready."
        )
    # Nothing was paused on a hand-sent prompt; claiming otherwise would be a
    # fabricated explanation.
    return (
        f"{provider} quota limit reached; this turn did not run."
        " Retry once the quota window resets."
    )


def round_views(
    store: ProjectStore,
    session_id: str,
    *,
    auto_numbers: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    if auto_numbers is None:
        auto_numbers = store.auto_number_map_for_view()
    config = store.load_session(session_id)
    rounds_dir = store.rounds_dir(session_id)
    views: list[dict[str, Any]] = []
    known = {record.n for record in config.rounds}
    for record in sorted(config.rounds, key=lambda item: item.n):
        prompt = rounds_dir / f"round-{record.n:02d}.prompt.md"
        output = rounds_dir / f"round-{record.n:02d}.md"
        partial = rounds_dir / f"round-{record.n:02d}.partial.md"
        auto_number = (
            auto_numbers.get(record.auto.auto_id)
            if record.auto is not None
            else None
        )
        views.append(
            {
                "n": record.n,
                "record": record,
                "quota_notice": quota_notice(record),
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
                "auto_number": auto_number,
                "auto_url": (
                    project_url(
                        store.project.name,
                        f"/auto-runs/{auto_number}",
                    )
                    if auto_number is not None
                    else None
                ),
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
                "quota_notice": None,
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
                "auto_number": None,
                "auto_url": None,
            }
        )
    return sorted(views, key=lambda item: item["n"])


def conversation_round_views(
    store: ProjectStore,
    session_id: str,
    *,
    auto_numbers: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Project rounds visible as shared conversation messages."""

    return round_views(store, session_id, auto_numbers=auto_numbers)


def auto_run_usage(store: ProjectStore, auto_id: str) -> TurnUsage:
    """Total what one Auto run cost, across every round it produced.

    Preparation, discussion and failed attempts all bill, so all of them
    count: a total that silently omitted the failures would understate the
    run every time a turn was retried.
    """

    return total_turn_usage(
        record.usage
        for session in store.list_sessions()
        for record in session.rounds
        if record.auto is not None and record.auto.auto_id == auto_id
    )


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
        visible_rounds = [
            record
            for record in session.rounds
            if record.auto is None or record.auto.phase != "preparation"
        ]
        latest = max(visible_rounds, key=lambda item: item.n, default=None)
        preview_error: str | None = None
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
            preview = text or "No output."
            preview = _truncate_plain_text(preview)
            if latest.status == "error":
                preview_error = _truncate_plain_text(latest.error or "Run failed.")
            preview_status = latest.status
        views.append(
            {
                "session": session,
                "selected": selected is not None and session.id == selected.id,
                "preview": preview,
                "preview_error": preview_error,
                "preview_status": preview_status,
                "latest_round": latest.n if latest is not None else None,
            }
        )
    return views
