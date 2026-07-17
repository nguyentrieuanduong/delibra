"""Shared server-authored view identifiers."""

from __future__ import annotations

from app.storage import validate_id


def round_dom_id(session_id: str, round_n: int) -> str:
    """Return the one DOM id used by live and completed round fragments."""
    validate_id(session_id, "session id")
    if round_n < 1:
        raise ValueError("round number must be positive")
    return f"round-{session_id}-{round_n}"
