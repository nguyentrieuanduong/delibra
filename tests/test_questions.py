"""Unit tests for the Auto questions store, view, outbox and prompt."""

from hashlib import sha256
import json
import os
from pathlib import Path
import random

import pytest

from app.questions import (
    OUTBOX_MAX_BYTES,
    OUTBOX_MAX_QUESTIONS,
    OUTBOX_NAME,
    QUESTIONS_DIR,
    OutboxError,
    ProposedQuestion,
    Question,
    QuestionBook,
    QuestionStore,
    discard_outbox,
    outbox_rule_expressible,
    parse_outbox,
    prepare_outbox,
    questions_prompt,
    render_view,
    take_outbox,
)
from app.storage import ConflictError, NotFoundError, StorageError


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


def store(tmp_path: Path, limit: int = 96 * 1024) -> QuestionStore:
    root = tmp_path / ".delibra"
    root.mkdir(exist_ok=True)
    return QuestionStore(root, limit)


def ask(questions: QuestionStore, *texts: str, blocking: bool = False):
    return questions.add(
        [ProposedQuestion(text, f"assume {text}", blocking) for text in texts],
        asked_by="a" * 32,
        asked_by_name="Alpha",
        auto_run=3,
        cycle=2,
        round_n=14,
    )


def view(questions: QuestionStore) -> dict:
    return json.loads(questions.view_path.read_bytes())


def test_a_missing_history_is_an_empty_book(tmp_path: Path) -> None:
    book = store(tmp_path).load()

    assert (book.next_id, book.questions) == (1, [])


def test_add_assigns_ids_records_provenance_and_writes_both_files(tmp_path: Path) -> None:
    questions = store(tmp_path)

    added = ask(questions, "Which database?", "Deploy where?")

    assert [question.id for question in added] == ["Q1", "Q2"]
    first = questions.load().questions[0]
    assert (first.asked_by, first.asked_by_name, first.auto_run, first.cycle, first.round) == (
        "a" * 32, "Alpha", 3, 2, 14,
    )
    assert first.status == "open"
    assert [entry["id"] for entry in view(questions)["questions"]] == ["Q2", "Q1"]
    assert view(questions)["format"] == "delibra-questions-view/1"


def test_ids_are_never_reused_after_delete(tmp_path: Path) -> None:
    questions = store(tmp_path)
    ask(questions, "one", "two")

    questions.delete("Q2")
    [added] = ask(questions, "three")

    assert added.id == "Q3"


def test_answers_append_revisions_and_the_view_holds_only_the_latest(tmp_path: Path) -> None:
    questions = store(tmp_path)
    ask(questions, "Which database?")

    questions.answer("Q1", " Postgres ")
    questions.answer("Q1", "SQLite")
    cleared = questions.clear_answer("Q1")
    answered = questions.answer("Q1", "MySQL")

    assert [(item.revision, item.text) for item in answered.answers] == [
        (1, "Postgres"), (2, "SQLite"), (3, None), (4, "MySQL"),
    ]
    assert cleared.status == "open"
    assert answered.status == "answered"
    [entry] = view(questions)["questions"]
    assert (entry["answer"], entry["answer_revision"], entry["status"]) == ("MySQL", 4, "answered")


def test_dismiss_duplicate_and_restore_move_questions_out_of_and_into_the_view(
    tmp_path: Path,
) -> None:
    questions = store(tmp_path)
    ask(questions, "one", "two", "three")
    questions.answer("Q2", "yes")

    questions.dismiss("Q1")
    duplicate = questions.mark_duplicate("Q3", "Q2")

    assert duplicate.duplicate_of == "Q2"
    assert [entry["id"] for entry in view(questions)["questions"]] == ["Q2"]
    assert questions.restore("Q1").status == "open"
    restored = questions.restore("Q3")
    assert (restored.status, restored.duplicate_of) == ("open", None)
    questions.dismiss("Q2")
    assert questions.restore("Q2").status == "answered"


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (lambda q: q.answer("Q1", "late"), ConflictError),
        (lambda q: q.clear_answer("Q1"), ConflictError),
        (lambda q: q.dismiss("Q1"), ConflictError),
        (lambda q: q.mark_duplicate("Q1", "Q2"), ConflictError),
    ],
)
def test_a_dismissed_question_must_be_restored_before_it_changes(
    tmp_path: Path, action, error
) -> None:
    questions = store(tmp_path)
    ask(questions, "one", "two")
    questions.dismiss("Q1")

    with pytest.raises(error):
        action(questions)


def test_invalid_answers_and_references_are_rejected(tmp_path: Path) -> None:
    questions = store(tmp_path)
    ask(questions, "one")

    with pytest.raises(StorageError, match="1 to 20000"):
        questions.answer("Q1", "   ")
    with pytest.raises(StorageError, match="1 to 20000"):
        questions.answer("Q1", "x" * 20_001)
    with pytest.raises(NotFoundError):
        questions.answer("Q9", "yes")
    with pytest.raises(StorageError, match="cannot duplicate itself"):
        questions.mark_duplicate("Q1", "Q1")
    with pytest.raises(StorageError, match="Q9 does not exist"):
        questions.mark_duplicate("Q1", "Q9")


def test_render_view_drops_oldest_answered_then_oldest_open_over_the_limit(
    tmp_path: Path,
) -> None:
    questions = store(tmp_path)
    ask(questions, *(f"question {index} " + "x" * 400 for index in range(1, 7)))
    for question_id in ("Q1", "Q2", "Q3"):
        questions.answer(question_id, "answer " + "y" * 400)
    book = questions.load()
    full = render_view(book, 1024 * 1024)

    rendered = render_view(book, len(full.data) - 1)
    assert rendered.omitted == frozenset({"Q1"})
    small = render_view(book, 1500)
    assert small.omitted >= {"Q1", "Q2", "Q3"}
    assert len(small.data) <= 1500 or small.omitted == frozenset(f"Q{i}" for i in range(1, 7))
    for kept in json.loads(small.data)["questions"]:
        assert kept["id"] not in small.omitted
    assert json.loads(small.data)["omitted"] == len(small.omitted)


def test_render_view_uses_bounded_serializations_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.questions as questions_module

    book = QuestionBook(
        next_id=1201,
        questions=[
            Question(
                id=f"Q{number}", asked_by="a" * 32, asked_by_name="Alpha",
                auto_run=1, cycle=1, round=1, asked_at="2026-10-18T00:00:00Z",
                question="x" * 120, assumption="", blocking=False,
            )
            for number in range(1, 1201)
        ],
    )
    original_dumps = questions_module.json.dumps
    calls = 0

    def counted_dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(questions_module.json, "dumps", counted_dumps)
    rendered = render_view(book, 96 * 1024)

    assert rendered.omitted
    assert len(rendered.data) <= 96 * 1024
    assert calls <= 16


def test_refresh_view_describes_exactly_the_bytes_it_wrote(tmp_path: Path) -> None:
    questions = store(tmp_path)
    ask(questions, "one", "two")
    questions.answer("Q1", "yes")
    questions.view_path.unlink()

    refreshed = questions.refresh_view()

    assert questions.view_path.read_bytes() == refreshed.data
    assert refreshed.sha256 == sha256(refreshed.data).hexdigest()
    assert refreshed.seen == ("Q2@0", "Q1@1")


def test_turn_start_refresh_repairs_view_after_interrupted_pair_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = store(tmp_path)
    ask(questions, "one")
    stale_view = questions.view_path.read_bytes()

    def fail_view_write(_path: Path, _data: bytes) -> None:
        raise StorageError("injected view write failure")

    monkeypatch.setattr("app.questions.atomic_write_bytes", fail_view_write)
    with pytest.raises(StorageError, match="injected view write failure"):
        questions.answer("Q1", "yes")
    assert questions.load().find("Q1").answer == "yes"
    assert questions.view_path.read_bytes() == stale_view

    monkeypatch.undo()
    refreshed = questions.refresh_view()
    assert questions.view_path.read_bytes() == refreshed.data
    assert json.loads(refreshed.data)["questions"][0]["answer"] == "yes"


def test_a_corrupt_history_without_a_backup_is_a_storage_error(tmp_path: Path) -> None:
    questions = store(tmp_path)
    questions.history_path.write_text('{"format": "other"}', encoding="utf-8")

    with pytest.raises(StorageError, match="questions history is invalid"):
        questions.load()


def test_prepare_outbox_creates_the_directory_and_removes_a_stale_outbox(tmp_path: Path) -> None:
    path = prepare_outbox(tmp_path)
    path.write_text("stale", encoding="utf-8")

    again = prepare_outbox(tmp_path)

    assert again == tmp_path / QUESTIONS_DIR / OUTBOX_NAME
    assert not again.exists()
    assert (tmp_path / QUESTIONS_DIR).stat().st_mode & 0o777 == 0o700


def test_prepare_outbox_replaces_a_planted_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / OUTBOX_NAME).write_text("keep", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / QUESTIONS_DIR).symlink_to(target)

    prepare_outbox(root)

    assert not (root / QUESTIONS_DIR).is_symlink()
    assert (target / OUTBOX_NAME).read_text(encoding="utf-8") == "keep"


def test_take_outbox_reads_then_removes_it(tmp_path: Path) -> None:
    path = prepare_outbox(tmp_path)
    path.write_bytes(b'{"questions": []}')

    assert take_outbox(tmp_path) == b'{"questions": []}'
    assert not path.exists()
    assert take_outbox(tmp_path) is None


def test_take_outbox_without_a_directory_is_none(tmp_path: Path) -> None:
    assert take_outbox(tmp_path) is None


def test_take_outbox_refuses_a_symlinked_outbox_and_keeps_its_target(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text('{"questions": []}', encoding="utf-8")
    (tmp_path / "root").mkdir()
    path = prepare_outbox(tmp_path / "root")
    path.symlink_to(target)

    with pytest.raises(OutboxError, match="not a regular file"):
        take_outbox(tmp_path / "root")
    assert target.exists()
    assert not path.is_symlink()


def test_take_outbox_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    path = prepare_outbox(tmp_path)
    os.mkfifo(path)

    with pytest.raises(OutboxError, match="not a regular file"):
        take_outbox(tmp_path)
    assert not path.exists()


def test_take_outbox_bounds_the_read_one_byte_past_the_limit(tmp_path: Path) -> None:
    path = prepare_outbox(tmp_path)
    path.write_bytes(b" " * (OUTBOX_MAX_BYTES + 10))

    assert len(take_outbox(tmp_path)) == OUTBOX_MAX_BYTES + 1


def test_discard_outbox_removes_a_directory_planted_as_the_outbox(tmp_path: Path) -> None:
    path = prepare_outbox(tmp_path)
    path.mkdir()
    (path / "inner").write_text("x", encoding="utf-8")

    discard_outbox(tmp_path)

    assert not path.exists()


def test_questions_prompt_names_both_paths_and_the_pause_behaviour() -> None:
    view_path = Path("/p/.delibra/questions.json")
    outbox_path = Path("/w/questions/outbox.json")

    pausing = questions_prompt(view_path, outbox_path, pause_enabled=True)
    unattended = questions_prompt(view_path, outbox_path, pause_enabled=False)

    for prompt in (pausing, unattended):
        assert "/p/.delibra/questions.json" in prompt
        assert "/w/questions/outbox.json" in prompt
        assert "at most 25 questions" in prompt
        assert "not by itself a reason to write Not converged" in prompt
    assert "unless your turn ends the run by agreement or the cycle limit" in pausing
    assert "the run then pauses for the user after your turn" in pausing
    assert "this run does not pause" in unattended


def test_questions_prompt_without_an_outbox_still_names_the_answer_view() -> None:
    prompt = questions_prompt(
        Path("/p/.delibra/questions.json"), None, pause_enabled=True
    )

    assert "/p/.delibra/questions.json" in prompt
    assert "read it before deciding" in prompt
    assert "write it to" not in prompt
    assert "blocking" not in prompt


def test_outbox_rule_expressible_rejects_paths_a_permission_rule_cannot_name() -> None:
    assert outbox_rule_expressible(Path("/home/.delibra/build-tmp/a/questions"))
    assert not outbox_rule_expressible(Path("/home/de,libra/build-tmp/a/questions"))
    assert not outbox_rule_expressible(Path("/home/x[1]/questions"))
