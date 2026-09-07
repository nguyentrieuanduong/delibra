from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RateLimitObservation
from app.storage import RegistryStore


def test_badge_poll_reads_new_account_usage_without_an_auto_event(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home", usage_poll_seconds=37)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        initial = client.get("/usage/badge")
        app.state.usage_monitor.record(
            RateLimitObservation(
                provider="codex",
                account_key="default",
                window="five_hour",
                used_percent=58.0,
                status="unknown",
                resets_at=now + timedelta(hours=2),
                observed_at=now,
                source="codex_rollout_token_count",
            )
        )
        updated = client.get("/usage/badge")

    assert initial.status_code == 200
    assert "Codex" in initial.text
    assert "5h: unknown" in initial.text
    assert "weekly: unknown" in initial.text
    assert updated.status_code == 200
    assert "5h: 42% remaining" in updated.text
    assert 'id="usage-badge"' in updated.text
    assert 'hx-get="/usage/badge"' in updated.text
    assert 'hx-trigger="load, every 37s"' in updated.text
    assert 'hx-target="this"' in updated.text
    assert 'hx-swap="outerHTML"' in updated.text


def test_chat_topic_header_loads_the_account_usage_badge(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home", usage_poll_seconds=23)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Quota", project_path)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{project.name}/chat")

    assert response.status_code == 200
    topic_header = response.text.split('class="workspace-topic"', 1)[1].split(
        "</section>", 1
    )[0]
    assert 'id="usage-badge"' in topic_header
    assert 'hx-get="/usage/badge"' in topic_header
    assert 'hx-trigger="load, every 23s"' in topic_header


def test_badge_distinguishes_status_only_and_paused_windows(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home")
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)
    reset = now + timedelta(hours=1)

    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            RateLimitObservation(
                provider="claude",
                account_key="default",
                window="five_hour",
                used_percent=None,
                status="warning",
                resets_at=reset,
                observed_at=now,
                source="claude_rate_limit_event",
            )
        )
        app.state.usage_monitor.record(
            RateLimitObservation(
                provider="codex",
                account_key="default",
                window="seven_day",
                used_percent=98.0,
                status="unknown",
                resets_at=reset,
                observed_at=now,
                source="codex_rollout_token_count",
            )
        )
        response = client.get("/usage/badge")

    assert response.status_code == 200
    assert 'class="usage-badge paused"' in response.text
    assert "5h:" in response.text
    assert "unavailable (status warning;" in response.text
    assert f'datetime="{reset.isoformat()}"' in response.text
    assert "weekly: 2% remaining" in response.text
