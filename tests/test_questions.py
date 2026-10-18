"""Unit tests for the Auto questions store, view, outbox and prompt."""

import json
import random

import pytest

from app.questions import (
    OUTBOX_MAX_BYTES,
    OUTBOX_MAX_QUESTIONS,
    OutboxError,
    ProposedQuestion,
    parse_outbox,
)


def outbox(*questions: object) -> bytes:
    return json.dumps({"questions": list(questions)}).encode("utf-8")


def test_parse_outbox_strips_text_and_defaults_optional_keys() -> None:
    parsed = parse_outbox(outbox(
        {"question": "  Which database?  ", "assumption": " SQLite ", "blocking": True},
        {"question": "Deploy target?"},
    ))

    assert parsed == [
        ProposedQuestion("Which database?", "SQLite", True),
        ProposedQuestion("Deploy target?", "", False),
    ]


def test_parse_outbox_accepts_an_empty_list() -> None:
    assert parse_outbox(outbox()) == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\xff", "not valid UTF-8 JSON"),
        (b"{", "not valid UTF-8 JSON"),
        (b"[]", 'only key is "questions"'),
        (b'{"questions": [], "extra": 1}', 'only key is "questions"'),
        (b'{"questions": {}}', '"questions" must be a list'),
        (b'{"questions": ["text"]}', "question 1 must be an object"),
        (b'{"questions": [{"question": "q", "answer": "a"}]}', "unknown keys: answer"),
        (b'{"questions": [{"question": "   "}]}', "question 1 needs 1 to 2000"),
        (b'{"questions": [{"question": 5}]}', "question 1 needs 1 to 2000"),
        (b'{"questions": [{"question": "q", "assumption": 1}]}', "assumption must be text"),
        (b'{"questions": [{"question": "q", "blocking": 1}]}', "blocking must be true or false"),
        (b'{"questions": [{"question": "a\\u0000b"}]}', "question 1 needs 1 to 2000"),
        (b"[" * 100_000, "not valid UTF-8 JSON"),
    ],
)
def test_parse_outbox_rejects_anything_outside_the_schema(data: bytes, message: str) -> None:
    with pytest.raises(OutboxError, match=message):
        parse_outbox(data)


def test_parse_outbox_enforces_the_question_and_size_limits() -> None:
    with pytest.raises(OutboxError, match="more than 25 questions"):
        parse_outbox(outbox(*({"question": "q"} for _ in range(OUTBOX_MAX_QUESTIONS + 1))))
    with pytest.raises(OutboxError, match="question 1 needs 1 to 2000"):
        parse_outbox(outbox({"question": "x" * 2001}))
    with pytest.raises(OutboxError, match="larger than 196608 bytes"):
        parse_outbox(b" " * (OUTBOX_MAX_BYTES + 1))


_FUZZ_VALUES: tuple[object, ...] = (
    None, True, False, 0, 1, -1, 2.5, "", " ", "q", "  text  ", "a\x00b", "x" * 2001,
    [], {}, ["q"], {"question": "q"},
)
_FUZZ_KEYS = ("question", "assumption", "blocking", "answer", "")


def _fuzz_item(rng: random.Random) -> object:
    if rng.random() < 0.1:
        return rng.choice(_FUZZ_VALUES)
    return {key: rng.choice(_FUZZ_VALUES) for key in rng.sample(_FUZZ_KEYS, rng.randint(0, 3))}


def _fuzz_payload(rng: random.Random) -> bytes:
    roll = rng.random()
    if roll < 0.15:
        return bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
    if roll < 0.25:
        return json.dumps(rng.choice(_FUZZ_VALUES)).encode("utf-8")
    items = [_fuzz_item(rng) for _ in range(rng.randint(0, OUTBOX_MAX_QUESTIONS + 2))]
    payload: dict[str, object] = {"questions": items}
    if rng.random() < 0.1:
        payload["extra"] = 1
    return json.dumps(payload).encode("utf-8")


def _expected_valid(data: bytes) -> list[ProposedQuestion] | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"questions"}:
        return None
    items = payload["questions"]
    if not isinstance(items, list) or len(items) > OUTBOX_MAX_QUESTIONS:
        return None
    parsed: list[ProposedQuestion] = []
    for item in items:
        if not isinstance(item, dict) or not set(item) <= {"question", "assumption", "blocking"}:
            return None
        question = item.get("question")
        assumption = item.get("assumption", "")
        blocking = item.get("blocking", False)
        if not isinstance(question, str) or not question.strip() or len(question) > 2000 or "\x00" in question:
            return None
        if not isinstance(assumption, str) or len(assumption) > 2000 or "\x00" in assumption:
            return None
        if not isinstance(blocking, bool):
            return None
        parsed.append(ProposedQuestion(question.strip(), assumption.strip(), blocking))
    return parsed


def test_parse_outbox_generated_inputs_parse_or_raise_only_outbox_error() -> None:
    rng = random.Random(20261018)
    valid = 0
    for _ in range(3000):
        data = _fuzz_payload(rng)
        expected = _expected_valid(data)
        if expected is None:
            with pytest.raises(OutboxError):
                parse_outbox(data)
        else:
            valid += 1
            assert parse_outbox(data) == expected
    assert valid > 50
