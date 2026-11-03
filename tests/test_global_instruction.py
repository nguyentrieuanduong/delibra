from __future__ import annotations

from pathlib import Path

import pytest

from app.global_instruction import (
    GLOBAL_INSTRUCTION_MAX_BYTES,
    GlobalInstructionError,
    read_global_instruction,
    save_global_instruction,
)
from app.storage import StorageError


def test_no_file_means_no_instruction(tmp_path: Path) -> None:
    assert read_global_instruction(tmp_path) == ""


def test_save_then_read_round_trips_with_lf_line_ends(tmp_path: Path) -> None:
    saved = save_global_instruction(tmp_path, "Be concise.\r\nCite sources.\r\n")

    assert saved == "Be concise.\nCite sources.\n"
    assert read_global_instruction(tmp_path) == saved
    assert (tmp_path / "instructions.md").stat().st_mode & 0o777 == 0o600


def test_blank_text_removes_the_file(tmp_path: Path) -> None:
    save_global_instruction(tmp_path, "Be concise.")

    assert save_global_instruction(tmp_path, "  \r\n") == ""
    assert not (tmp_path / "instructions.md").exists()


def test_exactly_64_kb_is_accepted(tmp_path: Path) -> None:
    text = "a" * GLOBAL_INSTRUCTION_MAX_BYTES

    assert save_global_instruction(tmp_path, text) == text


def test_over_64_kb_is_refused_and_keeps_the_old_text(tmp_path: Path) -> None:
    save_global_instruction(tmp_path, "old")

    # 32769 two-byte characters: 65538 bytes, under the character count a box allows.
    with pytest.raises(GlobalInstructionError, match="^The global instruction is larger than 64 KB$"):
        save_global_instruction(tmp_path, "é" * 32769)
    assert read_global_instruction(tmp_path) == "old"


@pytest.mark.parametrize(
    "contents,message",
    [
        (b"a" * (GLOBAL_INSTRUCTION_MAX_BYTES + 1), "^The global instruction is larger than 64 KB$"),
        (b"\xff\xfe", "^The global instruction is not valid UTF-8$"),
    ],
)
def test_a_hand_edited_file_is_checked_on_read(tmp_path: Path, contents: bytes, message: str) -> None:
    (tmp_path / "instructions.md").write_bytes(contents)

    with pytest.raises(GlobalInstructionError, match=message):
        read_global_instruction(tmp_path)


def test_a_whitespace_only_file_is_no_instruction(tmp_path: Path) -> None:
    (tmp_path / "instructions.md").write_text("\n \n")

    assert read_global_instruction(tmp_path) == ""


def test_its_errors_are_storage_errors() -> None:
    # The app's StorageError handler turns a refused turn into a visible message.
    assert issubclass(GlobalInstructionError, StorageError)
