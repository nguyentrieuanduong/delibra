from pathlib import Path
import re
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.notes import NoteStore
from app.storage import ProjectStore, RegistryStore
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


def test_notes_are_refused_without_a_run_that_accepts_them(tmp_path: Path) -> None:
    app, project, store = notes_app(tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        without_run = client.post(f"{base(project)}/auto-notes", data={"text": "x"})
        seed_durable_auto(store, auto_id="b" * 32, status="converged", publish=False)
        finished = client.post(f"{base(project)}/auto-notes", data={"text": "x"})
        blank = client.post(f"{base(project)}/auto-notes", data={"text": "   "})

    assert without_run.status_code == 409
    assert finished.status_code == 409
    assert blank.status_code == 422
    assert NoteStore(store.root).load().notes == []


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


def test_an_ended_run_warns_about_undelivered_notes_and_refuses_new_ones(
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
        refused = client.post(f"{base(project)}/auto-notes", data={"text": "x"})

    page_text = re.sub(r"\s+", " ", page.text)
    assert "Notes not delivered:</strong> N1 (not yet to Beta)." in page_text
    assert "Continue Auto delivers them" in page_text
    setup_text = re.sub(r"\s+", " ", setup.text)
    assert (
        f"Auto {record.number} still has notes its agents have not all received: N1."
        in setup_text
    )
    assert refused.status_code == 409
    assert f"Auto {record.number} has ended (converged)" in refused.json()["detail"]
    assert [note.status for note in NoteStore(store.root).load().notes] == ["pending"]


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
