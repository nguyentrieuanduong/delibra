"""Material a build agent inherits when the previous agent could not finish."""

from __future__ import annotations

import os
from pathlib import Path

SKIPPED_DIRS = frozenset({".delibra", ".git", "envs"})
# Only the tail of a failed turn's output: the end is where it stopped.
HANDOVER_PARTIAL_LIMIT = 65_536


def modified_files_since(root: Path, since_epoch: float, limit: int = 200) -> list[str]:
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        if current == root:
            dirnames[:] = [name for name in dirnames if name not in SKIPPED_DIRS]
        dirnames.sort()
        for name in sorted(filenames):
            path = current / name
            try:
                if path.lstat().st_mtime > since_epoch:
                    found.append(path.relative_to(root).as_posix())
            except OSError:
                continue
    found.sort()
    return found[:limit] + (["... (truncated)"] if len(found) > limit else [])


def render_handover(name: str, reason: str, partial: bytes, files: list[str]) -> bytes:
    listing = "\n".join(f"- {item}" for item in files) or "- none"
    return (
        f"{name} could not finish its turn ({reason}). Continue the same step.\n\n"
        "Partial output of that turn:\n\n"
    ).encode() + partial[-HANDOVER_PARTIAL_LIMIT:] + (
        f"\n\nProject files modified during that turn:\n{listing}\n"
    ).encode()
