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

    assert modified_files_since(root, SINCE, limit=3) == [
        "f0.py", "f1.py", "f2.py", "... (truncated)",
    ]


def test_render_handover_contains_name_reason_output_and_files() -> None:
    text = render_handover("Alpha", "quota", b"half done", ["src/new.py"]).decode()
    assert "Alpha could not finish its turn (quota)" in text
    assert "half done" in text
    assert "- src/new.py" in text
    assert "- none" in render_handover("Alpha", "quota", b"", []).decode()
