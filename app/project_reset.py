"""The Manage page's Clear-and-reset levels: zip the data, then remove it.

Callers hold registry_project_sessions over the project and every agent and
have checked that no Auto run is active and every agent is idle. Run off the
event loop: zipping and removing take time proportional to the data.
"""

from __future__ import annotations

from pathlib import Path

from app.archive import write_archive
from app.notes import NoteStore, chat_note_store
from app.questions import QuestionStore
from app.storage import ProjectStore, StorageError

CLEAR_SCOPES = ("history", "agents", "full")


class PartialClearError(StorageError):
    """Removal failed after the archive was written; the archive holds it all."""

    def __init__(self, archive: Path, reason: str) -> None:
        super().__init__(
            f"Partly cleared; everything removed is saved in {archive.name}: {reason}"
        )
        self.archive = archive


def _with_backup(path: Path) -> list[Path]:
    return [path, path.with_name(f"{path.name}.bak")]


def clear_project(store: ProjectStore, scope: str, *, view_limit: int) -> Path:
    """Archive, then remove, this level's data. Returns the archive path."""

    if scope not in CLEAR_SCOPES:
        raise StorageError("unknown reset scope")
    store.require_auto_inactive()
    if scope == "full":
        store.require_settings_resettable()
    questions = QuestionStore(store.root, view_limit)
    notes = NoteStore(store.root)
    sessions = store.list_sessions()
    sources = [
        store.auto_runs_root,
        *_with_backup(questions.history_path),
        questions.view_path,
        *_with_backup(notes.history_path),
        notes.view_path,
        *(store.session_dir(session.id) for session in sessions),
    ]
    if scope == "full":
        sources += _with_backup(store.manifest_path)
    archive = write_archive(store.root, scope, sources)
    try:
        store.clear_auto_runs()
        questions.clear()
        notes.clear()
        for session in sessions:
            if scope == "history":
                store.clear_session_history(session.id)
                chat_note_store(store, session.id).clear()
            else:
                store.delete_session(session.id)
        if scope == "full":
            store.reset_settings()
    except (OSError, StorageError) as exc:
        raise PartialClearError(archive, str(exc)) from exc
    return archive
