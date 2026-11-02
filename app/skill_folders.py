"""One shared, read-only skills folder per mode (user decision 2026-11-02).

`.delibra/skills/build/` serves build runs; `.delibra/skills/chat/` serves
chat turns and discussion runs. Beside each folder sit `<mode>-index.md` and
`<mode>.sha256`. The digest is removed before a rewrite and written last, so a
crash mid-refresh leaves no digest and the next refresh rewrites everything.
Callers hold a lock under which no turn reads the folder being rewritten, or
pass `rewrite=False`.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import shutil
from typing import Literal

from app.coding import CodingPolicyError, read_skill_library, render_skill_index
from app.storage import (
    StorageError,
    _assert_no_symlink_components,
    _check_skill_destination,
    atomic_write_bytes,
    ensure_owned_directory,
)

SkillMode = Literal["build", "chat"]


@dataclass(frozen=True)
class SkillSnapshot:
    index: bytes
    # Content digest of every listed file (destination + sha256 of its bytes);
    # "" when a stale folder was kept and its digest is unknown.
    digest: str


def skills_root(delibra_root: Path) -> Path:
    return delibra_root / "skills"


def skill_folder(delibra_root: Path, mode: SkillMode) -> Path:
    return skills_root(delibra_root) / mode


def skill_index_path(delibra_root: Path, mode: SkillMode) -> Path:
    return skills_root(delibra_root) / f"{mode}-index.md"


def _digest_path(delibra_root: Path, mode: SkillMode) -> Path:
    return skills_root(delibra_root) / f"{mode}.sha256"


def _library_digest(library: list[tuple[str, str, bytes]]) -> str:
    hasher = sha256()
    for _source, destination, contents in library:
        hasher.update(f"{destination}\0{sha256(contents).hexdigest()}\n".encode())
    return hasher.hexdigest()


def _read_digest(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii")
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def read_skill_index(delibra_root: Path, mode: SkillMode) -> bytes | None:
    path = skill_index_path(delibra_root, mode)
    _assert_no_symlink_components(path, delibra_root, allow_missing_leaf=True)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def sync_folder(folder: Path, files: list[tuple[str, bytes]]) -> None:
    """Make `folder` hold exactly `files` (relative path, contents)."""

    wanted: set[Path] = set()
    for relative, contents in files:
        _check_skill_destination(relative)
        target = folder / relative
        for parent in reversed(target.parents):
            if parent.is_relative_to(folder) and parent != folder:
                ensure_owned_directory(parent, folder)
        _assert_no_symlink_components(target, folder, allow_missing_leaf=True)
        atomic_write_bytes(target, contents)
        wanted.add(target)
    for current, directories, names in os.walk(folder, topdown=False, followlinks=False):
        here = Path(current)
        for name in names:
            if here / name not in wanted:
                (here / name).unlink()
        for name in directories:
            child = here / name
            if child.is_symlink():
                child.unlink()
            elif not any(child.iterdir()):
                child.rmdir()


def refresh_skill_folder(
    delibra_root: Path, mode: SkillMode, paths: list[str], *, rewrite: bool = True
) -> SkillSnapshot | None:
    """Check `paths` now, always, and bring the mode's folder in line with them.

    With `rewrite=False` another turn may be reading the folder, so nothing is
    written: a stale folder is offered as it is, and with no index there is
    nothing consistent to offer (None; the next idle send rewrites). The list
    is still read and checked, so a removed or symlinked file is refused.
    """

    root = ensure_owned_directory(skills_root(delibra_root), delibra_root)
    folder = skill_folder(delibra_root, mode)
    index_path = skill_index_path(delibra_root, mode)
    digest_path = _digest_path(delibra_root, mode)
    if not paths:
        if rewrite:
            digest_path.unlink(missing_ok=True)
            index_path.unlink(missing_ok=True)
            _assert_no_symlink_components(folder, root, allow_missing_leaf=True)
            if folder.exists():
                shutil.rmtree(folder)
        return None
    try:
        library = read_skill_library(paths)
    except CodingPolicyError as exc:
        raise StorageError(f"Skill file changed or is unavailable: {exc}") from exc
    digest = _library_digest(library)
    index = render_skill_index(library, folder)
    current = _read_digest(digest_path)
    if current == digest and folder.is_dir() and read_skill_index(delibra_root, mode) == index:
        return SkillSnapshot(index, digest)
    if not rewrite:
        shown = read_skill_index(delibra_root, mode)
        return SkillSnapshot(shown, current or "") if shown is not None else None
    digest_path.unlink(missing_ok=True)
    ensure_owned_directory(folder, root)
    sync_folder(folder, [(destination, contents) for _source, destination, contents in library])
    atomic_write_bytes(index_path, index)
    atomic_write_bytes(digest_path, digest.encode("ascii"))
    return SkillSnapshot(index, digest)


def chat_skills_note(index_path: Path) -> str:
    return (
        f"Skills you may read are listed in {index_path.as_posix()} with each one's "
        "name, path and description. Read a skill's SKILL.md before acting when your "
        "role, the message or the skill's description says it applies; its other "
        "files sit beside it."
    )
