from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
import zipfile

import pytest

from app import archive as archive_module
from app.archive import write_archive
from app.storage import OwnershipError, StorageError


def _tree(root: Path) -> None:
    (root / "auto-runs" / "1").mkdir(parents=True)
    (root / "auto-runs" / "1" / "topic.md").write_text("Topic", encoding="utf-8")
    (root / "sessions" / "Alpha" / "rounds").mkdir(parents=True)
    (root / "questions-history.json").write_text("{}", encoding="utf-8")


def test_write_archive_zips_sources_with_paths_relative_to_root(tmp_path: Path) -> None:
    root = tmp_path / ".delibra"
    _tree(root)

    path = write_archive(
        root,
        "history",
        [root / "auto-runs", root / "sessions", root / "questions-history.json", root / "missing.json"],
    )

    assert path.parent == root / "archive"
    assert re.fullmatch(r"\d{8}T\d{6}Z-history-[0-9a-f]{8}\.zip", path.name)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in (root / "archive").iterdir()] == [path.name]
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [
            "auto-runs/",
            "auto-runs/1/",
            "auto-runs/1/topic.md",
            "sessions/",
            "sessions/Alpha/",
            "sessions/Alpha/rounds/",
            "questions-history.json",
        ]
        assert archive.read("auto-runs/1/topic.md") == b"Topic"
    assert (root / "auto-runs" / "1" / "topic.md").read_text(encoding="utf-8") == "Topic"


def test_write_archive_stores_symlinks_unfollowed_and_skips_fifos(tmp_path: Path) -> None:
    root = tmp_path / ".delibra"
    workspace = root / "sessions" / "Alpha" / "workspace"
    workspace.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (workspace / "link").symlink_to(secret)
    os.mkfifo(workspace / "pipe")

    path = write_archive(root, "agents", [root / "sessions"])

    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("sessions/Alpha/workspace/link")
        assert stat.S_ISLNK(info.external_attr >> 16)
        assert archive.read(info) == str(secret).encode()
        assert "sessions/Alpha/workspace/pipe" not in archive.namelist()


def test_a_failed_check_leaves_no_zip_and_keeps_sources(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".delibra"
    _tree(root)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda _self: "auto-runs/1/topic.md")

    with pytest.raises(StorageError, match="archive check failed at auto-runs/1/topic.md"):
        write_archive(root, "history", [root / "auto-runs"])

    assert list((root / "archive").iterdir()) == []
    assert (root / "auto-runs" / "1" / "topic.md").exists()


def test_an_unwritable_archive_dir_is_a_storage_error(tmp_path: Path) -> None:
    root = tmp_path / ".delibra"
    _tree(root)
    (root / "archive").mkdir(mode=0o500)
    try:
        with pytest.raises(StorageError, match="archive could not be written: Permission denied"):
            write_archive(root, "history", [root / "auto-runs"])
    finally:
        (root / "archive").chmod(0o700)
    assert list((root / "archive").iterdir()) == []


def test_a_parent_swapped_for_an_outside_symlink_mid_walk_never_enters_the_zip(
    tmp_path: Path, monkeypatch
) -> None:
    # Codex B1: the walk opens through no-follow directory descriptors, so a
    # parent replaced by a symlink after it was entered cannot redirect a read.
    root = tmp_path / ".delibra"
    sub = root / "sessions" / "Alpha" / "workspace" / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("outside", encoding="utf-8")
    real_open = os.open

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "a.txt" and not sub.is_symlink():
            sub.rename(sub.with_name("moved"))
            sub.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    path = write_archive(root, "agents", [root / "sessions"])
    monkeypatch.undo()

    assert sub.is_symlink()  # the swap happened mid-walk
    with zipfile.ZipFile(path) as archive:
        assert archive.read("sessions/Alpha/workspace/sub/a.txt") == b"inside"
        assert all(archive.read(name) != b"outside" for name in archive.namelist())


def test_a_name_collision_never_replaces_an_earlier_archive(tmp_path: Path, monkeypatch) -> None:
    # Codex B2: the final name is reserved exclusively; a taken name is retried.
    root = tmp_path / ".delibra"
    _tree(root)
    moment = datetime(2026, 11, 12, 14, 30, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(archive_module, "_now", lambda: moment)
    suffixes = iter(["a" * 32, "a" * 32, "b" * 32])
    monkeypatch.setattr(archive_module, "uuid4", lambda: SimpleNamespace(hex=next(suffixes)))

    first = write_archive(root, "history", [root / "auto-runs"])
    (root / "auto-runs" / "1" / "topic.md").write_text("Changed", encoding="utf-8")
    second = write_archive(root, "history", [root / "auto-runs"])

    assert first.name == "20261112T143005Z-history-aaaaaaaa.zip"
    assert second.name == "20261112T143005Z-history-bbbbbbbb.zip"
    assert sorted(item.name for item in (root / "archive").iterdir()) == [first.name, second.name]
    with zipfile.ZipFile(first) as archive:
        assert archive.read("auto-runs/1/topic.md") == b"Topic"
    with zipfile.ZipFile(second) as archive:
        assert archive.read("auto-runs/1/topic.md") == b"Changed"


@pytest.mark.parametrize(
    "source", ["outside", ".delibra", ".delibra/archive", ".delibra/sessions/../../outside"]
)
def test_write_archive_refuses_sources_outside_root_or_the_archive_itself(
    tmp_path: Path, source: str
) -> None:
    root = tmp_path / ".delibra"
    _tree(root)

    with pytest.raises(OwnershipError):
        write_archive(root, "full", [tmp_path / source])

    assert list((root / "archive").iterdir()) == []
