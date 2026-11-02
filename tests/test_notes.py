import json
from pathlib import Path
import random
import re

import pytest

from app.notes import (
    MARKS_MAX_BYTES,
    MARKS_MAX_IDS,
    MAX_PENDING_NOTES,
    NOTE_MAX_CHARS,
    NoteStore,
    notes_prompt,
    parse_marks,
)
from app.questions import OutboxError
from app.storage import ConflictError, NotFoundError, StorageError


def view(notes: NoteStore) -> dict:
    return json.loads(notes.view_path.read_bytes())


def statuses(notes: NoteStore) -> dict[str, str]:
    return {note.id: note.status for note in notes.load().notes}


def test_a_posted_note_is_pending_and_in_the_view(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    note = notes.add("  Prefer SQLite  ", auto_run=3)
    assert (note.id, note.text, note.auto_run, note.status) == (
        "N1", "Prefer SQLite", 3, "pending",
    )
    assert view(notes)["notes"] == [
        {"id": "N1", "posted_at": note.posted_at, "text": "Prefer SQLite"}
    ]
    assert notes.load().notes[0].to_dict() == note.to_dict()


def test_note_text_is_bounded(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    for text in ("", "   ", "x" * (NOTE_MAX_CHARS + 1), "a\x00b"):
        with pytest.raises(StorageError):
            notes.add(text, auto_run=1)
    assert notes.load().notes == []


def test_pending_notes_per_run_are_capped(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    for index in range(MAX_PENDING_NOTES):
        notes.add(f"note {index}", auto_run=1)
    with pytest.raises(ConflictError):
        notes.add("one too many", auto_run=1)
    notes.delete("N1")
    assert notes.add("room again", auto_run=1).status == "pending"


def test_delivery_needs_every_required_participant(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("first", auto_run=1)
    notes.record_delivery(1, "a", ["N1"], {"a", "b"})
    [note] = notes.load().notes
    assert (note.status, note.delivered_to) == ("pending", ["a"])
    assert [entry["id"] for entry in view(notes)["notes"]] == ["N1"]
    notes.record_delivery(1, "b", ["N1"], {"a", "b"})
    [note] = notes.load().notes
    assert (note.status, note.delivered_to) == ("delivered", ["a", "b"])
    assert note.closed_at is not None
    assert view(notes)["notes"] == []


def test_a_turn_only_delivers_the_notes_its_view_held(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("seen", auto_run=1)
    notes.add("posted mid-turn", auto_run=1)
    notes.record_delivery(1, "a", ["N1"], {"a"})
    assert statuses(notes) == {"N1": "delivered", "N2": "pending"}


def test_losing_a_participant_settles_notes_the_rest_received(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("x", auto_run=1)
    notes.record_delivery(1, "a", ["N1"], {"a", "b"})
    notes.record_delivery(1, None, [], {"a"})
    assert statuses(notes) == {"N1": "delivered"}


def test_no_available_participant_delivers_nothing(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("x", auto_run=1)
    notes.record_delivery(1, None, [], set())
    assert statuses(notes) == {"N1": "pending"}


def test_delete_hides_a_pending_note_and_keeps_its_history(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("x", auto_run=1)
    deleted = notes.delete("N1")
    assert deleted.status == "deleted" and deleted.closed_at is not None
    assert view(notes)["notes"] == []
    assert statuses(notes) == {"N1": "deleted"}
    with pytest.raises(ConflictError):
        notes.delete("N1")
    with pytest.raises(NotFoundError):
        notes.delete("N9")


def test_a_new_run_abandons_earlier_pending_notes(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("old", auto_run=1)
    notes.add("delivered", auto_run=1)
    notes.record_delivery(1, "a", ["N2"], {"a"})
    notes.abandon_before(2)
    assert statuses(notes) == {"N1": "undelivered", "N2": "delivered"}
    assert notes.refresh_view(2).seen == ()


def test_the_view_shows_only_the_given_runs_pending_notes(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("run one", auto_run=1)
    assert notes.refresh_view(2).seen == ()
    shown = notes.refresh_view(1)
    assert shown.seen == ("N1",)
    assert shown.data == notes.view_path.read_bytes()


def test_a_corrupt_history_is_a_storage_error(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.history_path.write_text('{"format": "other"}', encoding="utf-8")
    with pytest.raises(StorageError):
        notes.load()


def test_the_live_prompt_says_notes_can_arrive_mid_turn() -> None:
    prompt = notes_prompt(Path("/p/.delibra/notes.json"), live=True, marks_path=None)
    assert "/p/.delibra/notes.json" in prompt
    assert "at any time, including while you are working" in prompt
    assert "before each significant decision, and again before finishing" in prompt
    assert "overrides the topic where they conflict" in prompt


def test_the_copy_prompt_says_mid_turn_notes_wait_for_the_next_turn() -> None:
    prompt = notes_prompt(Path("/w/questions/notes.json"), live=False, marks_path=None)
    assert "/w/questions/notes.json" in prompt
    assert "at any time" not in prompt
    assert "reach your next one" in prompt
    assert "overrides the topic where they conflict" in prompt


def test_the_prompt_asks_for_marks_only_when_a_marks_path_exists() -> None:
    view = Path("/p/.delibra/notes.json")
    marks = Path("/w/questions/notes-read.json")
    assert "/w/questions/notes-read.json" in notes_prompt(view, live=True, marks_path=marks)
    assert '{"read": ["N1"]}' in notes_prompt(view, live=True, marks_path=marks)
    assert "notes-read.json" not in notes_prompt(view, live=True, marks_path=None)


def test_a_mark_credits_a_note_the_turn_start_view_lacked(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("posted mid-turn", auto_run=1)
    credit = notes.record_delivery(1, "a", [], {"a", "b"}, marked=["N1"])
    [note] = notes.load().notes
    assert credit.marked == ("N1",) and credit.unknown == ()
    assert (note.delivered_to, note.marked_by, note.status) == (["a"], ["a"], "pending")


def test_marks_for_other_runs_or_missing_notes_are_unknown(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("run one", auto_run=1)
    notes.add("run two", auto_run=2)
    credit = notes.record_delivery(2, "a", [], {"a", "b"}, marked=["N1", "N9", "N2"])
    assert credit.marked == ("N2",)
    assert credit.unknown == ("N1", "N9")
    assert statuses(notes) == {"N1": "pending", "N2": "pending"}


def test_marking_an_already_delivered_note_is_neither_credit_nor_unknown(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("x", auto_run=1)
    notes.record_delivery(1, "a", ["N1"], {"a"})
    credit = notes.record_delivery(1, "a", [], {"a"}, marked=["N1"])
    assert credit.marked == () and credit.unknown == ()


def test_parse_marks_accepts_ids_and_drops_duplicates() -> None:
    assert parse_marks(b'{"read": ["N2", "N1", "N2"]}') == ["N2", "N1"]
    assert parse_marks(b'{"read": []}') == []


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b'["N1"]',
        b'{"read": "N1"}',
        b'{"read": ["N1"], "extra": 1}',
        b'{"read": ["Q1"]}',
        b'{"read": [1]}',
        b'\xff',
    ],
)
def test_parse_marks_rejects_anything_outside_the_schema(data: bytes) -> None:
    with pytest.raises(OutboxError):
        parse_marks(data)


def test_parse_marks_enforces_its_limits() -> None:
    too_many = json.dumps({"read": [f"N{i}" for i in range(1, MARKS_MAX_IDS + 2)]}).encode()
    with pytest.raises(OutboxError):
        parse_marks(too_many)
    with pytest.raises(OutboxError):
        parse_marks(b" " * (MARKS_MAX_BYTES + 1))


_FUZZ_IDS = ["N1", "N25", "N999999999", "N0", "N1000000000", "Q1", "n1", "N", "N1 ", "", 1, None, True]


def _fuzz_marks(rng: random.Random) -> bytes:
    if rng.random() < 0.15:
        return bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
    count = rng.randrange(6) if rng.random() < 0.95 else MARKS_MAX_IDS + 1
    ids = [rng.choice(_FUZZ_IDS) for _ in range(count)]
    payload: object = {"read": ids}
    if rng.random() < 0.1:
        payload = {"read": ids, "extra": 1}
    elif rng.random() < 0.1:
        payload = ids
    return json.dumps(payload).encode("utf-8")


def _expected_marks(data: bytes) -> list[str] | None:
    if len(data) > MARKS_MAX_BYTES:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"read"}:
        return None
    ids = payload["read"]
    if not isinstance(ids, list) or len(ids) > MARKS_MAX_IDS:
        return None
    if not all(isinstance(item, str) and re.fullmatch(r"N[1-9][0-9]{0,8}", item) for item in ids):
        return None
    return list(dict.fromkeys(ids))


def test_parse_marks_generated_inputs_parse_or_raise_only_outbox_error() -> None:
    rng = random.Random(20261028)
    valid = 0
    for _ in range(3000):
        data = _fuzz_marks(rng)
        expected = _expected_marks(data)
        if expected is None:
            with pytest.raises(OutboxError):
                parse_marks(data)
        else:
            valid += 1
            assert parse_marks(data) == expected
    assert valid > 50


def test_the_revision_grows_with_every_change_and_only_then(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    seen = [notes.load().revision]

    def changed() -> None:
        revision = notes.load().revision
        assert revision > seen[-1]
        seen.append(revision)

    notes.add("first", auto_run=1)
    changed()
    notes.add("second", auto_run=1)
    changed()
    notes.record_delivery(1, "a", ["N1"], {"a", "b"})
    changed()
    notes.record_delivery(1, "b", [], {"a", "b"}, marked=["N1"])
    changed()  # credit, mark and retirement
    notes.record_delivery(1, None, [], {"a", "b"})
    assert notes.load().revision == seen[-1]  # nothing changed
    notes.delete("N2")
    changed()
    notes.add("third", auto_run=1)
    notes.abandon_before(2)
    changed()


def test_clear_deletes_every_note_but_never_reuses_an_id(tmp_path: Path) -> None:
    notes = NoteStore(tmp_path)
    notes.add("First", auto_run=1)
    notes.add("Second", auto_run=2)

    assert notes.clear() == 2

    assert notes.load().notes == []
    # The recovery backup must not resurrect the cleared notes
    # (atomic_write_json_recovery_pair rewrote it with the cleared book).
    notes.history_path.write_text("{corrupt", encoding="utf-8")
    assert notes.load().notes == []
    assert notes.add("Third", auto_run=3).id == "N3"


from app.notes import CHAT_MARKS_NAME, CHAT_VIEW_NAME, ChatNoteStore, notes_prompt  # noqa: E402

SESSION = "a" * 32


def _chat(tmp_path: Path) -> ChatNoteStore:
    (tmp_path / "s" / "workspace").mkdir(parents=True)
    return ChatNoteStore(tmp_path / "s", tmp_path / "s" / "workspace")


def _start(chat: ChatNoteStore):
    return chat.turn_start(SESSION, lambda n: None)


def _complete(chat: ChatNoteStore, session_id: str, seen) -> list[str]:
    marked, warnings = chat.read_credit()
    chat.apply_credit(session_id, seen, marked)
    return warnings


def test_a_chat_note_is_in_the_view_and_delivered_once_the_turn_saw_it(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    chat.add("Use tables", name="Alpha")
    view = _start(chat)
    assert view.seen == ("N1",)
    shown = json.loads((tmp_path / "s" / "workspace" / CHAT_VIEW_NAME).read_text())
    assert shown["notes"][0]["text"] == "Use tables"

    assert _complete(chat, SESSION, view.seen) == []

    [note] = chat.load().notes
    assert (note.status, note.delivered_to, note.auto_run) == ("delivered", [SESSION], 0)


def test_a_note_added_mid_turn_counts_only_when_marked(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    view = _start(chat)
    chat.add("Shorter", name="Alpha")
    chat.add("Later", name="Alpha")
    (tmp_path / "s" / "workspace" / CHAT_MARKS_NAME).write_text('{"read": ["N1", "N9"]}')

    warnings = _complete(chat, SESSION, view.seen)

    notes = {note.id: note for note in chat.load().notes}
    assert notes["N1"].status == "delivered" and notes["N1"].marked_by == [SESSION]
    assert notes["N2"].status == "pending"
    assert warnings == ["Note read marks name unknown notes: N9"]
    assert not (tmp_path / "s" / "workspace" / CHAT_MARKS_NAME).exists()
    assert _start(chat).seen == ("N2",)


def test_bad_marks_are_a_warning_not_a_failure(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    chat.add("x", name="Alpha")
    (tmp_path / "s" / "workspace" / CHAT_MARKS_NAME).write_text("not json")
    assert _complete(chat, SESSION, ())[0].startswith("Note read marks ignored: ")
    assert chat.load().notes[0].status == "pending"


def test_a_symlinked_marks_file_is_refused(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    chat.add("x", name="Alpha")
    secret = tmp_path / "secret.json"
    secret.write_text('{"read": ["N1"]}')
    (tmp_path / "s" / "workspace" / CHAT_MARKS_NAME).symlink_to(secret)
    assert _complete(chat, SESSION, ())[0].startswith("Note read marks ignored: ")
    assert chat.load().notes[0].status == "pending"


def test_chat_notes_are_limited_like_auto_notes(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    for index in range(25):
        chat.add(f"n{index}", name="Alpha")
    with pytest.raises(ConflictError, match="^Alpha already has 25 pending notes$"):
        chat.add("one more", name="Alpha")


def test_a_saved_intent_is_credited_only_when_its_round_completed(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    chat.add("once", name="Alpha")
    chat.add("twice", name="Alpha")
    chat.record_intent(3, ["N1"], ["N2"])

    chat.reconcile(SESSION, {3: "error"}.get)
    assert not chat.intent_path.exists()
    assert [note.status for note in chat.load().notes] == ["pending", "pending"]

    chat.record_intent(4, ["N1"], ["N2"])
    view = chat.turn_start(SESSION, {4: "complete"}.get)

    assert [note.status for note in chat.load().notes] == ["delivered", "delivered"]
    assert view.seen == ()
    assert not chat.intent_path.exists()


def test_apply_intent_credits_and_removes_it(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    chat.add("x", name="Alpha")
    chat.record_intent(1, ["N1"], [])

    chat.apply_intent(SESSION)

    assert chat.load().notes[0].status == "delivered"
    assert not chat.intent_path.exists()
    chat.apply_intent(SESSION)  # nothing left: a no-op


@pytest.mark.parametrize("body", ["not json", '{"round": "1", "seen": [], "marked": []}', '{"round": 1}', "[]"])
def test_a_corrupt_intent_is_dropped(tmp_path: Path, body: str) -> None:
    chat = _chat(tmp_path)
    chat.add("x", name="Alpha")
    chat.intent_path.write_text(body)

    chat.reconcile(SESSION, lambda n: "complete")

    assert chat.load().notes[0].status == "pending"
    assert not chat.intent_path.exists()


def test_the_intent_lives_outside_the_workspace(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    assert chat.intent_path == tmp_path / "s" / "notes-credit.json"
    assert chat.view_path.parent == chat.marks_path.parent == tmp_path / "s" / "workspace"


def test_the_chat_wording_of_the_notes_prompt() -> None:
    text = notes_prompt(
        Path("/w/delibra-notes.json"), live=True, marks_path=Path("/w/delibra-notes-read.json"),
        sent="to you in this conversation", overrides="the message",
    )
    assert text.startswith(" Notes the user sent to you in this conversation are in /w/delibra-notes.json.")
    assert text.endswith("overrides the message where they conflict.")
    assert notes_prompt(Path("/v"), live=True, marks_path=None).startswith(
        " Notes the user sent during this run are in /v."
    )
