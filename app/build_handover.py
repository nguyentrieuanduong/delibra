"""Material a build agent inherits when the previous agent could not finish."""

from __future__ import annotations

import os
from pathlib import Path

# Delibra owns only the root `.delibra` and `envs`; same-named directories
# deeper down are project code. `.git` is skipped at every depth (nested
# repositories and submodules).
SKIPPED_EVERYWHERE = frozenset({".git"})
SKIPPED_AT_ROOT = frozenset({".delibra", "envs"})
MAX_SCANNED_ENTRIES = 50_000
# Only the tail of a failed turn's output: the end is where it stopped.
HANDOVER_PARTIAL_LIMIT = 65_536


def modified_files_since(
    root: Path,
    since_epoch: float,
    limit: int = 200,
    max_entries: int = MAX_SCANNED_ENTRIES,
) -> list[str]:
    """Paths changed since a failed turn started, for the next agent.

    Bounded twice, by `limit` reported paths and `max_entries` walked
    entries: a large tree must not stall a handover. `os.scandir` streams
    entries, where `os.walk` would list a whole directory before any budget
    check. Symlinks are never followed or listed.
    """

    found: list[str] = []
    scanned = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if scanned >= max_entries:
                        return sorted(found) + [
                            f"... (scan stopped after {max_entries} entries)"
                        ]
                    scanned += 1
                    if entry.name in SKIPPED_EVERYWHERE or (
                        directory == root and entry.name in SKIPPED_AT_ROOT
                    ):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif (
                            entry.is_file(follow_symlinks=False)
                            and entry.stat(follow_symlinks=False).st_mtime > since_epoch
                        ):
                            found.append(Path(entry.path).relative_to(root).as_posix())
                            if len(found) > limit:
                                return sorted(found[:limit]) + ["... (truncated)"]
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(found)


def render_handover(name: str, reason: str, partial: bytes, files: list[str]) -> bytes:
    listing = "\n".join(f"- {item}" for item in files) or "- none"
    return (
        f"{name} could not finish its turn ({reason}). Continue the same step.\n\n"
        "Partial output of that turn:\n\n"
    ).encode() + partial[-HANDOVER_PARTIAL_LIMIT:] + (
        f"\n\nProject files modified during that turn:\n{listing}\n"
    ).encode()
