"""Project-wide questions Auto agents ask the user, and the user's answers.

Agents never write the store. Each writes only its own outbox, which Delibra
harvests after a completed turn. ``questions-history.json`` is the source of
truth; ``questions.json`` is the view agents read, regenerated on every write.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


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
