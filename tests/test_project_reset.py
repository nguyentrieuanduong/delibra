from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from app.coding import CodingEnvState
from app.models import SessionConfig
from app.notes import NoteStore, chat_note_store
from app.project_reset import PartialClearError, clear_project
from app.questions import ProposedQuestion, QuestionStore
from app.storage import ConflictError, ProjectStore, RegistryStore

ALPHA = "1" * 32


def _project(tmp_path: Path) -> tuple[ProjectStore, QuestionStore]:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store = ProjectStore(RegistryStore(tmp_path / "home").register("Reset", project_dir))
    for index, name in enumerate(("Alpha", "Beta")):
        store.create_session(
            SessionConfig(
                id=str(index + 1) * 32, name=name, agent="fake", model="success",
                effort="low", role_instructions="", cli_session_id=f"native-{index}",
                status="idle", created_at="2026-07-19T00:00:00Z", rounds=[],
            )
        )
    (store.rounds_dir(ALPHA) / "round-02.md").write_text("Old round", encoding="utf-8")
    questions = QuestionStore(store.root, 20)
    questions.add(
        [ProposedQuestion("Old?", "", False)],
        asked_by=ALPHA, asked_by_name="Alpha", auto_run=1, cycle=1, round_n=1,
    )
    NoteStore(store.root).add("Old note", auto_run=1)
    chat_note_store(store, ALPHA).add("Chat note", name="Alpha")
    store.set_use_global_instruction(False)
    return store, questions


def test_history_archives_then_clears_and_keeps_agents_settings_and_numbers(
    tmp_path: Path, reserve_auto_run
) -> None:
    store, questions = _project(tmp_path)
    reserve_auto_run(store, auto_id="f" * 32)
    store.clear_auto_reservation("f" * 32)

    archive = clear_project(store, "history", view_limit=20)

    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
    assert {
        "auto-runs/1/config.json",
        "questions-history.json",
        "notes-history.json",
        "sessions/Alpha/config.json",
        "sessions/Alpha/rounds/round-02.md",
        "sessions/Alpha/notes-history.json",
    } <= names
    assert "manifest.json" not in names
    assert store.list_auto_runs() == []
    assert store.reserve_auto_run_number() == 2
    assert questions.load().questions == [] and questions.load().next_id == 2
    notes = NoteStore(store.root).load()
    assert notes.notes == [] and notes.next_id == 2
    assert chat_note_store(store, ALPHA).load().notes == []
    alpha = store.load_session(ALPHA)
    assert alpha.cli_session_id is None
    assert store.allocate_round(ALPHA) == 3
    assert sorted(session.name for session in store.list_sessions()) == ["Alpha", "Beta"]
    assert store.use_global_instruction() is False


def test_agents_level_also_removes_every_agent(tmp_path: Path) -> None:
    store, _ = _project(tmp_path)

    archive = clear_project(store, "agents", view_limit=20)

    assert store.list_sessions() == []
    assert list(store.sessions_root.iterdir()) == []
    with zipfile.ZipFile(archive) as zipped:
        assert "sessions/Beta/config.json" in zipped.namelist()
    assert store.use_global_instruction() is False


def test_full_reset_also_archives_and_resets_settings(tmp_path: Path) -> None:
    store, _ = _project(tmp_path)

    archive = clear_project(store, "full", view_limit=20)

    assert store.list_sessions() == []
    assert store.use_global_instruction() is True
    with zipfile.ZipFile(archive) as zipped:
        assert json.loads(zipped.read("manifest.json"))["use_global_instruction"] is False


def test_full_reset_refuses_before_archiving_while_the_env_is_created(tmp_path: Path) -> None:
    store, questions = _project(tmp_path)
    store.set_coding_env(CodingEnvState(status="creating", updated_at="t"))

    with pytest.raises(ConflictError):
        clear_project(store, "full", view_limit=20)

    assert not (store.root / "archive").exists()
    assert len(questions.load().questions) == 1


def test_a_removal_failure_names_the_archive(tmp_path: Path, monkeypatch) -> None:
    store, _ = _project(tmp_path)

    def broken(_self, _session_id: str) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(ProjectStore, "delete_session", broken)

    with pytest.raises(PartialClearError) as caught:
        clear_project(store, "agents", view_limit=20)

    assert caught.value.archive.exists()
    assert str(caught.value).startswith(
        f"Partly cleared; everything removed is saved in {caught.value.archive.name}:"
    )
