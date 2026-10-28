"""Project-wide questions Auto agents ask the user, and the user's answers.

Agents never write the store. Each writes only its own outbox, which Delibra
harvests after a completed turn. ``questions-history.json`` is the source of
truth; ``questions.json`` is the view agents read, regenerated on every write.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import stat
from typing import Any

from app.storage import (
    ConflictError,
    NotFoundError,
    StorageError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_json_recovery_pair,
    first_unsafe_writable_root_character,
    load_json_recover,
    utc_now,
)


QUESTIONS_DIR = "questions"
OUTBOX_NAME = "outbox.json"
OUTBOX_MAX_BYTES = 192 * 1024
OUTBOX_MAX_QUESTIONS = 25
QUESTION_MAX_CHARS = 2_000
ASSUMPTION_MAX_CHARS = 2_000
ANSWER_MAX_CHARS = 20_000
QUESTION_ID = re.compile(r"Q[1-9][0-9]{0,8}")
_OUTBOX_KEYS = frozenset({"question", "assumption", "blocking"})
VIEW_COPY_NAME = "questions.json"
LOGGER = logging.getLogger(__name__)


class OutboxError(ValueError):
    """An agent's outbox broke its contract; the turn keeps its other results."""


@dataclass(frozen=True)
class ProposedQuestion:
    question: str
    assumption: str
    blocking: bool


def parse_outbox(data: bytes) -> list[ProposedQuestion]:
    """Validate one agent's outbox, rejecting anything outside the schema."""

    if len(data) > OUTBOX_MAX_BYTES:
        raise OutboxError(f"outbox is larger than {OUTBOX_MAX_BYTES} bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OutboxError("outbox is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"questions"}:
        raise OutboxError('outbox must be an object whose only key is "questions"')
    items = payload["questions"]
    if not isinstance(items, list):
        raise OutboxError('"questions" must be a list')
    if len(items) > OUTBOX_MAX_QUESTIONS:
        raise OutboxError(f"outbox has more than {OUTBOX_MAX_QUESTIONS} questions")
    return [_proposal(index, item) for index, item in enumerate(items, start=1)]


def _proposal(index: int, item: Any) -> ProposedQuestion:
    if not isinstance(item, dict):
        raise OutboxError(f"question {index} must be an object")
    unknown = sorted(set(item) - _OUTBOX_KEYS)
    if unknown:
        raise OutboxError(f"question {index} has unknown keys: {', '.join(unknown)}")
    question = item.get("question")
    assumption = item.get("assumption", "")
    blocking = item.get("blocking", False)
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > QUESTION_MAX_CHARS
        or "\x00" in question
    ):
        raise OutboxError(
            f"question {index} needs 1 to {QUESTION_MAX_CHARS} characters of text"
        )
    if (
        not isinstance(assumption, str)
        or len(assumption) > ASSUMPTION_MAX_CHARS
        or "\x00" in assumption
    ):
        raise OutboxError(
            f"question {index} assumption must be text of at most "
            f"{ASSUMPTION_MAX_CHARS} characters"
        )
    if not isinstance(blocking, bool):
        raise OutboxError(f"question {index} blocking must be true or false")
    return ProposedQuestion(question.strip(), assumption.strip(), blocking)


HISTORY_FORMAT = "delibra-questions/1"
VIEW_FORMAT = "delibra-questions-view/1"
QUESTION_STATUSES = frozenset({"open", "answered", "dismissed", "duplicate"})
VISIBLE_STATUSES = frozenset({"open", "answered"})
TURN_OUTCOMES = frozenset({"failed", "stopped"})


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    return None if data.get(key) is None else _text(data, key)


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    # ``type(...) is not int`` and not ``isinstance``: ``True`` is an ``int``.
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    return None if data.get(key) is None else _int(data, key)


def _bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


@dataclass(frozen=True)
class AnswerRevision:
    revision: int
    text: str | None
    at: str

    @classmethod
    def from_dict(cls, data: Any) -> "AnswerRevision":
        if not isinstance(data, dict):
            raise ValueError("answer revision must be an object")
        revision = _int(data, "revision")
        if revision < 1:
            raise ValueError("answer revision number is invalid")
        return cls(revision, _optional_text(data, "text"), _text(data, "at"))

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "text": self.text, "at": self.at}


@dataclass
class Question:
    id: str
    asked_by: str
    asked_by_name: str
    auto_run: int | None
    cycle: int | None
    round: int
    asked_at: str
    question: str
    assumption: str
    blocking: bool
    status: str = "open"
    duplicate_of: str | None = None
    answers: list[AnswerRevision] = field(default_factory=list)
    # How the turn that asked this ended, when it did not complete.
    turn_outcome: str | None = None

    @property
    def number(self) -> int:
        return int(self.id[1:])

    @property
    def latest(self) -> AnswerRevision | None:
        return self.answers[-1] if self.answers else None

    @property
    def answer(self) -> str | None:
        latest = self.latest
        return latest.text if latest is not None else None

    @classmethod
    def from_dict(cls, data: Any) -> "Question":
        if not isinstance(data, dict):
            raise ValueError("question must be an object")
        question = cls(
            id=_text(data, "id"),
            asked_by=_text(data, "asked_by"),
            asked_by_name=_text(data, "asked_by_name"),
            auto_run=_optional_int(data, "auto_run"),
            cycle=_optional_int(data, "cycle"),
            round=_int(data, "round"),
            asked_at=_text(data, "asked_at"),
            question=_text(data, "question"),
            assumption=_text(data, "assumption"),
            blocking=_bool(data, "blocking"),
            status=_text(data, "status"),
            duplicate_of=_optional_text(data, "duplicate_of"),
            answers=[AnswerRevision.from_dict(item) for item in _list(data, "answers")],
            turn_outcome=_optional_text(data, "turn_outcome"),
        )
        if not QUESTION_ID.fullmatch(question.id):
            raise ValueError("question id is invalid")
        if question.status not in QUESTION_STATUSES:
            raise ValueError("question status is invalid")
        if question.turn_outcome is not None and question.turn_outcome not in TURN_OUTCOMES:
            raise ValueError("question turn outcome is invalid")
        return question

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "asked_by": self.asked_by,
            "asked_by_name": self.asked_by_name,
            "auto_run": self.auto_run,
            "cycle": self.cycle,
            "round": self.round,
            "asked_at": self.asked_at,
            "question": self.question,
            "assumption": self.assumption,
            "blocking": self.blocking,
            "status": self.status,
            "duplicate_of": self.duplicate_of,
            "answers": [item.to_dict() for item in self.answers],
        }
        # Written only when set, so existing histories stay byte-identical.
        if self.turn_outcome is not None:
            data["turn_outcome"] = self.turn_outcome
        return data


class QuestionsViewWriteError(StorageError):
    def __init__(self, saved: Sequence[Question]) -> None:
        super().__init__("questions were saved but the agent view write failed")
        self.saved = tuple(saved)


@dataclass
class QuestionBook:
    next_id: int = 1
    questions: list[Question] = field(default_factory=list)

    def find(self, question_id: str) -> Question:
        for question in self.questions:
            if question.id == question_id:
                return question
        raise NotFoundError(f"question {question_id} not found")

    @classmethod
    def from_dict(cls, data: Any) -> "QuestionBook":
        if not isinstance(data, dict) or data.get("format") != HISTORY_FORMAT:
            raise ValueError("format is not delibra-questions/1")
        book = cls(
            next_id=_int(data, "next_id"),
            questions=[Question.from_dict(item) for item in _list(data, "questions")],
        )
        ids = [question.id for question in book.questions]
        if len(ids) != len(set(ids)) or any(
            question.number >= book.next_id for question in book.questions
        ):
            raise ValueError("question ids are inconsistent")
        return book

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": HISTORY_FORMAT,
            "next_id": self.next_id,
            "questions": [question.to_dict() for question in self.questions],
        }


@dataclass(frozen=True)
class QuestionView:
    data: bytes
    sha256: str
    # "Q7@2": a question id and the answer revision the view carries for it.
    seen: tuple[str, ...]
    omitted: frozenset[str]


def _view_entry(question: Question) -> dict[str, Any]:
    latest = question.latest
    return {
        "id": question.id,
        "asked_by": question.asked_by_name,
        "question": question.question,
        "assumption": question.assumption,
        "blocking": question.blocking,
        "status": question.status,
        "answer": latest.text if latest is not None else None,
        "answer_revision": latest.revision if latest is not None else 0,
    }


def render_view(book: QuestionBook, limit: int) -> QuestionView:
    """The agent-facing view: open and answered questions, latest answer only.

    Over the byte limit the oldest answered questions leave the view first,
    then the oldest open ones; the history keeps every one of them.
    """

    shown = sorted(
        (question for question in book.questions if question.status in VISIBLE_STATUSES),
        key=lambda question: question.number,
        reverse=True,
    )
    oldest_first = shown[::-1]
    drop_order = [q.id for q in oldest_first if q.status == "answered"] + [
        q.id for q in oldest_first if q.status == "open"
    ]
    def encode(drop_count: int) -> tuple[bytes, list[dict[str, Any]]]:
        omitted = set(drop_order[:drop_count])
        entries = [_view_entry(question) for question in shown if question.id not in omitted]
        data = (
            json.dumps(
                {"format": VIEW_FORMAT, "omitted": drop_count, "questions": entries},
                ensure_ascii=False,
                indent=1,
            )
            + "\n"
        ).encode("utf-8")
        return data, entries

    low, high = 0, len(drop_order)
    while low < high:
        middle = (low + high) // 2
        candidate, _ = encode(middle)
        if len(candidate) <= limit:
            high = middle
        else:
            low = middle + 1
    data, entries = encode(low)
    return QuestionView(
        data,
        sha256(data).hexdigest(),
        tuple(f"{entry['id']}@{entry['answer_revision']}" for entry in entries),
        frozenset(drop_order[:low]),
    )


class QuestionStore:
    """The project's question files. Every caller that writes holds the project lock."""

    def __init__(self, root: Path, view_limit: int) -> None:
        self.history_path = root / "questions-history.json"
        self.view_path = root / "questions.json"
        self.view_limit = view_limit

    def load(self) -> QuestionBook:
        backup = self.history_path.with_name(f"{self.history_path.name}.bak")
        if not self.history_path.exists() and not backup.exists():
            return QuestionBook()
        try:
            return QuestionBook.from_dict(load_json_recover(self.history_path))
        except ValueError as exc:
            raise StorageError(f"questions history is invalid: {exc}") from exc

    def refresh_view(self) -> QuestionView:
        """Rewrite the agent view from the history and describe what it holds."""

        view = render_view(self.load(), self.view_limit)
        atomic_write_bytes(self.view_path, view.data)
        return view

    def _save(self, book: QuestionBook, *, replace_recovery: bool = False) -> None:
        # History first: a crash between the writes leaves a stale view, which
        # the next refresh_view rewrites, never a view the history lacks.
        writer = atomic_write_json_recovery_pair if replace_recovery else atomic_write_json
        writer(self.history_path, book.to_dict())
        atomic_write_bytes(self.view_path, render_view(book, self.view_limit).data)

    def _change(
        self,
        question_id: str,
        change: Callable[[QuestionBook, Question], None],
    ) -> Question:
        book = self.load()
        question = book.find(question_id)
        change(book, question)
        self._save(book)
        return question

    def add(
        self,
        proposals: Sequence[ProposedQuestion],
        *,
        asked_by: str,
        asked_by_name: str,
        auto_run: int | None,
        cycle: int | None,
        round_n: int,
    ) -> list[Question]:
        book = self.load()
        asked_at = utc_now()
        added: list[Question] = []
        for proposal in proposals:
            question = Question(
                id=f"Q{book.next_id}",
                asked_by=asked_by,
                asked_by_name=asked_by_name,
                auto_run=auto_run,
                cycle=cycle,
                round=round_n,
                asked_at=asked_at,
                question=proposal.question,
                assumption=proposal.assumption,
                blocking=proposal.blocking,
            )
            book.next_id += 1
            book.questions.append(question)
            added.append(question)
        if added:
            atomic_write_json(self.history_path, book.to_dict())
            try:
                atomic_write_bytes(self.view_path, render_view(book, self.view_limit).data)
            except StorageError as exc:
                raise QuestionsViewWriteError(added) from exc
        return added

    def set_turn_outcome(self, question_ids: Sequence[str], outcome: str) -> None:
        """Label questions from a failed or stopped turn; missing ids are skipped."""

        if outcome not in TURN_OUTCOMES:
            raise ValueError("question turn outcome is invalid")
        book = self.load()
        wanted = set(question_ids)
        changed = False
        for question in book.questions:
            if question.id in wanted and question.turn_outcome != outcome:
                question.turn_outcome = outcome
                changed = True
        if changed:
            self._save(book)

    @staticmethod
    def _require_visible(question: Question, action: str) -> None:
        if question.status not in VISIBLE_STATUSES:
            raise ConflictError(f"restore {question.id} before you {action} it")

    @staticmethod
    def _append_answer(question: Question, text: str | None) -> None:
        question.answers.append(AnswerRevision(len(question.answers) + 1, text, utc_now()))
        question.status = "answered" if text is not None else "open"

    def answer(self, question_id: str, text: str) -> Question:
        text = text.strip()
        if not text or len(text) > ANSWER_MAX_CHARS or "\x00" in text:
            raise StorageError(f"an answer must be 1 to {ANSWER_MAX_CHARS} characters")

        def change(_book: QuestionBook, question: Question) -> None:
            self._require_visible(question, "answer")
            self._append_answer(question, text)

        return self._change(question_id, change)

    def clear_answer(self, question_id: str) -> Question:
        def change(_book: QuestionBook, question: Question) -> None:
            self._require_visible(question, "clear")
            if question.answer is not None:
                self._append_answer(question, None)

        return self._change(question_id, change)

    def dismiss(self, question_id: str) -> Question:
        def change(_book: QuestionBook, question: Question) -> None:
            self._require_visible(question, "dismiss")
            question.status = "dismissed"

        return self._change(question_id, change)

    def mark_duplicate(self, question_id: str, original_id: str) -> Question:
        def change(book: QuestionBook, question: Question) -> None:
            self._require_visible(question, "mark")
            if original_id == question_id:
                raise StorageError("a question cannot duplicate itself")
            try:
                book.find(original_id)
            except NotFoundError:
                raise StorageError(f"{original_id} does not exist") from None
            question.status = "duplicate"
            question.duplicate_of = original_id

        return self._change(question_id, change)

    def restore(self, question_id: str) -> Question:
        def change(_book: QuestionBook, question: Question) -> None:
            if question.status in VISIBLE_STATUSES:
                raise ConflictError(f"{question.id} is not dismissed")
            question.status = "answered" if question.answer is not None else "open"
            question.duplicate_of = None

        return self._change(question_id, change)

    def delete(self, question_id: str) -> None:
        book = self.load()
        book.questions.remove(book.find(question_id))
        self._save(book, replace_recovery=True)


# O_NONBLOCK: an agent can plant a FIFO, and a blocking open would hang the
# post-turn block that holds the project lock.
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _open_directory(directory: Path) -> int:
    try:
        return os.open(directory, _OPEN_FLAGS | os.O_DIRECTORY)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OutboxError("questions directory is not a real directory") from exc


def _remove_entry(descriptor: int, name: str) -> None:
    pending: list[tuple[int, str, int | None]] = [(descriptor, name, None)]
    opened_descriptors: set[int] = set()
    try:
        while pending:
            parent, entry, opened = pending.pop()
            if opened is not None:
                os.close(opened)
                opened_descriptors.remove(opened)
                os.rmdir(entry, dir_fd=parent)
                continue
            try:
                info = os.stat(entry, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(entry, dir_fd=parent)
                continue
            # Agents can leave any level unreadable. Repair each directory
            # through its parent descriptor without following symlinks.
            os.chmod(entry, 0o700, dir_fd=parent, follow_symlinks=False)
            child = os.open(entry, _OPEN_FLAGS | os.O_DIRECTORY, dir_fd=parent)
            opened_descriptors.add(child)
            pending.append((parent, entry, child))
            with os.scandir(child) as entries:
                pending.extend((child, item.name, None) for item in entries)
    finally:
        for opened in opened_descriptors:
            os.close(opened)


def prepare_outbox(root: Path, extra_names: Sequence[str] = ()) -> Path:
    """Prepare a previous agent's questions directory for this turn.

    ``extra_names`` are other per-turn files the directory holds beside the
    outbox; a previous turn's copy of each is removed too.
    """
    root_descriptor = _open_directory(root)
    try:
        try:
            info = os.stat(QUESTIONS_DIR, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            os.unlink(QUESTIONS_DIR, dir_fd=root_descriptor)
            info = None
        if info is None:
            os.mkdir(QUESTIONS_DIR, mode=0o700, dir_fd=root_descriptor)
        os.chmod(QUESTIONS_DIR, 0o700, dir_fd=root_descriptor, follow_symlinks=False)
        descriptor = os.open(
            QUESTIONS_DIR, _OPEN_FLAGS | os.O_DIRECTORY, dir_fd=root_descriptor
        )
        try:
            for name in (OUTBOX_NAME, *extra_names):
                _remove_entry(descriptor, name)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_descriptor)
    return root / QUESTIONS_DIR / OUTBOX_NAME


def _read_outbox(
    descriptor: int, name: str = OUTBOX_NAME, limit: int = OUTBOX_MAX_BYTES
) -> bytes | None:
    try:
        file_descriptor = os.open(name, _OPEN_FLAGS, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OutboxError("outbox is not a regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise OutboxError("outbox is not a regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(file_descriptor)


def take_outbox(
    root: Path, *, name: str = OUTBOX_NAME, limit: int = OUTBOX_MAX_BYTES
) -> bytes | None:
    """Read and remove the agent's outbox; None when it wrote none.

    The read stops one byte past the limit, so parse_outbox rejects an
    oversized file without Delibra ever holding more than that.
    """

    try:
        descriptor = _open_directory(root / QUESTIONS_DIR)
    except FileNotFoundError:
        return None
    try:
        return _read_outbox(descriptor, name, limit)
    finally:
        try:
            _remove_entry(descriptor, name)
        finally:
            os.close(descriptor)


def discard_outbox(root: Path, *, name: str = OUTBOX_NAME) -> None:
    """Drop a failed or cancelled turn's outbox; it never reaches the store."""

    try:
        take_outbox(root, name=name)
    except (OutboxError, OSError):
        LOGGER.warning("could not discard the questions outbox under %s", root, exc_info=True)


def questions_prompt(
    view_path: Path, outbox_path: Path | None, *, pause_enabled: bool
) -> str:
    """Read answers on every turn; ask only when an outbox is available."""

    read_view = (
        f" The user's questions and answers are in {view_path.as_posix()}; read it "
        "before deciding, and again before finishing if you asked or are waiting "
        "on anything. Treat its answers as user decisions."
    )
    if outbox_path is None:
        return read_view
    pause = (
        "unless your turn ends the run by agreement or the cycle limit, the run "
        "then pauses for the user after your turn"
        if pause_enabled
        else "this run does not pause, so continue on your stated assumption"
    )
    return (
        read_view
        + " If you need something "
        "only the user can decide (a preference, access, or a decision outside "
        f"the material), write it to {outbox_path.as_posix()} as JSON: "
        '{"questions": [{"question": "...", "assumption": "...", "blocking": false}]} '
        f"with at most {OUTBOX_MAX_QUESTIONS} questions. State in assumption what "
        "you will proceed on, then continue. Set blocking to true only if no "
        f"reasonable assumption exists; {pause}. Do not ask what the material or "
        "another participant can answer. An open question for the user is not by "
        "itself a reason to write Not converged."
    )


def outbox_rule_expressible(outbox_dir: Path) -> bool:
    """Whether a Claude permission rule can name this outbox directory.

    Claude's build argv grants the outbox with an ``Edit`` rule, and some
    characters cannot appear in one; the adapter and AutoManager both ask
    here, so an outbox is never promised without the rule that allows it.
    """

    return first_unsafe_writable_root_character(outbox_dir.as_posix()) is None
