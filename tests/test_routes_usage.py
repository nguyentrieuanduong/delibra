from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RateLimitObservation
from app.storage import RegistryStore
from app.usage import USAGE_PERSISTENCE_WARNING


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


def test_badge_reports_when_quota_durability_is_degraded(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(home=tmp_path / "home")
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    def fail_usage_write(*_args, **_kwargs) -> None:
        raise OSError("read-only quota store")

    monkeypatch.setattr("app.usage.atomic_write_owned_json", fail_usage_write)
    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            RateLimitObservation(
                provider="codex",
                account_key="default",
                window="five_hour",
                used_percent=10.0,
                status="unknown",
                resets_at=now + timedelta(hours=2),
                observed_at=now,
                source="codex_rollout_token_count",
            )
        )
        response = client.get("/usage/badge")

    assert response.status_code == 200
    assert USAGE_PERSISTENCE_WARNING in response.text
    assert 'class="usage-durability notice"' in response.text
    assert 'role="status"' in response.text


def test_first_badge_read_hydrates_codex_quota_from_the_latest_rollout(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(home=tmp_path / "home")
    rollout_dir = settings.codex_home / "sessions" / "2026" / "09" / "07"
    rollout_dir.mkdir(parents=True)
    reset = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    rollout = rollout_dir / (
        "rollout-2026-09-07T10-02-47-01a0412b-8a1d-7fe0-b58d-d6db6ca38dcc.jsonl"
    )
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 94.0,
                            "window_minutes": 300,
                            "resets_at": reset,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/usage/badge")

        def reject_second_scan(*_args, **_kwargs):
            raise AssertionError("Codex hydration repeated")

        monkeypatch.setattr(
            "app.runner.read_latest_codex_rate_limits", reject_second_scan
        )
        repeated = client.get("/usage/badge")

    assert response.status_code == 200
    assert "5h: 6% remaining" in response.text
    assert 'class="usage-badge paused"' in response.text
    assert repeated.status_code == 200


def test_badge_restores_persisted_quota_after_an_application_restart(
    tmp_path,
) -> None:
    settings = Settings(home=tmp_path / "home")
    now = datetime.now(timezone.utc)
    first = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(first, base_url="http://localhost"):
        first.state.usage_monitor.record(
            RateLimitObservation(
                provider="codex",
                account_key="default",
                window="seven_day",
                used_percent=29.0,
                status="unknown",
                resets_at=now + timedelta(days=2),
                observed_at=now,
                source="codex_rollout_token_count",
            )
        )

    restarted = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(restarted, base_url="http://localhost") as client:
        response = client.get("/usage/badge")

    assert response.status_code == 200
    assert "weekly: 71% remaining" in response.text
