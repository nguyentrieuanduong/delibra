from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.path_picker import (
    PickerError,
    breadcrumbs,
    display_path,
    kilobytes,
    list_machine_directory,
    picker_start,
)


def _tree(root: Path) -> Path:
    (root / "b-dir").mkdir(parents=True)
    (root / "A-dir").mkdir()
    (root / ".hidden").mkdir()
    (root / "z.md").write_text("# Z\n")
    (root / "a.MD").write_text("x" * 1500)
    (root / "notes.txt").write_text("t")
    os.symlink(root / "z.md", root / "link.md")
    return root


def test_folders_come_first_then_markdown_then_symlinks(tmp_path) -> None:
    root = _tree(tmp_path / "skills")

    listing = list_machine_directory(str(root), markdown=True, hidden=False)

    assert [(entry.name, entry.kind) for entry in listing.entries] == [
        ("A-dir", "directory"),
        ("b-dir", "directory"),
        ("a.MD", "markdown"),
        ("z.md", "markdown"),
        ("link.md", "symlink"),
    ]
    assert listing.entries[2].size == 1500
    assert listing.entries[2].path == str(root / "a.MD")
    assert listing.path == str(root)
    assert listing.truncated is False


def test_folder_mode_lists_no_files_and_hidden_needs_asking(tmp_path) -> None:
    root = _tree(tmp_path / "skills")

    listing = list_machine_directory(str(root), markdown=False, hidden=True)

    assert [entry.name for entry in listing.entries] == [".hidden", "A-dir", "b-dir", "link.md"]


@pytest.mark.parametrize("suffix", ["link", "real/..", "missing", "file.md"])
def test_only_a_real_folder_can_be_browsed(tmp_path, suffix) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "file.md").write_text("x")
    os.symlink(tmp_path / "real", tmp_path / "link")
    raw = f"{tmp_path}/{suffix}"

    with pytest.raises(PickerError) as caught:
        list_machine_directory(raw, markdown=True, hidden=False)

    assert str(caught.value) == (
        f"{raw!r} cannot be browsed: open a folder reached without symlinks"
    )


@pytest.mark.parametrize("raw", ["relative", "/a\x01b", "/" + "a" * 4096])
def test_malformed_paths_cannot_be_browsed(raw) -> None:
    with pytest.raises(PickerError, match="cannot be browsed"):
        list_machine_directory(raw, markdown=True, hidden=False)


def test_a_listing_stops_at_the_entry_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.coding.GUIDANCE_SCAN_MAX_ENTRIES", 3)
    for index in range(10):
        (tmp_path / f"{index}.md").write_text("x")

    listing = list_machine_directory(str(tmp_path), markdown=True, hidden=False)

    assert listing.truncated is True
    assert len(listing.entries) == 3


def test_paths_under_home_display_with_a_tilde(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert display_path(str(tmp_path)) == "~"
    assert display_path(str(tmp_path / "a" / "b.md")) == "~/a/b.md"
    assert display_path(f"{tmp_path}x/b.md") == f"{tmp_path}x/b.md"
    assert display_path("/opt/a.md") == "/opt/a.md"
    assert list_machine_directory("~", markdown=True, hidden=False).path == str(tmp_path)


def test_breadcrumbs_start_at_root_and_call_home_tilde(tmp_path, monkeypatch) -> None:
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)

    assert breadcrumbs(f"{home}/projects/x") == [
        ("/", "/"),
        ("~", home),
        ("projects", f"{home}/projects"),
        ("x", f"{home}/projects/x"),
    ]
    assert breadcrumbs("/opt/a") == [("/", "/"), ("opt", "/opt"), ("a", "/opt/a")]
    assert breadcrumbs("/") == [("/", "/")]


def test_the_start_folder_follows_the_field(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "a.md").write_text("x")

    assert picker_start(str(tmp_path / "skills")) == str(tmp_path / "skills")
    assert picker_start("~/skills/a.md") == str(tmp_path / "skills")
    assert picker_start("", str(tmp_path / "skills")) == str(tmp_path / "skills")
    assert picker_start("relative", "bad\x01") == str(tmp_path)


def test_kilobytes_round_up() -> None:
    assert [kilobytes(size) for size in (0, 1, 1024, 1025)] == [0, 1, 1, 2]
