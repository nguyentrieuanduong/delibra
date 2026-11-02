from __future__ import annotations

from pathlib import Path

import pytest

from app.skill_folders import (
    chat_skills_note,
    read_skill_index,
    refresh_skill_folder,
    skill_folder,
    skill_index_path,
    sync_folder,
)
from app.storage import OwnershipError, StorageError


def _suite(root: Path) -> list[str]:
    skill = root / "vi-humanize" / "SKILL.md"
    (skill.parent / "references").mkdir(parents=True)
    skill.write_text("---\nname: vi-humanize\ndescription: Viết tự nhiên.\n---\n")
    (skill.parent / "references" / "r.md").write_text("ref\n")
    return [str(skill), str(skill.parent / "references" / "r.md")]


def _delibra(tmp_path: Path) -> Path:
    root = tmp_path / "project" / ".delibra"
    root.mkdir(parents=True)
    return root


def test_a_refresh_copies_the_list_and_writes_index_and_digest_last(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")

    snapshot = refresh_skill_folder(root, "chat", paths)

    assert snapshot is not None
    index = snapshot.index
    folder = skill_folder(root, "chat")
    assert folder == root / "skills" / "chat"
    assert (folder / "vi-humanize" / "SKILL.md").read_bytes() == Path(paths[0]).read_bytes()
    assert (folder / "vi-humanize" / "references" / "r.md").read_text() == "ref\n"
    assert index == skill_index_path(root, "chat").read_bytes()
    assert index.decode() == (
        f"- vi-humanize — {folder / 'vi-humanize' / 'SKILL.md'}\n  Viết tự nhiên.\n"
    )
    assert snapshot.digest == (root / "skills" / "chat.sha256").read_text()
    assert len(snapshot.digest) == 64
    assert not (root / "skills" / "build").exists()


def test_an_unchanged_list_rewrites_nothing(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    first = refresh_skill_folder(root, "build", paths)
    copied = skill_folder(root, "build") / "vi-humanize" / "SKILL.md"
    before = copied.stat().st_mtime_ns

    assert refresh_skill_folder(root, "build", paths) == first

    assert copied.stat().st_mtime_ns == before


def test_an_edit_rewrites_and_a_dropped_file_is_removed(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    first = refresh_skill_folder(root, "chat", paths)
    Path(paths[0]).write_text("---\nname: vi-humanize\ndescription: Mới.\n---\n")

    second = refresh_skill_folder(root, "chat", paths[:1])

    folder = skill_folder(root, "chat")
    assert "Mới." in (folder / "vi-humanize" / "SKILL.md").read_text()
    assert not (folder / "vi-humanize" / "references").exists()
    assert second is not None and first is not None and second.digest != first.digest


def test_a_content_change_alone_changes_the_digest(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    first = refresh_skill_folder(root, "chat", paths)
    Path(paths[1]).write_text("edited reference\n")

    second = refresh_skill_folder(root, "chat", paths)

    assert first is not None and second is not None
    assert second.index == first.index
    assert second.digest != first.digest
    assert (skill_folder(root, "chat") / "vi-humanize" / "references" / "r.md").read_text() == (
        "edited reference\n"
    )


def test_an_empty_list_removes_folder_index_and_digest(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    refresh_skill_folder(root, "chat", _suite(tmp_path / "src"))

    assert refresh_skill_folder(root, "chat", []) is None

    assert not (root / "skills" / "chat").exists()
    assert read_skill_index(root, "chat") is None
    assert not (root / "skills" / "chat.sha256").exists()


def test_a_missing_file_is_refused_with_the_build_message(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    Path(paths[1]).unlink()

    with pytest.raises(StorageError, match="^Skill file changed or is unavailable: "):
        refresh_skill_folder(root, "chat", paths)


def test_without_rewrite_a_stale_folder_stays_but_the_list_is_still_checked(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    first = refresh_skill_folder(root, "chat", paths)
    Path(paths[0]).write_text("---\nname: vi-humanize\ndescription: Mới.\n---\n")

    assert refresh_skill_folder(root, "chat", paths, rewrite=False) == first
    assert "Mới." not in (skill_folder(root, "chat") / "vi-humanize" / "SKILL.md").read_text()

    Path(paths[1]).unlink()
    with pytest.raises(StorageError, match="^Skill file changed or is unavailable: "):
        refresh_skill_folder(root, "chat", paths, rewrite=False)
    assert refresh_skill_folder(root, "chat", [], rewrite=False) is None
    assert skill_folder(root, "chat").is_dir()


def test_without_rewrite_a_missing_index_writes_nothing(tmp_path: Path) -> None:
    root = _delibra(tmp_path)
    paths = _suite(tmp_path / "src")
    refresh_skill_folder(root, "chat", paths)
    skill_index_path(root, "chat").unlink()
    copied = skill_folder(root, "chat") / "vi-humanize" / "SKILL.md"
    before = copied.stat().st_mtime_ns
    Path(paths[0]).write_text("---\nname: vi-humanize\ndescription: Mới.\n---\n")

    assert refresh_skill_folder(root, "chat", paths, rewrite=False) is None

    assert copied.stat().st_mtime_ns == before
    assert not skill_index_path(root, "chat").exists()


@pytest.mark.parametrize("destination", ["../escape.md", "/abs.md", "a//b.md", "a/./b.md", ""])
def test_a_copy_outside_its_folder_is_refused(tmp_path: Path, destination: str) -> None:
    folder = _delibra(tmp_path) / "skills" / "chat"
    folder.mkdir(parents=True)

    with pytest.raises(OwnershipError, match="skill copy path"):
        sync_folder(folder, [(destination, b"x")])
    assert not (tmp_path / "project" / ".delibra" / "skills" / "escape.md").exists()


def test_the_chat_note_names_the_index() -> None:
    note = chat_skills_note(Path("/p/.delibra/skills/chat-index.md"))
    assert note.startswith("Skills you may read are listed in /p/.delibra/skills/chat-index.md")


def test_a_project_without_a_skills_folder_has_no_index(tmp_path: Path) -> None:
    root = _delibra(tmp_path)

    assert read_skill_index(root, "chat") is None
    assert read_skill_index(root, "build") is None
    assert not (root / "skills").exists()
