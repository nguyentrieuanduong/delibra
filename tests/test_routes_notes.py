from pathlib import Path
import re
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.notes import NoteStore
from app.storage import ProjectStore, RegistryStore
from app.views import auto_notes_context, chat_notes_context
from tests.test_auto import seed_durable_auto


def notes_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("Notes", project_dir)
    store = ProjectStore(project)
    for index, name in enumerate(("Alpha", "Beta"), start=1):
        store.create_session(
            SessionConfig(
                id=str(index) * 32,
                name=name,
                agent="claude",
                model="fake",
                effort="low",
                role_instructions="",
                cli_session_id=None,
                status="idle",
                created_at="2026-10-26T00:00:00Z",
                rounds=[],
            )
        )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    return app, project, store


def base(project) -> str:
    return f"/projects/{quote(project.name, safe='')}"


def notes_textarea(html: str) -> str:
    match = re.search(r"<textarea[^>]*data-auto-notes-input[^>]*>", html)
    assert match is not None
    return match[0]


def test_a_note_posts_to_a_paused_run_escaped(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="a" * 32, status="stopped", publish=False)

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(f"{base(project)}/auto-notes", data={"text": "<b>Prefer</b> SQLite"})

    assert response.status_code == 200
    assert 'id="auto-notes-list"' in response.text
    assert "&lt;b&gt;Prefer&lt;/b&gt; SQLite" in response.text
    assert "delivered to 0/2" in response.text
    assert "marked read by" not in response.text
    [note] = NoteStore(store.root).load().notes
    assert (note.auto_run, note.status) == (record.number, "pending")


def test_notes_are_refused_only_without_any_auto_run(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        without_run = client.post(f"{base(project)}/auto-notes", data={"text": "x"})
        seed_durable_auto(store, auto_id="b" * 32, status="converged", publish=False)
        blank = client.post(f"{base(project)}/auto-notes", data={"text": "   "})

    assert without_run.status_code == 409
    assert blank.status_code == 422
    assert NoteStore(store.root).load().notes == []


def test_a_note_to_an_ended_run_waits_for_continue_auto(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="b" * 32, status="converged", publish=False)

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")
        posted = client.post(f"{base(project)}/auto-notes", data={"text": "After the end"})

    assert 'data-auto-notes-open="true"' in page.text
    assert "disabled" not in notes_textarea(page.text)
    assert "or on Continue Auto once it ends" in re.sub(r"\s+", " ", page.text)
    assert posted.status_code == 200
    text = re.sub(r"\s+", " ", posted.text)
    assert "N1 · pending · delivered to 0/2" in text
    assert '<span class="auto-note-waiting">waits for Continue Auto</span>' in text
    [note] = NoteStore(store.root).load().notes
    assert (note.auto_run, note.status) == (record.number, "pending")


def test_a_note_to_a_run_that_cannot_continue_is_kept_undelivered(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="b" * 32, status="converged", publish=False)
    # A discussion cursor with no discussion turn: Continue Auto cannot rebuild it.
    broken = store.load_auto_run(record.id)
    broken.current_cycle = 1
    store.save_auto_run(broken)

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")
        posted = client.post(f"{base(project)}/auto-notes", data={"text": "Too late"})

    assert "disabled" not in notes_textarea(page.text)
    # No refusal: the note is kept, and marked as never reaching an agent.
    assert posted.status_code == 200
    text = re.sub(r"\s+", " ", posted.text)
    assert '<li class="auto-note undelivered">' in text
    assert "N1 · undelivered" in text
    assert "/auto-notes/N1/delete" not in text
    [note] = NoteStore(store.root).load().notes
    assert (note.auto_run, note.status, note.text) == (record.number, "undelivered", "Too late")


def test_only_an_ended_run_marks_its_pending_notes_waiting(tmp_path: Path) -> None:
    _app, _project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="b" * 32, status="discussing", publish=False)

    running = auto_notes_context(store, record)
    record.status = "stopped"
    paused = auto_notes_context(store, record)

    assert (running["notes_open"], running["notes_waiting"]) == (True, False)
    assert (paused["notes_open"], paused["notes_waiting"]) == (True, True)
    assert auto_notes_context(store, None)["notes_open"] is False


def test_the_list_names_agents_that_marked_a_note(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="e" * 32, status="stopped", publish=False)
    notes = NoteStore(store.root)
    notes.add("Prefer SQLite", auto_run=record.number)
    notes.record_delivery(record.number, "1" * 32, [], {"1" * 32, "2" * 32}, marked=["N1"])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(f"{base(project)}/auto-notes", data={"text": "Second"})

    text = re.sub(r"\s+", " ", response.text)
    assert "N1 · pending · delivered to 1/2 · marked read by Alpha" in text


def test_delete_marks_a_pending_note_deleted(tmp_path: Path) -> None:

    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="c" * 32, status="stopped", publish=False)
    NoteStore(store.root).add("Prefer SQLite", auto_run=record.number)

    with TestClient(app, base_url="http://localhost") as client:
        deleted = client.post(f"{base(project)}/auto-notes/N1/delete")
        again = client.post(f"{base(project)}/auto-notes/N1/delete")
        unknown = client.post(f"{base(project)}/auto-notes/zz/delete")

    assert deleted.status_code == 200
    assert "N1 · deleted" in re.sub(r"\s+", " ", deleted.text)
    assert "/auto-notes/N1/delete" not in deleted.text
    assert again.status_code == 409
    assert unknown.status_code == 404
    assert NoteStore(store.root).load().notes[0].status == "deleted"


def test_the_chat_page_puts_an_open_notes_box_under_the_composer(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    seed_durable_auto(store, auto_id="d" * 32, status="stopped", publish=False)

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")

    assert page.status_code == 200
    # The composer's column in the top row, before the Auto panel beside it.
    order = [
        page.text.index(marker)
        for marker in (
            "data-conversation-top",
            'class="conversation-input"',
            'id="chat-composer"',
            'id="auto-notes"',
            'id="auto-panel"',
        )
    ]
    assert order == sorted(order)
    assert 'rows="1"' in notes_textarea(page.text)
    assert 'aria-describedby="auto-note-hint"' in notes_textarea(page.text)
    assert 'aria-label="Send note to agents"' in page.text
    assert 'data-auto-notes-open="true"' in page.text
    assert "disabled" not in notes_textarea(page.text)


def test_the_notes_box_is_disabled_without_an_open_run(tmp_path: Path) -> None:
    app, project, _store = notes_app(tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")

    assert "disabled" in notes_textarea(page.text)


def test_an_ended_run_warns_about_undelivered_notes_and_queues_new_ones(
    tmp_path: Path,
) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="f" * 32, status="converged", publish=False)
    notes = NoteStore(store.root)
    notes.add("Late", auto_run=record.number)
    notes.record_delivery(record.number, "1" * 32, ["N1"], {"1" * 32, "2" * 32})

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")
        setup = client.get(f"{base(project)}/auto/setup")
        queued = client.post(f"{base(project)}/auto-notes", data={"text": "x"})

    page_text = re.sub(r"\s+", " ", page.text)
    assert "Notes not delivered:</strong> N1 (not yet to Beta)." in page_text
    assert "Continue Auto delivers them" in page_text
    setup_text = re.sub(r"\s+", " ", setup.text)
    assert (
        f"Auto {record.number} still has notes its agents have not all received: N1."
        in setup_text
    )
    assert queued.status_code == 200
    assert [note.status for note in NoteStore(store.root).load().notes] == ["pending", "pending"]


def test_lists_carry_the_store_revision(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    seed_durable_auto(store, auto_id="9" * 32, status="stopped", publish=False)

    with TestClient(app, base_url="http://localhost") as client:
        posted = client.post(f"{base(project)}/auto-notes", data={"text": "Prefer SQLite"})
        status = client.get(f"{base(project)}/auto-runs/1")

    revision = NoteStore(store.root).load().revision
    marker = f'id="auto-notes-list" class="auto-notes-list" data-notes-revision="{revision}"'
    assert marker in posted.text
    assert marker in status.text


def test_only_this_machine_may_steer_a_build_run_with_a_note(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="8" * 32, status="stopped", publish=False)
    build = store.load_auto_run(record.id)
    build.kind = "build"
    store.save_auto_run(build)

    # Build agents have a shell, and a note overrides the topic: the same
    # loopback rule as starting or continuing a build run.
    with TestClient(app, base_url="http://localhost", client=("192.168.1.20", 5000)) as lan:
        refused = lan.post(f"{base(project)}/auto-notes", data={"text": "Run this"})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as local:
        accepted = local.post(f"{base(project)}/auto-notes", data={"text": "Run this"})

    assert refused.status_code == 403
    assert accepted.status_code == 200
    assert [note.text for note in NoteStore(store.root).load().notes] == ["Run this"]


def test_a_lan_client_may_still_note_a_discussion_run(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    seed_durable_auto(store, auto_id="7" * 32, status="stopped", publish=False)

    with TestClient(app, base_url="http://localhost", client=("192.168.1.20", 5000)) as lan:
        posted = lan.post(f"{base(project)}/auto-notes", data={"text": "Prefer SQLite"})

    assert posted.status_code == 200


from app.notes import chat_note_store  # noqa: E402

ALPHA = "1" * 32


def test_a_chat_note_reaches_the_replying_agent(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        app.state.manager.replying = lambda project_id, session_id: session_id == ALPHA
        response = client.post(
            f"{base(project)}/auto-notes",
            data={"text": "<i>Short</i>", "target": "chat", "session_id": ALPHA},
        )

    assert response.status_code == 200
    assert 'id="chat-notes-list"' in response.text
    assert f'data-session-id="{ALPHA}"' in response.text
    assert "&lt;i&gt;Short&lt;/i&gt;" in response.text
    [note] = chat_note_store(store, ALPHA).load().notes
    assert (note.text, note.status) == ("<i>Short</i>", "pending")
    assert NoteStore(store.root).load().notes == []


def test_a_chat_note_to_an_idle_agent_is_refused(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    seed_durable_auto(store, auto_id="a" * 32, status="stopped", publish=False)
    with TestClient(app, base_url="http://localhost") as client:
        app.state.manager.replying = lambda project_id, session_id: False
        response = client.post(
            f"{base(project)}/auto-notes", data={"text": "x", "target": "chat", "session_id": ALPHA}
        )
        missing = client.post(
            f"{base(project)}/auto-notes", data={"text": "x", "target": "chat", "session_id": "9" * 32}
        )

    assert response.status_code == 409
    assert "Alpha is not replying; send a message instead" in response.text
    assert missing.status_code == 404
    assert chat_note_store(store, ALPHA).load().notes == []
    assert NoteStore(store.root).load().notes == []


def test_a_pending_chat_note_can_be_deleted_and_reset_clears_chat_notes(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    chat_note_store(store, ALPHA).add("one", name="Alpha")
    chat_note_store(store, "2" * 32).add("two", name="Beta")
    with TestClient(app, base_url="http://localhost") as client:
        deleted = client.post(f"{base(project)}/sessions/{ALPHA}/chat-notes/N1/delete")
        reset = client.post(f"{base(project)}/reset", data={"scope": "notes"}, follow_redirects=False)

    assert deleted.status_code == 200
    assert "N1 · deleted" in deleted.text
    assert reset.status_code == 303
    assert chat_note_store(store, ALPHA).load().notes == []
    assert chat_note_store(store, "2" * 32).load().notes == []


def test_after_auto_ends_an_idle_agent_box_sends_to_the_run_as_when_paused(
    tmp_path: Path,
) -> None:
    app, project, store = notes_app(tmp_path)
    seed_durable_auto(store, auto_id="b" * 32, status="converged", publish=False)
    chat_note_store(store, ALPHA).add("Earlier note", name="Alpha")
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat?agent={ALPHA}").text

    chat_list = re.search(r'<div id="chat-notes-list"[^>]*>', page)[0]
    auto_list = re.search(r'<div id="auto-notes-list"[^>]*>', page)[0]
    # An ended run takes notes for Continue Auto, as a paused run does.
    assert " hidden" in chat_list and "Earlier note" in page
    assert " hidden" not in auto_list
    assert " disabled" not in notes_textarea(page)
    assert 'name="target" value="auto"' in page


def test_a_replying_agent_gets_the_chat_target_on_the_page(tmp_path: Path) -> None:
    from app.models import RoundRecord, SourceDescriptor

    app, project, store = notes_app(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        # After startup: recovery would mark a round with no live process as failed.
        config = store.load_session(ALPHA)
        config.status = "running"
        config.rounds.append(
            RoundRecord(
                n=1, status="running", error=None, warnings=[], agent="claude", model="fake",
                effort="low", started_at="2026-11-02T00:00:00Z", finished_at=None,
                source=SourceDescriptor(type="user"),
            )
        )
        store.save_session(config)
        page = client.get(f"{base(project)}/chat?agent={ALPHA}").text

    assert 'name="target" value="chat"' in page
    assert 'aria-describedby="chat-note-hint"' in notes_textarea(page)
    assert " disabled" not in notes_textarea(page)
    assert 'data-agent-replying="true"' in page


def test_the_notes_list_sits_in_a_disclosure_with_a_count(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="d" * 32, status="stopped", publish=False)

    with TestClient(app, base_url="http://localhost") as client:
        empty = client.get(f"{base(project)}/chat").text
        notes = NoteStore(store.root)
        notes.add("First", auto_run=record.number)
        notes.add("Second", auto_run=record.number)
        notes.delete("N1")
        page = client.get(f"{base(project)}/chat").text

    disclosure = re.search(r"<details[^>]*data-notes-disclosure[^>]*>", empty)[0]
    # Nothing to collapse yet.
    assert " hidden" in disclosure
    disclosure = re.search(r"<details[^>]*data-notes-disclosure[^>]*>", page)[0]
    assert " open" in disclosure and " hidden" not in disclosure
    text = re.sub(r"\s+", " ", page)
    assert "<summary>Notes<span data-notes-summary-count> · 2 (1 pending)</span></summary>" in text
    # Both lists live inside it, so a collapsed section hides either one.
    inner = page[page.index("data-notes-disclosure"):page.index("</details>", page.index("data-notes-disclosure"))]
    assert 'id="auto-notes-list"' in inner and 'id="chat-notes-list"' in inner


BETA = "2" * 32


def _mark_running(store: ProjectStore, session_id: str) -> None:
    session = store.load_session(session_id)
    session.status = "running"
    store.save_session(session)


def test_an_unread_auto_note_is_edited_while_every_agent_is_idle(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="c" * 32, status="stopped", publish=False)
    NoteStore(store.root).add("Prefer SQLite", auto_run=record.number)
    with TestClient(app, base_url="http://localhost") as client:
        edited = client.post(
            f"{base(project)}/auto-notes/N1/edit", data={"text": "<b>Prefer</b> Postgres"}
        )
        unknown = client.post(f"{base(project)}/auto-notes/zz/edit", data={"text": "x"})
        missing = client.post(f"{base(project)}/auto-notes/N9/edit", data={"text": "x"})

    assert edited.status_code == 200
    assert 'id="auto-notes-list"' in edited.text
    assert 'hx-swap-oob="outerHTML"' in edited.text
    assert "&lt;b&gt;Prefer&lt;/b&gt; Postgres" in edited.text
    assert unknown.status_code == 404
    assert missing.status_code == 404
    assert NoteStore(store.root).load().find("N1").text == "<b>Prefer</b> Postgres"


def test_a_note_edit_is_refused_while_any_agent_works_in_memory(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="c" * 32, status="stopped", publish=False)
    NoteStore(store.root).add("Prefer SQLite", auto_run=record.number)
    chat_note_store(store, ALPHA).add("one", name="Alpha")
    with TestClient(app, base_url="http://localhost") as client:
        app.state.manager.has_active_project = lambda project_id: True
        auto = client.post(f"{base(project)}/auto-notes/N1/edit", data={"text": "x"})
        chat = client.post(
            f"{base(project)}/sessions/{ALPHA}/chat-notes/N1/edit", data={"text": "x"}
        )

    for response in (auto, chat):
        assert response.status_code == 409
        assert "Notes can be edited only while no agent is running" in response.text
    assert NoteStore(store.root).load().find("N1").text == "Prefer SQLite"
    assert chat_note_store(store, ALPHA).load().find("N1").text == "one"


def test_a_note_edit_is_refused_while_another_agent_is_running(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    chat_note_store(store, ALPHA).add("one", name="Alpha")
    with TestClient(app, base_url="http://localhost") as client:
        _mark_running(store, BETA)
        response = client.post(
            f"{base(project)}/sessions/{ALPHA}/chat-notes/N1/edit", data={"text": "x"}
        )

    assert response.status_code == 409
    assert "Notes can be edited only while no agent is running" in response.text
    assert chat_note_store(store, ALPHA).load().find("N1").text == "one"


def test_an_unread_chat_note_is_edited_and_a_reached_one_is_refused(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    notes = chat_note_store(store, ALPHA)
    notes.add("one", name="Alpha")
    notes.add("two", name="Alpha")
    notes.apply_credit(ALPHA, ["N2"], [])  # N2 reached Alpha and retired
    with TestClient(app, base_url="http://localhost") as client:
        edited = client.post(
            f"{base(project)}/sessions/{ALPHA}/chat-notes/N1/edit", data={"text": "uno"}
        )
        closed = client.post(
            f"{base(project)}/sessions/{ALPHA}/chat-notes/N2/edit", data={"text": "dos"}
        )
        stranger = client.post(
            f"{base(project)}/sessions/{'9' * 32}/chat-notes/N1/edit", data={"text": "x"}
        )

    assert edited.status_code == 200
    assert 'id="chat-notes-list"' in edited.text
    assert "uno" in edited.text
    assert closed.status_code == 409
    assert "N2 is already delivered" in closed.text
    assert stranger.status_code == 404


def test_notes_are_editable_only_while_the_project_is_idle(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="c" * 32, status="stopped", publish=False)
    assert auto_notes_context(store, record)["notes_edit_open"] is True
    assert chat_notes_context(
        store, store.load_session(ALPHA), auto_active=False, auto_open=True
    )["chat_notes_edit_open"] is True
    _mark_running(store, BETA)
    assert auto_notes_context(store, record)["notes_edit_open"] is False
    assert chat_notes_context(
        store, store.load_session(ALPHA), auto_active=False, auto_open=True
    )["chat_notes_edit_open"] is False


def test_only_this_machine_may_edit_a_build_run_note(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)
    record = seed_durable_auto(store, auto_id="8" * 32, status="stopped", publish=False)
    build = store.load_auto_run(record.id)
    build.kind = "build"
    store.save_auto_run(build)
    NoteStore(store.root).add("Run tests", auto_run=record.number)

    with TestClient(app, base_url="http://localhost", client=("192.168.1.20", 5000)) as lan:
        refused = lan.post(f"{base(project)}/auto-notes/N1/edit", data={"text": "Run this"})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as local:
        accepted = local.post(f"{base(project)}/auto-notes/N1/edit", data={"text": "Run lint"})

    assert refused.status_code == 403
    assert accepted.status_code == 200
    assert NoteStore(store.root).load().find("N1").text == "Run lint"
