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
    settings = Settings(
        home=tmp_path / "home",
        usage_poll_seconds=37,
        usage_low_quota_poll_seconds=11,
    )
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
    assert 'hx-trigger="every 37s"' in updated.text
    assert 'hx-target="this"' in updated.text
    assert 'hx-swap="outerHTML"' in updated.text


def test_chat_topic_header_loads_the_account_usage_badge(tmp_path) -> None:
    settings = Settings(
        home=tmp_path / "home",
        usage_poll_seconds=23,
        usage_low_quota_poll_seconds=11,
    )
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
    assert 'hx-trigger="every 23s"' in topic_header


def test_badge_never_carries_a_load_trigger_into_its_own_swap(tmp_path) -> None:
    # htmx processes swapped-in content (`makeAjaxLoadTask` -> `processNode`),
    # so a `load` trigger on a fragment that replaces itself re-fires on every
    # swap: a request loop bounded by round-trip time rather than by the poll
    # interval. Both render paths must therefore ship the state instead of
    # asking for it on load.
    settings = Settings(home=tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = RegistryStore(settings.home).register("Loop", project_path)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            _observation("codex", "five_hour", 58.0, status="unknown", now=now)
        )
        polled = client.get("/usage/badge")
        page = client.get(f"/projects/{project.name}/chat")

    topic_header = page.text.split('class="workspace-topic"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "load" not in polled.text.split('hx-trigger="', 1)[1].split('"', 1)[0]
    assert "load" not in topic_header.split('hx-trigger="', 1)[1].split('"', 1)[0]
    # The page ships the current state, so dropping `load` costs no freshness.
    assert "5h: 42% remaining" in page.text
    assert "5h: 42% remaining" in polled.text


def _observation(
    provider: str,
    window: str,
    used_percent: float | None,
    *,
    status: str,
    now: datetime,
) -> RateLimitObservation:
    return RateLimitObservation(
        provider=provider,
        account_key="default",
        window=window,
        used_percent=used_percent,
        status=status,
        resets_at=now + timedelta(hours=2),
        observed_at=now,
        source=(
            "codex_rollout_token_count"
            if provider == "codex"
            else "claude_rate_limit_event"
        ),
    )


def test_badge_polls_slowly_until_a_window_falls_below_the_warning_threshold(
    tmp_path,
) -> None:
    # The tier boundary is the warning threshold itself, so exactly 20 %
    # remaining has not fallen below it and must keep the idle interval.
    settings = Settings(
        home=tmp_path / "home",
        usage_poll_seconds=1800,
        usage_low_quota_poll_seconds=300,
    )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            _observation("codex", "five_hour", 80.0, status="unknown", now=now)
        )
        at_threshold = client.get("/usage/badge")
        app.state.usage_monitor.record(
            _observation(
                "codex",
                "five_hour",
                81.0,
                status="unknown",
                now=now + timedelta(seconds=1),
            )
        )
        below_threshold = client.get("/usage/badge")

    assert "5h: 20% remaining" in at_threshold.text
    assert 'hx-trigger="every 1800s"' in at_threshold.text
    assert "5h: 19% remaining" in below_threshold.text
    assert 'hx-trigger="every 300s"' in below_threshold.text


def test_badge_keeps_the_idle_interval_for_windows_without_a_percentage(
    tmp_path,
) -> None:
    # Polling never calls a provider, so a fast poll cannot discover anything
    # about a window that reports no percentage -- and Claude reports none at
    # all, so any other choice would pin the badge to the fast rate forever.
    settings = Settings(
        home=tmp_path / "home",
        usage_poll_seconds=1800,
        usage_low_quota_poll_seconds=300,
    )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        unknown = client.get("/usage/badge")
        app.state.usage_monitor.record(
            _observation("claude", "five_hour", None, status="warning", now=now)
        )
        status_only = client.get("/usage/badge")

    assert 'hx-trigger="every 1800s"' in unknown.text
    assert "unavailable (status warning;" in status_only.text
    assert 'hx-trigger="every 1800s"' in status_only.text


def test_badge_takes_the_fastest_interval_any_live_window_selects(tmp_path) -> None:
    settings = Settings(
        home=tmp_path / "home",
        usage_poll_seconds=1800,
        usage_low_quota_poll_seconds=300,
    )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            _observation("codex", "seven_day", 10.0, status="unknown", now=now)
        )
        app.state.usage_monitor.record(
            _observation("codex", "five_hour", 95.0, status="unknown", now=now)
        )
        response = client.get("/usage/badge")

    assert "weekly: 90% remaining" in response.text
    assert "5h: 5% remaining" in response.text
    assert 'hx-trigger="every 300s"' in response.text


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
