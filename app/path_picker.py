"""This machine's folders for the Browse… dialog, read without following links."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Literal

from app import coding
from app.coding import open_without_symlinks
from app.config import expand_home

PICKER_PATH_MAX = 4096


class PickerError(ValueError):
    pass


@dataclass(frozen=True)
class PickerEntry:
    name: str
    path: str
    kind: Literal["directory", "markdown", "symlink"]
    size: int = 0


@dataclass(frozen=True)
class PickerListing:
    path: str
    entries: tuple[PickerEntry, ...]
    truncated: bool


def kilobytes(size: int) -> int:
    return -(-size // 1024)


def display_path(path: str) -> str:
    """``~`` form of a path under this user's home, else the path unchanged."""

    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


def _refusal(raw: str) -> PickerError:
    return PickerError(f"{raw!r} cannot be browsed: open a folder reached without symlinks")


def _resolve(raw: str) -> Path:
    if raw in ("", "~"):
        return Path.home()
    if len(raw) > PICKER_PATH_MAX or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise _refusal(raw[:200])
    path = expand_home(raw)
    if not path.is_absolute():
        raise _refusal(raw)
    return path


def _is_directory(path: Path) -> bool:
    try:
        descriptor = open_without_symlinks(path)
    except OSError:
        return False
    try:
        return stat.S_ISDIR(os.fstat(descriptor).st_mode)
    finally:
        os.close(descriptor)


def picker_start(*candidates: str) -> str:
    """The first candidate that is a folder (a file's own folder), else home."""

    for raw in candidates:
        raw = raw.strip()
        if not raw:
            continue
        try:
            path = _resolve(raw)
        except PickerError:
            continue
        for option in (path, path.parent):
            if _is_directory(option):
                return str(option)
    return str(Path.home())


def breadcrumbs(path: str) -> list[tuple[str, str]]:
    """(label, path) pairs from ``/``; this user's home is labelled ``~``."""

    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        crumbs = [("/", "/"), ("~", home)]
        current = home
        rest = path[len(home) :].strip("/")
        parts = rest.split("/") if rest else []
    else:
        crumbs = [("/", "/")]
        current = ""
        parts = list(Path(path).parts[1:])
    for part in parts:
        current = f"{current.rstrip('/')}/{part}"
        crumbs.append((part, current))
    return crumbs


def list_machine_directory(raw: str, *, markdown: bool, hidden: bool) -> PickerListing:
    """One folder's sub-folders, `.md` files (when asked) and symlinks.

    Opened with `open_without_symlinks`, so the dialog cannot reach a folder
    the guidance code would refuse; read lazily within the scan budget.
    """

    path = _resolve(raw)
    try:
        descriptor = open_without_symlinks(path)
    except OSError as exc:
        raise _refusal(raw) from exc
    entries: list[PickerEntry] = []
    truncated = False
    budget = coding.GUIDANCE_SCAN_MAX_ENTRIES
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _refusal(raw)
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if budget == 0:
                    truncated = True
                    break
                budget -= 1
                if entry.name.startswith(".") and not hidden:
                    continue
                full = str(path / entry.name)
                if entry.is_symlink():
                    entries.append(PickerEntry(entry.name, full, "symlink"))
                elif entry.is_dir(follow_symlinks=False):
                    entries.append(PickerEntry(entry.name, full, "directory"))
                elif (
                    markdown
                    and entry.is_file(follow_symlinks=False)
                    and entry.name.lower().endswith(".md")
                ):
                    size = entry.stat(follow_symlinks=False).st_size
                    entries.append(PickerEntry(entry.name, full, "markdown", size))
    finally:
        os.close(descriptor)
    order = {"directory": 0, "markdown": 1, "symlink": 2}
    entries.sort(key=lambda item: (order[item.kind], item.name.casefold(), item.name))
    return PickerListing(str(path), tuple(entries), truncated)
