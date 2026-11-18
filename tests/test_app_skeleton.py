from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.storage import ProjectStore, RegistryStore


def test_session_page_renders_seeded_rounds_and_safe_markdown(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Seeded Project", project_dir)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Researcher",
        agent="claude",
        model="sonnet",
        effort="high",
        role_instructions="Be rigorous.",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[
            RoundRecord(
                n=1,
                status="complete",
                error=None,
                warnings=[],
                agent="claude",
                model="sonnet",
                effort="high",
                started_at="2026-07-17T00:00:01Z",
                finished_at="2026-07-17T00:00:02Z",
                source=SourceDescriptor(type="user"),
            )
        ],
    )
    store.create_session(session)
    rounds = store.rounds_dir(session.id)
    (rounds / "round-01.prompt.md").write_text("What is **safe**?")
    (rounds / "round-01.md").write_text("A **rendered** answer. <script>bad()</script>")

    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/sessions/{session.id}")
    assert response.status_code == 200
    assert "Researcher" in response.text
    assert "<strong>rendered</strong>" in response.text
    assert "<script>bad()" not in response.text
    assert "Claude CLI not found" in response.text


def test_invalid_route_ids_and_missing_round_are_client_errors(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register("Seeded Project", project_dir)
    store = ProjectStore(project)
    session = SessionConfig(
        id="a" * 32,
        name="Empty",
        agent="claude",
        model="sonnet",
        effort="low",
        role_instructions="",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )
    store.create_session(session)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(app, base_url="http://localhost") as client:
        invalid_project = client.get("/projects/not-an-id")
        invalid_session = client.get(f"/projects/{quote(project.name, safe='')}/sessions/not-an-id")
        missing_session = client.get(f"/projects/{quote(project.name, safe='')}/sessions/{'b' * 32}")
        missing_round = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{session.id}/rounds/99"
        )
        invalid_stream = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/not-an-id/rounds/1/stream"
        )
        missing_stream = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{session.id}/rounds/99/stream"
        )
        invalid_cancel = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/not-an-id/cancel"
        )
    assert invalid_project.status_code == 404
    assert invalid_session.status_code == 422
    assert missing_session.status_code == 404
    assert missing_round.status_code == 404
    assert invalid_stream.status_code == 422
    assert missing_stream.status_code == 404
    assert invalid_cancel.status_code == 422


def test_the_app_logs_to_its_home_while_it_runs(tmp_path) -> None:
    import logging

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.log_setup import PrivateRotatingFileHandler
    from app.main import create_app

    home = tmp_path / "home"
    app = create_app(
        settings_override=Settings(home=home, run_timeout=2, log_level="DEBUG"),
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(app, base_url="http://localhost"):
        assert logging.getLogger("app").level == logging.DEBUG
    text = (home / "logs" / "delibra.log").read_text(encoding="utf-8")

    assert f"Delibra started: home {home}, log {home / 'logs' / 'delibra.log'}" in text
    assert "Delibra stopped" in text
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home / "logs").stat().st_mode & 0o777 == 0o700
    assert not any(isinstance(h, PrivateRotatingFileHandler) for h in logging.getLogger("app").handlers)


def test_settings_read_the_log_level_from_the_environment(tmp_path, monkeypatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("DELIBRA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DELIBRA_LOG_LEVEL", "warning")
    assert Settings.from_env().log_level == "warning"
    monkeypatch.delenv("DELIBRA_LOG_LEVEL")
    assert Settings.from_env().log_level is None


def test_the_settings_lock_poll_is_not_access_logged() -> None:
    import logging

    from app.main import QuietPollFilter

    def access(path: str, method: str = "GET", status: int = 200) -> logging.LogRecord:
        # uvicorn's layout: client, method, path with query, HTTP version, status.
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", method, path, "1.1", status), None,
        )

    quiet = QuietPollFilter()
    assert quiet.filter(access("/projects/P/settings-lock?tab=coding")) is False
    assert quiet.filter(access("/projects/P/settings-lock")) is False
    assert quiet.filter(access("/projects/P/settings/coding")) is True
    # Only the successful GET poll itself is dropped.
    assert quiet.filter(access("/projects/P/reset?x=/settings-lock", method="POST")) is True
    assert quiet.filter(access("/projects/P/chat?next=/projects/P/settings-lock")) is True
    assert quiet.filter(access("/projects/P/settings-lock?tab=coding", method="POST")) is True
    assert quiet.filter(access("/projects/settings-lock/chat")) is True
    assert quiet.filter(access("/projects/settings-lock/settings")) is True
    assert quiet.filter(access("/projects/P/settings-lockdown")) is True
    assert quiet.filter(access("/projects/P/settings-lock/extra")) is True
    assert quiet.filter(access("/projects/P/settings-lock?tab=nope", status=404)) is True
    assert quiet.filter(access("/projects/P/settings-lock", status=500)) is True
    assert quiet.filter(logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain", None, None)) is True


def test_create_app_installs_the_quiet_poll_filter(tmp_path) -> None:
    import logging

    from app.main import QuietPollFilter

    create_app(
        settings_override=Settings(home=tmp_path / "home", run_timeout=2),
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    assert any(isinstance(f, QuietPollFilter) for f in logging.getLogger("uvicorn.access").filters)


def test_the_loopback_gate_names_the_setting_it_refuses() -> None:
    import pytest
    from fastapi import HTTPException

    from app.security import CODING_ONLY_HERE, require_loopback_client

    class Peer:
        scope = {"client": ("192.168.1.20", 5000)}

    with pytest.raises(HTTPException) as default:
        require_loopback_client(Peer())
    with pytest.raises(HTTPException) as named:
        require_loopback_client(Peer(), reason=CODING_ONLY_HERE)
    assert (default.value.status_code, default.value.detail) == (403, "Only this machine can change this setting")
    assert named.value.detail == CODING_ONLY_HERE
