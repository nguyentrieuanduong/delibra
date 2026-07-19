"""Pure Auto-mode context rendering and verdict protocol helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from typing import Literal, Sequence


AUTO_VERDICT = re.compile(
    r'^\[DELIBRA_AUTO run="([0-9a-f]{32})" '
    r'turn="([A-Za-z0-9_-]{43})" decision="(agree|continue)"\]$'
)
AUTO_CONTROL_SHAPE = re.compile(r"^\[DELIBRA_AUTO\b.*\]$")
AUTO_VERDICT_WARNING = (
    "Auto verdict footer was missing or invalid; treated as continue."
)


@dataclass(frozen=True)
class ParsedVerdict:
    decision: Literal["agree", "continue"]
    content: str
    warning: str | None


@dataclass(frozen=True)
class ContextEntry:
    label: str
    content: bytes


def new_turn_token() -> str:
    """Return the fixed-width URL-safe token required by the verdict grammar."""

    token = secrets.token_urlsafe(32)
    if len(token) != 43:  # pragma: no cover - documents the stdlib contract.
        raise RuntimeError("generated Auto turn token has an unexpected length")
    return token


def parse_auto_verdict(text: str, auto_id: str, turn_token: str) -> ParsedVerdict:
    """Parse only one exact current control footer from the final non-empty line."""

    lines = text.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    control_lines = [
        index for index, line in enumerate(lines) if AUTO_CONTROL_SHAPE.fullmatch(line)
    ]
    if not nonempty or len(control_lines) != 1 or control_lines[0] != nonempty[-1]:
        return ParsedVerdict("continue", text, AUTO_VERDICT_WARNING)

    marker_index = control_lines[0]
    match = AUTO_VERDICT.fullmatch(lines[marker_index])
    if match is None or match.group(1) != auto_id or match.group(2) != turn_token:
        return ParsedVerdict("continue", text, AUTO_VERDICT_WARNING)

    content_lines = lines[:marker_index] + lines[marker_index + 1 :]
    return ParsedVerdict(match.group(3), "\n".join(content_lines), None)


def _untrusted_section(label: str, content: bytes) -> bytes:
    if not label or len(label) > 200 or "\n" in label or "\r" in label:
        raise ValueError("Auto context label is invalid")
    heading = f"## {label} — UNTRUSTED MATERIAL\n\n".encode("utf-8")
    return heading + content + b"\n"


def render_preparation_context(topic: bytes) -> bytes:
    """Render the independent preparation material without history or peers."""

    return b"# Auto preparation material\n\n" + _untrusted_section("Topic", topic)


def render_discussion_context(
    topic: bytes,
    *,
    preparations: Sequence[ContextEntry],
    baseline_entries: Sequence[ContextEntry],
    discussion_entries: Sequence[ContextEntry],
) -> bytes:
    """Render already-selected discussion material in caller-supplied stable order."""

    sections = [b"# Auto discussion material\n\n", _untrusted_section("Topic", topic)]
    sections.extend(
        _untrusted_section(f"Preparation: {entry.label}", entry.content)
        for entry in preparations
    )
    sections.extend(
        _untrusted_section(f"Conversation: {entry.label}", entry.content)
        for entry in baseline_entries
    )
    sections.extend(
        _untrusted_section(f"Discussion: {entry.label}", entry.content)
        for entry in discussion_entries
    )
    return b"\n".join(sections)
