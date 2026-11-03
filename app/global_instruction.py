"""The user's global instruction: one text added to every agent turn.

Delibra runs Claude with ``--setting-sources ""`` and Codex with its own
CODEX_HOME, so ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md never reach its
agents; this file stands in for them (user decision 2026-11-03).
"""

from __future__ import annotations

from pathlib import Path

from app.storage import StorageError, atomic_write_bytes

GLOBAL_INSTRUCTION_FILENAME = "instructions.md"
GLOBAL_INSTRUCTION_MAX_BYTES = 64 * 1024


class GlobalInstructionError(StorageError):
    """The global instruction cannot be saved or used as it is."""


def global_instruction_path(home: Path) -> Path:
    return home / GLOBAL_INSTRUCTION_FILENAME


def _checked(data: bytes) -> str:
    if len(data) > GLOBAL_INSTRUCTION_MAX_BYTES:
        raise GlobalInstructionError(
            f"The global instruction is larger than {GLOBAL_INSTRUCTION_MAX_BYTES // 1024} KB"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise GlobalInstructionError("The global instruction is not valid UTF-8") from None
    return text if text.strip() else ""


def read_global_instruction(home: Path) -> str:
    """The saved text, or "" when there is none; blocking."""

    try:
        with global_instruction_path(home).open("rb") as handle:
            data = handle.read(GLOBAL_INSTRUCTION_MAX_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise GlobalInstructionError(
            f"The global instruction cannot be read: {exc.strerror or exc}"
        ) from None
    return _checked(data)


def save_global_instruction(home: Path, text: str) -> str:
    """Store ``text`` with LF line ends; a blank text removes the file. Blocking."""

    checked = _checked(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    path = global_instruction_path(home)
    if not checked:
        path.unlink(missing_ok=True)
        return ""
    atomic_write_bytes(path, checked.encode("utf-8"))
    return checked
