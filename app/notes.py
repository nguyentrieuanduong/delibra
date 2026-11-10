"""Notes the user sends to the agents of an Auto run.

``notes-history.json`` is the source of truth; ``notes.json`` is the view
agents read, regenerated on every write. A note leaves the view once every
available participant has completed a turn whose turn-start view held it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING, Any

from app.coding import open_without_symlinks
from app.questions import OutboxError
from app.storage import (
    ConflictError,
    NotFoundError,
    StorageError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_json_recovery_pair,
    load_json_recover,
    utc_now,
)


if TYPE_CHECKING:
    from app.storage import ProjectStore

HISTORY_NAME = "notes-history.json"
VIEW_NAME = "notes.json"
HISTORY_FORMAT = "delibra-notes/1"
VIEW_FORMAT = "delibra-notes-view/1"
NOTE_MAX_CHARS = 20_000
MAX_PENDING_NOTES = 25
NOTE_ID = re.compile(r"N[1-9][0-9]{0,8}")
NOTE_STATUSES = frozenset({"pending", "delivered", "deleted", "undelivered"})
# An agent's read marks live beside its questions outbox, in their own file
# so a bad mark never costs the turn its questions.
MARKS_NAME = "notes-read.json"
MARKS_MAX_BYTES = 4 * 1024
MARKS_MAX_IDS = 100


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    # ``type(...) is not int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _ids(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of text")
    return list(value)


@dataclass
class Note:
    id: str
    auto_run: int
    posted_at: str
    text: str
    status: str = "pending"
    delivered_to: list[str] = field(default_factory=list)
    marked_by: list[str] = field(default_factory=list)
    closed_at: str | None = None

    @property
    def number(self) -> int:
        return int(self.id[1:])

    def close(self, status: str, at: str) -> None:
        self.status = status
        self.closed_at = at

    @classmethod
    def from_dict(cls, data: Any) -> "Note":
        if not isinstance(data, dict):
            raise ValueError("note must be an object")
        note = cls(
            id=_text(data, "id"),
            auto_run=_int(data, "auto_run"),
            posted_at=_text(data, "posted_at"),
            text=_text(data, "text"),
            status=_text(data, "status"),
            delivered_to=_ids(data, "delivered_to"),
            marked_by=_ids(data, "marked_by"),
            closed_at=None if data.get("closed_at") is None else _text(data, "closed_at"),
        )
        if not NOTE_ID.fullmatch(note.id):
            raise ValueError("note id is invalid")
        if note.status not in NOTE_STATUSES:
            raise ValueError("note status is invalid")
        return note

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "auto_run": self.auto_run,
            "posted_at": self.posted_at,
            "text": self.text,
            "status": self.status,
            "delivered_to": list(self.delivered_to),
            "marked_by": list(self.marked_by),
            "closed_at": self.closed_at,
        }


@dataclass
class NoteBook:
    next_id: int = 1
    notes: list[Note] = field(default_factory=list)

    def find(self, note_id: str) -> Note:
        for note in self.notes:
            if note.id == note_id:
                return note
        raise NotFoundError(f"note {note_id} not found")

    @property
    def revision(self) -> int:
        """Grows with every change to any note and never shrinks, so two
        renderings of the list can be ordered: ids issued, deliveries, marks
        and closures each add one."""

        return self.next_id + sum(
            len(note.delivered_to) + len(note.marked_by) + (note.status != "pending")
            for note in self.notes
        )

    def pending(self, auto_run: int) -> list[Note]:
        return [
            note
            for note in self.notes
            if note.status == "pending" and note.auto_run == auto_run
        ]

    @classmethod
    def from_dict(cls, data: Any) -> "NoteBook":
        if not isinstance(data, dict) or data.get("format") != HISTORY_FORMAT:
            raise ValueError(f"format is not {HISTORY_FORMAT}")
        notes = data.get("notes")
        if not isinstance(notes, list):
            raise ValueError("notes must be a list")
        book = cls(next_id=_int(data, "next_id"), notes=[Note.from_dict(item) for item in notes])
        ids = [note.id for note in book.notes]
        if len(ids) != len(set(ids)) or any(note.number >= book.next_id for note in book.notes):
            raise ValueError("note ids are inconsistent")
        return book

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": HISTORY_FORMAT,
            "next_id": self.next_id,
            "notes": [note.to_dict() for note in self.notes],
        }


@dataclass(frozen=True)
class NoteView:
    data: bytes
    sha256: str
    seen: tuple[str, ...]


@dataclass(frozen=True)
class NoteCredit:
    # Pending notes of the run a turn's marks credited, and marked ids that
    # name no note of the run at all.
    marked: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()


def parse_marks(data: bytes) -> list[str]:
    """Validate an agent's read marks, rejecting anything outside the schema."""

    if len(data) > MARKS_MAX_BYTES:
        raise OutboxError(f"read marks are larger than {MARKS_MAX_BYTES} bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OutboxError("read marks are not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"read"}:
        raise OutboxError('read marks must be an object whose only key is "read"')
    ids = payload["read"]
    if not isinstance(ids, list) or len(ids) > MARKS_MAX_IDS:
        raise OutboxError(f'"read" must be a list of at most {MARKS_MAX_IDS} note ids')
    if not all(isinstance(item, str) and NOTE_ID.fullmatch(item) for item in ids):
        raise OutboxError('"read" must hold note ids such as "N1"')
    return list(dict.fromkeys(ids))


def render_view(book: NoteBook, auto_run: int) -> NoteView:
    """The agent-facing view: this run's pending notes, oldest first."""

    shown = sorted(book.pending(auto_run), key=lambda note: note.number)
    data = (
        json.dumps(
            {
                "format": VIEW_FORMAT,
                "notes": [
                    {"id": note.id, "posted_at": note.posted_at, "text": note.text}
                    for note in shown
                ],
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    ).encode("utf-8")
    return NoteView(data, sha256(data).hexdigest(), tuple(note.id for note in shown))


class NoteStore:
    """The project's note files. Every caller that writes holds the project lock."""

    def __init__(self, root: Path, *, view_path: Path | None = None) -> None:
        self.history_path = root / HISTORY_NAME
        self.view_path = view_path if view_path is not None else root / VIEW_NAME

    def load(self) -> NoteBook:
        backup = self.history_path.with_name(f"{self.history_path.name}.bak")
        if not self.history_path.exists() and not backup.exists():
            return NoteBook()
        try:
            return NoteBook.from_dict(load_json_recover(self.history_path))
        except ValueError as exc:
            raise StorageError(f"notes history is invalid: {exc}") from exc

    def refresh_view(self, auto_run: int) -> NoteView:
        view = render_view(self.load(), auto_run)
        atomic_write_bytes(self.view_path, view.data)
        return view

    def _save(self, book: NoteBook, auto_run: int) -> None:
        # History first: a crash between the writes leaves a stale view,
        # which the next refresh_view rewrites, never a view the history lacks.
        atomic_write_json(self.history_path, book.to_dict())
        atomic_write_bytes(self.view_path, render_view(book, auto_run).data)

    def add(self, text: str, *, auto_run: int, undelivered: bool = False) -> Note:
        """``undelivered`` keeps a note for a run that can never read it."""

        text = text.strip()
        if not text or len(text) > NOTE_MAX_CHARS or "\x00" in text:
            raise StorageError(f"a note must be 1 to {NOTE_MAX_CHARS} characters")
        book = self.load()
        if not undelivered and len(book.pending(auto_run)) >= MAX_PENDING_NOTES:
            raise ConflictError(
                f"Auto {auto_run} already has {MAX_PENDING_NOTES} pending notes"
            )
        note = Note(f"N{book.next_id}", auto_run, utc_now(), text)
        if undelivered:
            note.close("undelivered", note.posted_at)
        book.next_id += 1
        book.notes.append(note)
        self._save(book, auto_run)
        return note

    def delete(self, note_id: str) -> Note:
        book = self.load()
        note = book.find(note_id)
        if note.status != "pending":
            raise ConflictError(f"{note.id} is already {note.status}")
        note.close("deleted", utc_now())
        self._save(book, note.auto_run)
        return note

    def clear(self) -> int:
        """Delete every note; ids are never reused (turn records cite them)."""

        book = self.load()
        count = len(book.notes)
        book.notes = []
        # Replace the backup too, or recovery could resurrect the notes.
        atomic_write_json_recovery_pair(self.history_path, book.to_dict())
        atomic_write_bytes(self.view_path, render_view(book, 0).data)
        return count

    def record_delivery(
        self,
        auto_run: int,
        session_id: str | None,
        seen: Collection[str],
        required: Collection[str],
        *,
        marked: Collection[str] = (),
    ) -> NoteCredit:
        """Credit a completed turn with the notes its turn-start view held or
        it marked read, then retire every note all ``required`` participants
        have received. ``session_id`` None only re-checks retirement."""

        book = self.load()
        closed_at = utc_now()
        run_ids = {note.id for note in book.notes if note.auto_run == auto_run}
        credited: list[str] = []
        for note in book.pending(auto_run):
            if session_id is not None:
                if note.id in marked:
                    credited.append(note.id)
                    if session_id not in note.marked_by:
                        note.marked_by.append(session_id)
                if (note.id in seen or note.id in marked) and session_id not in note.delivered_to:
                    note.delivered_to.append(session_id)
            if required and set(required) <= set(note.delivered_to):
                note.close("delivered", closed_at)
        self._save(book, auto_run)
        return NoteCredit(
            tuple(credited),
            tuple(item for item in marked if item not in run_ids),
        )

    def abandon_before(self, auto_run: int) -> None:
        """A new run starts: earlier runs' pending notes never reach it."""

        book = self.load()
        closed_at = utc_now()
        for note in book.notes:
            if note.status == "pending" and note.auto_run < auto_run:
                note.close("undelivered", closed_at)
        self._save(book, auto_run)


def notes_prompt(
    view_path: Path,
    *,
    live: bool,
    marks_path: Path | None,
    sent: str = "during this run",
    overrides: str = "the topic",
) -> str:
    """Name the notes view; promise mid-turn notes only when the file is live,
    and ask for read marks only when the turn has somewhere to write them."""

    reading = (
        " The user can add a note at any time, including while you are working, "
        "so read the file at the start of your turn, again before each significant "
        "decision, and again before finishing."
        if live
        else " This copy was taken when your turn started; notes the user adds "
        "during your turn reach your next one."
    )
    marking = (
        f" After you read a note, record it in {marks_path.as_posix()} as JSON "
        '{"read": ["N1"]}, listing every note id you read this turn.'
        if marks_path is not None
        else ""
    )
    return (
        f" Notes the user sent {sent} are in {view_path.as_posix()}."
        + reading
        + marking
        + f" Treat its notes as user direction that overrides {overrides} where they conflict."
    )


# Notes to a running chat reply (design 2026-11-02): one agent's own history,
# with the view and read marks in its workspace, which a chat turn can read
# and write. Every chat note carries this run number.
CHAT_NOTES_RUN = 0
CHAT_VIEW_NAME = "delibra-notes.json"
CHAT_MARKS_NAME = "delibra-notes-read.json"
CHAT_INTENT_NAME = "notes-credit.json"


def _read_marks(path: Path) -> bytes | None:
    """The agent's marks, opened without following a symlink anywhere."""

    try:
        descriptor = open_without_symlinks(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OutboxError(f"read marks are unavailable: {exc}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OutboxError("read marks are not a regular file")
        return handle.read(MARKS_MAX_BYTES + 1)


class ChatNoteStore:
    """One agent's notes to its running chat reply. Writers hold that session's lock.

    A completed reply is credited in two steps around saving its round: the
    credit intent is written first (beside the history, where the agent cannot
    write), applied once the round is saved, and reconciled at the next turn
    start if the process stopped in between (Codex follow-up, 2026-11-02).
    """

    def __init__(self, session_dir: Path, workspace: Path) -> None:
        self._store = NoteStore(session_dir, view_path=workspace / CHAT_VIEW_NAME)
        self.view_path = workspace / CHAT_VIEW_NAME
        self.marks_path = workspace / CHAT_MARKS_NAME
        self.intent_path = session_dir / CHAT_INTENT_NAME

    def load(self) -> NoteBook:
        return self._store.load()

    def add(self, text: str, *, name: str) -> Note:
        if len(self.load().pending(CHAT_NOTES_RUN)) >= MAX_PENDING_NOTES:
            raise ConflictError(f"{name} already has {MAX_PENDING_NOTES} pending notes")
        return self._store.add(text, auto_run=CHAT_NOTES_RUN)

    def delete(self, note_id: str) -> Note:
        return self._store.delete(note_id)

    def clear(self) -> int:
        self.intent_path.unlink(missing_ok=True)
        return self._store.clear()

    def turn_start(
        self, session_id: str, round_status: Callable[[int], str | None]
    ) -> NoteView:
        """The view a chat turn starts with, after settling an earlier reply's
        credit; stale marks from an earlier turn go."""

        self.reconcile(session_id, round_status)
        self.marks_path.unlink(missing_ok=True)
        return self._store.refresh_view(CHAT_NOTES_RUN)

    def read_credit(self) -> tuple[list[str], list[str]]:
        """The ids the reply marked read, and the warnings they earn. Reads only."""

        warnings: list[str] = []
        marked: list[str] = []
        try:
            data = _read_marks(self.marks_path)
            if data is not None:
                marked = parse_marks(data)
        except OutboxError as exc:
            warnings.append(f"Note read marks ignored: {exc}")
        known = {note.id for note in self.load().notes}
        unknown = [item for item in marked if item not in known]
        if unknown:
            warnings.append(f"Note read marks name unknown notes: {', '.join(unknown)}")
        return marked, warnings

    def apply_credit(
        self, session_id: str, seen: Collection[str], marked: Collection[str]
    ) -> None:
        """Credit a reply whose completion is already saved."""

        self._store.record_delivery(
            CHAT_NOTES_RUN, session_id, seen, [session_id], marked=marked
        )
        self.marks_path.unlink(missing_ok=True)

    def record_intent(
        self, round_n: int, seen: Collection[str], marked: Collection[str]
    ) -> None:
        """Before the reply's completion is saved: what to credit if it is."""

        atomic_write_json(
            self.intent_path,
            {"round": round_n, "seen": list(seen), "marked": list(marked)},
        )

    def apply_intent(self, session_id: str) -> None:
        """Credit the saved intent and remove it."""

        intent = self._load_intent()
        if intent is not None:
            self.apply_credit(session_id, intent["seen"], intent["marked"])
        self.intent_path.unlink(missing_ok=True)

    def reconcile(
        self, session_id: str, round_status: Callable[[int], str | None]
    ) -> None:
        """Credit an intent whose round was saved complete; drop any other."""

        intent = self._load_intent()
        if intent is not None and round_status(intent["round"]) == "complete":
            self.apply_credit(session_id, intent["seen"], intent["marked"])
        self.intent_path.unlink(missing_ok=True)

    def _load_intent(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.intent_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None  # an unreadable intent credits nothing rather than guess
        if (
            not isinstance(data, dict)
            or type(data.get("round")) is not int
            or not isinstance(data.get("seen"), list)
            or not isinstance(data.get("marked"), list)
            or not all(isinstance(item, str) for item in data["seen"] + data["marked"])
        ):
            return None
        return data


def chat_note_store(store: "ProjectStore", session_id: str) -> ChatNoteStore:
    return ChatNoteStore(store.session_dir(session_id), store.workspace_dir(session_id))
