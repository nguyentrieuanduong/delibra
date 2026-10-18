"""Routes for the Chat page's Questions for you panel."""

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.questions import ProposedQuestion, QuestionStore
from app.storage import ProjectStore, RegistryStore


def questions_app(tmp_path: Path, *, view_limit: int = 96 * 1024):
    settings = Settings(
        home=tmp_path / "home", run_timeout=2,
        auto_questions_view_limit=view_limit,
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("Questions", project_dir)
    store = ProjectStore(project)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    questions = QuestionStore(store.root, settings.auto_questions_view_limit)
    return app, project, questions


def ask(questions: QuestionStore, text: str, *, blocking: bool = False) -> None:
    questions.add(
        [ProposedQuestion(text, "SQLite", blocking)],
        asked_by="a" * 32,
        asked_by_name="Alpha",
        auto_run=1,
        cycle=2,
        round_n=3,
    )


def base(project) -> str:
    return f"/projects/{quote(project.name, safe='')}"


def test_the_panel_lists_questions_escaped_with_their_badges(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "<b>Which?</b>", blocking=True)

    with TestClient(app, base_url="http://localhost") as client:
        panel = client.get(f"{base(project)}/questions")

    assert panel.status_code == 200
    assert "&lt;b&gt;Which?&lt;/b&gt;" in panel.text
    assert "<b>Which?</b>" not in panel.text
    assert "blocking" in panel.text
    assert 'id="questions-cursor" name="after" value="1"' in panel.text
    assert "(1 open)" in panel.text


def test_answering_rerenders_only_the_question_and_updates_the_view(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "Which database?")

    with TestClient(app, base_url="http://localhost") as client:
        first = client.post(f"{base(project)}/questions/Q1/answer", data={"answer": "Postgres"})
        second = client.post(f"{base(project)}/questions/Q1/answer", data={"answer": "SQLite"})

    assert first.status_code == 200
    assert second.text.lstrip().startswith('<li id="question-Q1"')
    assert "Update answer" in second.text
    assert "Answer history (2)" in second.text
    assert b'"answer": "SQLite"' in questions.view_path.read_bytes()


def test_clear_dismiss_restore_and_duplicate(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "one")
    ask(questions, "two")
    url = f"{base(project)}/questions"

    with TestClient(app, base_url="http://localhost") as client:
        client.post(f"{url}/Q1/answer", data={"answer": "yes"})
        cleared = client.post(f"{url}/Q1/clear")
        dismissed = client.post(f"{url}/Q1/dismiss")
        restored = client.post(f"{url}/Q1/restore")
        duplicate = client.post(f"{url}/Q2/duplicate", data={"duplicate_of": "Q1"})
        undone = client.post(f"{url}/Q2/restore")
        bad_reference = client.post(f"{url}/Q1/duplicate", data={"duplicate_of": "Q9"})
        restore_open = client.post(f"{url}/Q1/restore")

    assert "question-open" in cleared.text
    assert "question-dismissed" in dismissed.text
    assert "question-open" in restored.text
    assert "duplicate of Q1" in duplicate.text
    assert "question-open" in undone.text
    assert bad_reference.status_code == 422
    assert restore_open.status_code == 409


def test_delete_removes_the_question_and_its_item(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "one")

    with TestClient(app, base_url="http://localhost") as client:
        deleted = client.post(f"{base(project)}/questions/Q1/delete")

    assert deleted.status_code == 200
    assert "question-Q1" not in deleted.text
    assert questions.load().questions == []


def test_new_returns_only_questions_after_the_cursor(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "old")
    ask(questions, "fresh")

    with TestClient(app, base_url="http://localhost") as client:
        new = client.get(f"{base(project)}/questions/new", params={"after": "1"})
        no_cursor = client.get(f"{base(project)}/questions/new")

    assert "fresh" in new.text
    assert "old" not in new.text
    assert 'id="questions-cursor" name="after" value="2" hx-swap-oob="true"' in new.text
    assert no_cursor.status_code == 422


def test_new_questions_update_the_view_cap_warning(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path, view_limit=8192)
    for number in range(5):
        ask(questions, f"{number}:" + "x" * 1950)

    with TestClient(app, base_url="http://localhost") as client:
        new = client.get(f"{base(project)}/questions/new", params={"after": "0"})

    assert new.status_code == 200
    assert 'id="questions-omitted"' in new.text
    assert 'hx-swap-oob="outerHTML"' in new.text
    assert "not shown to agents" in new.text
    assert "Q1" in new.text


def test_answer_updates_the_view_cap_warning(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path, view_limit=8192)
    ask(questions, "Long answer?")

    with TestClient(app, base_url="http://localhost") as client:
        saved = client.post(
            f"{base(project)}/questions/Q1/answer",
            data={"answer": "y" * 8100},
        )
        cleared = client.post(f"{base(project)}/questions/Q1/clear")

    assert saved.status_code == 200
    assert 'id="questions-omitted"' in saved.text
    assert 'hx-swap-oob="outerHTML"' in saved.text
    assert "not shown to agents" in saved.text
    assert cleared.status_code == 200
    assert 'id="questions-omitted" hx-swap-oob="outerHTML"' in cleared.text
    assert "not shown to agents" not in cleared.text


def test_invalid_ids_and_empty_answers_are_rejected(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "one")

    with TestClient(app, base_url="http://localhost") as client:
        bad_id = client.post(f"{base(project)}/questions/Q0/answer", data={"answer": "x"})
        unknown = client.post(f"{base(project)}/questions/Q9/answer", data={"answer": "x"})
        empty = client.post(f"{base(project)}/questions/Q1/answer", data={"answer": "  "})

    assert bad_id.status_code == 404
    assert unknown.status_code == 404
    assert empty.status_code == 422


def test_the_chat_page_includes_the_panel(tmp_path: Path) -> None:
    app, project, questions = questions_app(tmp_path)
    ask(questions, "Visible on chat?")

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{base(project)}/chat")

    assert 'id="questions-panel"' in page.text
    assert "Visible on chat?" in page.text
