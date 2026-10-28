"""One Auto turn's live question harvest, against real files and a real store."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.live_questions import SCAN_READS, STORE_UNAVAILABLE, LiveQuestions
from app.questions import (
    QUESTIONS_DIR,
    SPOOL_NAME,
    QuestionStore,
    prepare_outbox,
)
from app.storage import StorageError


def ask(*texts: str, blocking: bool = False) -> str:
    return json.dumps(
        {"questions": [{"question": text, "blocking": blocking} for text in texts]}
    )


def live(tmp_path: Path) -> LiveQuestions:
    root = tmp_path / ".delibra"
    root.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    prepare_outbox(workspace)
    return LiveQuestions(
        QuestionStore(root, 96 * 1024),
        workspace / QUESTIONS_DIR / SPOOL_NAME,
        asked_by="s1",
        asked_by_name="Alpha",
        auto_run=3,
        cycle=2,
        round_n=7,
    )


def write(harvest: LiveQuestions, name: str, text: str, mtime_ns: int | None = None) -> Path:
    path = harvest.spool_dir / name
    path.write_text(text, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_files_are_saved_oldest_first_and_removed(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    write(harvest, "q2.json", ask("Second?"), 2_000)
    write(harvest, "q1.json", ask("First?", "Also first?"), 1_000)

    added = harvest.harvest(final=False)

    assert [question.question for question in added] == [
        "First?", "Also first?", "Second?",
    ]
    assert [question.id for question in harvest.asked] == ["Q1", "Q2", "Q3"]
    first = added[0]
    assert (first.asked_by, first.auto_run, first.cycle, first.round) == ("s1", 3, 2, 7)
    assert list(harvest.spool_dir.iterdir()) == []
    assert harvest.warnings == []


def test_a_half_written_file_waits_mid_turn_and_is_saved_once_complete(
    tmp_path: Path,
) -> None:
    harvest = live(tmp_path)
    path = write(harvest, "q1.json", '{"questions": [{"question": "Hal', 1_000)

    assert harvest.harvest(final=False) == []
    assert path.exists()
    assert harvest.warnings == []

    write(harvest, "q1.json", ask("Half?"), 2_000)
    [question] = harvest.harvest(final=False)
    assert question.question == "Half?"


def test_an_invalid_file_is_removed_with_a_warning_at_turn_end(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    write(harvest, "q1.json", '{"questions": [{"question": "x", "extra": 1}]}')

    assert harvest.harvest(final=False) == []
    assert harvest.harvest(final=True) == []

    assert harvest.warnings == [
        "question file q1.json ignored: question 1 has unknown keys: extra"
    ]
    assert list(harvest.spool_dir.iterdir()) == []


def test_entries_that_are_not_question_files_wait_mid_turn_and_go_at_turn_end(
    tmp_path: Path,
) -> None:
    harvest = live(tmp_path)
    write(harvest, "notes.txt", "x")
    os.mkfifo(harvest.spool_dir / "fifo.json")
    (harvest.spool_dir / "link.json").symlink_to(tmp_path / "secret.json")
    (tmp_path / "secret.json").write_text(ask("Secret?"), encoding="utf-8")

    assert harvest.harvest(final=False) == []
    assert len(list(harvest.spool_dir.iterdir())) == 3
    assert harvest.harvest(final=True) == []

    assert list(harvest.spool_dir.iterdir()) == []
    assert (tmp_path / "secret.json").exists()
    assert harvest.asked == []


def test_a_file_that_would_pass_64_questions_is_rejected_whole(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    write(harvest, "a.json", ask(*(f"q{index}?" for index in range(60))), 1_000)
    write(harvest, "b.json", ask(*(f"r{index}?" for index in range(5))), 2_000)
    write(harvest, "c.json", ask("s?", "t?", "u?", "v?"), 3_000)

    harvest.harvest(final=True)

    assert len(harvest.asked) == 64
    assert harvest.warnings == [
        "question file b.json ignored: the turn would pass 64 questions"
    ]
    assert list(harvest.spool_dir.iterdir()) == []


def test_the_turn_end_harvest_reads_every_file_however_many(tmp_path: Path) -> None:
    # Codex finding 1: no listing cap may leave a valid question unread.
    harvest = live(tmp_path)
    for index in range(300):
        write(harvest, f"junk{index:03}.txt", "x", 1_000)
    write(harvest, "zz.json", ask("Last?"), 2_000)

    [question] = harvest.harvest(final=True)

    assert question.question == "Last?"
    assert list(harvest.spool_dir.iterdir()) == []


def test_a_mid_turn_harvest_reads_at_most_scan_reads_files(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    for index in range(SCAN_READS + 1):
        write(harvest, f"q{index:03}.json", '{"questions": []}', 1_000 + index)

    harvest.harvest(final=False)

    assert [path.name for path in harvest.spool_dir.iterdir()] == [f"q{SCAN_READS:03}.json"]


def test_a_failed_history_write_keeps_the_file_for_the_next_harvest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex finding 2: never remove a file whose questions were not saved.
    harvest = live(tmp_path)
    path = write(harvest, "q1.json", ask("Kept?"))
    real_add = QuestionStore.add

    def failing_add(self, *args, **kwargs):
        raise StorageError("disk full")

    monkeypatch.setattr(QuestionStore, "add", failing_add)
    assert harvest.harvest(final=False) == []
    assert path.exists()
    assert harvest.warnings == [STORE_UNAVAILABLE]

    monkeypatch.setattr(QuestionStore, "add", real_add)
    [question] = harvest.harvest(final=False)
    assert question.question == "Kept?"
    assert not path.exists()


def test_a_failed_removal_never_saves_the_same_file_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex finding 2: saved, then the unlink failed; the next pass must not re-add.
    harvest = live(tmp_path)
    path = write(harvest, "q1.json", ask("Once?"))

    def failing_remove(spool_dir: Path, name: str) -> None:
        raise OSError("busy")

    monkeypatch.setattr("app.live_questions.remove_spool_entry", failing_remove)
    assert len(harvest.harvest(final=False)) == 1
    assert path.exists()

    monkeypatch.undo()
    assert harvest.harvest(final=False) == []
    assert not path.exists()
    assert len(harvest.store.load().questions) == 1


def test_a_reused_name_with_new_content_is_saved(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    write(harvest, "q1.json", ask("First?"))
    harvest.harvest(final=False)
    write(harvest, "q1.json", ask("Second?"))

    [question] = harvest.harvest(final=False)

    assert question.question == "Second?"


def test_a_missing_spool_warns_and_never_raises(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    harvest.spool_dir.rmdir()

    assert harvest.harvest(final=True) == []
    assert harvest.warnings[0].startswith("questions outbox unreadable:")


def test_mark_outcome_labels_every_question_of_the_turn(tmp_path: Path) -> None:
    harvest = live(tmp_path)
    write(harvest, "q1.json", ask("A?"), 1_000)
    harvest.harvest(final=False)
    write(harvest, "q2.json", ask("B?"), 2_000)
    harvest.harvest(final=True)

    harvest.mark_outcome("failed")

    assert [question.turn_outcome for question in harvest.store.load().questions] == [
        "failed", "failed",
    ]
