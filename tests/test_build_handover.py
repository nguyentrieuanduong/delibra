import os
from pathlib import Path

from app.build_handover import modified_files_since, render_handover

SINCE = 1_000_000.0


def touch(path: Path, when: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    os.utime(path, (when, when))


def test_lists_only_files_changed_since_and_skips_private_dirs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    touch(root / "old.py", SINCE - 10)
    touch(root / "src" / "new.py", SINCE + 10)
    for skipped in (".delibra/x", ".git/x", "envs/lib/x.py"):
        touch(root / skipped, SINCE + 10)

    assert modified_files_since(root, SINCE) == ["src/new.py"]


def test_does_not_follow_symlinked_directories(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    touch(tmp_path / "outside" / "secret.py", SINCE + 10)
    (root / "linked").symlink_to(tmp_path / "outside")

    assert modified_files_since(root, SINCE) == []


def test_caps_the_list(tmp_path: Path) -> None:
    root = tmp_path / "project"
    for index in range(5):
        touch(root / f"f{index}.py", SINCE + 10)

    result = modified_files_since(root, SINCE, limit=3)
    assert len(result) == 4
    assert set(result[:3]) <= {f"f{i}.py" for i in range(5)}
    assert result[-1] == "... (truncated)"


def test_render_handover_contains_name_reason_output_and_files() -> None:
    text = render_handover("Alpha", "quota", b"half done", ["src/new.py"]).decode()
    assert "Alpha could not finish its turn (quota)" in text
    assert "half done" in text
    assert "- src/new.py" in text
    assert "- none" in render_handover("Alpha", "quota", b"", []).decode()


def test_skips_git_at_every_depth_but_envs_and_delibra_only_at_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    for path in (
        "packages/lib/.git/objects/x",
        "src/envs/tool.py",
        "src/.delibra/note.md",
        "envs/lib/x.py",
        ".delibra/x",
    ):
        touch(root / path, SINCE + 10)

    assert modified_files_since(root, SINCE) == ["src/.delibra/note.md", "src/envs/tool.py"]


def test_stops_walking_after_the_entry_budget(tmp_path: Path) -> None:
    root = tmp_path / "project"
    for index in range(10):
        touch(root / f"old{index}.py", SINCE - 10)
    touch(root / "zz.py", SINCE + 10)

    result = modified_files_since(root, SINCE, max_entries=5)
    assert result[-1] == "... (scan stopped after 5 entries)"
    assert set(result[:-1]) <= {"zz.py"}


def test_directory_only_tree_respects_the_entry_budget(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for index in range(10):
        (root / f"dir{index}").mkdir()

    assert modified_files_since(root, SINCE, max_entries=5) == [
        "... (scan stopped after 5 entries)",
    ]
