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
from pathlib import Path
import re
from typing import Any

from app.storage import (
    ConflictError,
    NotFoundError,
    StorageError,
    atomic_write_bytes,
    atomic_write_json,
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
        )
        if not QUESTION_ID.fullmatch(question.id):
            raise ValueError("question id is invalid")
        if question.status not in QUESTION_STATUSES:
            raise ValueError("question status is invalid")
        return question

    def to_dict(self) -> dict[str, Any]:
        return {
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

    def _save(self, book: QuestionBook) -> None:
        # History first: a crash between the writes leaves a stale view, which
        # the next refresh_view rewrites, never a view the history lacks.
        atomic_write_json(self.history_path, book.to_dict())
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
            self._save(book)
        return added

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
        self._save(book)
