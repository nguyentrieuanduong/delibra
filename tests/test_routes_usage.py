from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RateLimitObservation
from app.storage import RegistryStore
from app.usage import USAGE_PERSISTENCE_WARNING


def cell(html: str, provider: str, window: str) -> str:
    """The rendered text of one provider row's window column.

    The badge is a table now, so a window's label is a column header and no
    longer sits next to its value in the markup. Reading the cell keeps every
    assertion below tied to the row *and* the column it means.
    """

    row = html.split(f'data-provider="{provider}"', 1)[1].split("</tr>", 1)[0]
    body = row.split(f'data-window="{window}"', 1)[1].split("</td>", 1)[0]
    return " ".join(body.lstrip(">").split())


def row_header(html: str, provider: str) -> str:
    row = html.split(f'data-provider="{provider}"', 1)[1].split("</tr>", 1)[0]
    header = row.split("<th", 1)[1].split(">", 1)[1].split("</th>", 1)[0]
    return " ".join(header.split())


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
    assert cell(initial.text, "codex", "five_hour") == "unknown"
    assert cell(initial.text, "codex", "seven_day") == "unknown"
    assert updated.status_code == 200
    assert cell(updated.text, "codex", "five_hour") == "42% remaining"
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


def test_the_badge_is_a_provider_by_window_table(tmp_path) -> None:
    """One row per provider, one column per window, headers on both axes."""

    settings = Settings(home=tmp_path / "home")
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)

    with TestClient(app, base_url="http://localhost") as client:
        app.state.usage_monitor.record(
            _observation("codex", "five_hour", 58.0, status="unknown", now=now)
        )
        app.state.usage_monitor.record(
            _observation("codex", "seven_day", 25.0, status="unknown", now=now)
        )
        response = client.get("/usage/badge")
        stylesheet = client.get("/static/app.css")

    text = response.text
    assert '<th scope="col">5-hour limit</th>' in text
    assert '<th scope="col">Weekly limit</th>' in text
    # Row headers, so a screen reader names the provider for every cell.
    assert text.count('<th scope="row">') == 2
    assert text.count('data-provider="') == 2
    assert text.count('data-window="') == 4
    assert cell(text, "codex", "five_hour") == "42% remaining"
    assert cell(text, "codex", "seven_day") == "75% remaining"
    assert cell(text, "claude", "five_hour") == "unknown"
    assert (
        ".usage-table { border-collapse: collapse; font-size: .75rem; width: auto; }"
        in stylesheet.text
    )
    assert "border: 1px solid #8885;" in stylesheet.text
    assert "padding: .05rem .25rem;" in stylesheet.text


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
    assert cell(page.text, "codex", "five_hour") == "42% remaining"
    assert cell(polled.text, "codex", "five_hour") == "42% remaining"


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

    assert cell(at_threshold.text, "codex", "five_hour") == "20% remaining"
    assert 'hx-trigger="every 1800s"' in at_threshold.text
    assert cell(below_threshold.text, "codex", "five_hour") == "19% remaining"
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
    assert cell(status_only.text, "claude", "five_hour").startswith(
        "unavailable (status warning;"
    )
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

    assert cell(response.text, "codex", "seven_day") == "90% remaining"
    assert cell(response.text, "codex", "five_hour") == "5% remaining"
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
    assert cell(response.text, "claude", "five_hour").startswith(
        "unavailable (status warning;"
    )
    assert f'datetime="{reset.isoformat()}"' in response.text
    assert cell(response.text, "codex", "seven_day") == "2% remaining"


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


def test_the_badge_schedules_codex_hydration_instead_of_waiting_for_it(
    tmp_path,
    monkeypatch,
) -> None:
    """Phase 8 made the quota read a subprocess, so a render must not wait.

    The account read is unavailable here (`/missing/codex`), which is also
    what exercises the rollout fallback: the figure still arrives, just on a
    later poll rather than inside the first render.
    """

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
        # The read runs on the event loop, so the figure lands on a later
        # poll. Bounded so a genuine failure to hydrate still fails the test.
        for _ in range(100):
            hydrated = client.get("/usage/badge")
            if cell(hydrated.text, "codex", "five_hour") == "6% remaining":
                break
            time.sleep(0.02)

        def reject_second_scan(*_args, **_kwargs):
            raise AssertionError("Codex hydration repeated inside its interval")

        monkeypatch.setattr(
            "app.runner.read_latest_codex_rollout_state", reject_second_scan
        )
        repeated = client.get("/usage/badge")

    assert response.status_code == 200
    assert cell(hydrated.text, "codex", "five_hour") == "6% remaining"
    assert 'class="usage-badge paused"' in hydrated.text
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
    assert cell(response.text, "codex", "seven_day") == "71% remaining"


def test_claudes_row_label_needs_no_capability_explanation(
    tmp_path,
) -> None:
    settings = Settings(home=tmp_path / "home")
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/usage/badge")

    assert row_header(response.text, "claude") == "Claude"
    assert "status only" not in response.text
    assert "no percentage" not in response.text
    # Codex is still waiting for its first read, which is a different state.
    assert cell(response.text, "codex", "five_hour") == "unknown"


def test_a_claude_status_still_renders_its_window_and_reset(tmp_path) -> None:
    settings = Settings(home=tmp_path / "home")
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    now = datetime.now(timezone.utc)
    reset = now + timedelta(hours=2)

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
        response = client.get("/usage/badge")

    assert cell(response.text, "claude", "five_hour").startswith(
        "unavailable (status warning;"
    )
    assert f'datetime="{reset.isoformat()}"' in response.text
    assert "status only" not in response.text
    assert "no percentage" not in response.text
