import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_vendored_htmx_assets_match_recorded_release() -> None:
    expected = {
        "htmx.min.js": "449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452",
        "sse.js": "be05b2e2265279f035271adbea0b72a356f20ce4dfa5870481bfe9c51b822fc1",
    }

    for filename, digest in expected.items():
        contents = (ROOT / "app" / "static" / filename).read_bytes()
        assert sha256(contents).hexdigest() == digest


def test_application_packages_are_importable() -> None:
    import app
    import app.agents
    import app.routes

    assert app and app.agents and app.routes


def test_max_run_timeout_defaults_to_four_hours_and_rejects_smaller_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELIBRA_MAX_RUN_TIMEOUT", raising=False)
    monkeypatch.delenv("DELIBRA_RUN_TIMEOUT", raising=False)
    assert Settings.from_env().max_run_timeout == 14_400

    with pytest.raises(ValueError, match="at least DELIBRA_RUN_TIMEOUT"):
        Settings(home=Path("/tmp/delibra-settings-test"), run_timeout=20, max_run_timeout=10)


@pytest.mark.parametrize(
    ("name", "attribute", "default", "low", "high"),
    [
        ("DELIBRA_AUTO_RESUME_DRAIN_SECONDS", "auto_resume_drain_seconds", 30, "0", "301"),
        ("DELIBRA_AUTO_TURN_RETRIES", "auto_turn_retries", 2, "-1", "11"),
        (
            "DELIBRA_AUTO_RETRY_BACKOFF_SECONDS",
            "auto_retry_backoff_seconds",
            5,
            "0",
            "301",
        ),
        (
            "DELIBRA_USAGE_WARN_REMAINING_PERCENT",
            "usage_warn_remaining_percent",
            20,
            "-1",
            "101",
        ),
        (
            "DELIBRA_USAGE_PAUSE_REMAINING_PERCENT",
            "usage_pause_remaining_percent",
            8,
            "-1",
            "101",
        ),
        (
            "DELIBRA_USAGE_WEEKLY_PAUSE_REMAINING_PERCENT",
            "usage_weekly_pause_remaining_percent",
            3,
            "-1",
            "101",
        ),
        ("DELIBRA_USAGE_POLL_SECONDS", "usage_poll_seconds", 1800, "9", "3601"),
        (
            "DELIBRA_USAGE_LOW_QUOTA_POLL_SECONDS",
            "usage_low_quota_poll_seconds",
            300,
            "9",
            "3601",
        ),
        ("DELIBRA_USAGE_STALENESS_SECONDS", "usage_staleness_seconds", 1800, "59", "86401"),
        (
            "DELIBRA_CODEX_APP_SERVER_TIMEOUT_SECONDS",
            "codex_app_server_timeout_seconds",
            15,
            "0",
            "121",
        ),
        (
            "DELIBRA_CODEX_QUOTA_REFRESH_SECONDS",
            "codex_quota_refresh_seconds",
            300,
            "29",
            "3601",
        ),
        (
            "DELIBRA_CODEX_ROLLOUT_SCAN_LIMIT",
            "codex_rollout_scan_limit",
            200,
            "0",
            "5001",
        ),
        (
            "DELIBRA_CODEX_ROLLOUT_READ_LIMIT",
            "codex_rollout_read_limit",
            4 * 1024 * 1024,
            "65535",
            str(64 * 1024 * 1024 + 1),
        ),
        (
            "DELIBRA_AUTO_COMPACT_INPUT_LIMIT",
            "auto_compact_input_limit",
            2 * 1024 * 1024,
            "0",
            str(64 * 1024 * 1024 + 1),
        ),
        (
            "DELIBRA_AUTO_COMPACT_OUTPUT_LIMIT",
            "auto_compact_output_limit",
            256 * 1024,
            "0",
            str(64 * 1024 * 1024 + 1),
        ),
        (
            "DELIBRA_AUTO_COMPACT_MIN_OUTPUT",
            "auto_compact_min_output",
            4 * 1024,
            "0",
            str(64 * 1024 * 1024 + 1),
        ),
        (
            "DELIBRA_AUTO_COMPACT_MAX_FAILURES",
            "auto_compact_max_failures",
            3,
            "0",
            "11",
        ),
        (
            "DELIBRA_AUTO_CONTEXT_TRIGGER_PERCENT",
            "auto_context_trigger_percent",
            70,
            "9",
            "96",
        ),
    ],
)
def test_new_settings_defaults_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    attribute: str,
    default: int,
    low: str,
    high: str,
) -> None:
    monkeypatch.delenv(name, raising=False)
    assert getattr(Settings.from_env(), attribute) == default

    for value in (low, high):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError):
            Settings.from_env()


def test_pause_threshold_must_stay_below_the_warning_threshold() -> None:
    # A pause at or above the warning point would fire before any warning was
    # ever shown, so the two thresholds are validated against each other.
    with pytest.raises(ValueError, match="below DELIBRA_USAGE_WARN_REMAINING_PERCENT"):
        Settings(
            home=Path("/tmp/delibra-settings-test"),
            usage_warn_remaining_percent=20,
            usage_pause_remaining_percent=20,
        )


def test_low_quota_polling_must_not_be_slower_than_idle_polling() -> None:
    # The low-quota tier exists to tighten the badge, so a value above the idle
    # interval would silently slow it down exactly when quota is running out.
    with pytest.raises(ValueError, match="DELIBRA_USAGE_POLL_SECONDS"):
        Settings(
            home=Path("/tmp/delibra-settings-test"),
            usage_poll_seconds=300,
            usage_low_quota_poll_seconds=301,
        )


def test_auto_summary_budget_is_clamped_rather_than_refused_at_startup() -> None:
    # Lowering only the stateless budget is a reasonable thing to do, so the
    # "half the prompt limit" invariant is applied where the summary is read
    # back -- refusing to start would be a worse answer than compacting less.
    settings = Settings(
        home=Path("/tmp/delibra-settings-test"),
        stateless_history_limit=1_000,
        auto_compact_output_limit=256 * 1024,
    )

    assert settings.effective_auto_compact_output_limit == 500


def test_auto_summary_minimum_never_exceeds_the_summary_budget() -> None:
    # A minimum above the budget would skip every compaction while reporting
    # "no headroom", which is a configuration error rather than a real one.
    settings = Settings(
        home=Path("/tmp/delibra-settings-test"),
        auto_compact_output_limit=4 * 1024,
        auto_compact_min_output=8 * 1024,
    )

    assert settings.effective_auto_compact_min_output == 4 * 1024


def test_auto_compaction_input_limit_is_bounded_by_the_prompt_limit() -> None:
    # Reading more material than a prompt could ever hold cannot help: the
    # summary still has to fit the same stateless budget.
    settings = Settings(
        home=Path("/tmp/delibra-settings-test"),
        stateless_history_limit=1_000,
        auto_compact_input_limit=2 * 1024 * 1024,
    )

    assert settings.effective_auto_compact_input_limit == 1_000


def test_auto_turn_retries_accepts_zero_to_disable_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _integer rejects anything below one by default, so zero needs an explicit
    # minimum of 0 -- and zero is the documented way to disable retry.
    monkeypatch.setenv("DELIBRA_AUTO_TURN_RETRIES", "0")

    assert Settings.from_env().auto_turn_retries == 0


def test_allowed_hosts_default_to_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("DELIBRA_HOME", str(tmp_path / "home"))

    assert Settings.from_env().allowed_hosts == ()


def test_allowed_hosts_come_from_the_config_file_and_the_env_var_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"allowed_hosts": ["192.168.1.50:8000", " my-mac.local "]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DELIBRA_HOME", str(home))
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)

    from_file = Settings.from_env()

    monkeypatch.setenv("DELIBRA_ALLOWED_HOSTS", "10.0.0.9:8000, ,10.0.0.10")
    from_env = Settings.from_env()

    assert from_file.allowed_hosts == ("192.168.1.50:8000", "my-mac.local")
    # The env var replaces the file rather than adding to it, so one command
    # line fully describes what a given run will accept.
    assert from_env.allowed_hosts == ("10.0.0.9:8000", "10.0.0.10")


@pytest.mark.parametrize(
    "contents",
    [
        '{"allowed_hosts": "192.168.1.50"}',
        '{"allowed_hosts": [8000]}',
        '{"allowed_hosts": ["has space"]}',
        '{"allowed_hosts": ["http://192.168.1.50:8000"]}',
        "not json",
    ],
)
def test_an_unusable_config_file_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
) -> None:
    # A security setting that silently falls back to its default is the one
    # failure mode worth refusing to start over.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(contents, encoding="utf-8")
    monkeypatch.setenv("DELIBRA_HOME", str(home))
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)

    with pytest.raises(ValueError):
        Settings.from_env()


def test_allowed_clients_default_to_unrestricted_and_load_from_both_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DELIBRA_HOME", str(home))
    monkeypatch.delenv("DELIBRA_ALLOWED_CLIENTS", raising=False)

    assert Settings.from_env().allowed_clients == ()

    (home / "config.json").write_text(
        json.dumps({"allowed_clients": ["192.168.1.0/24", "10.0.0.9"]}),
        encoding="utf-8",
    )
    from_file = Settings.from_env()

    monkeypatch.setenv("DELIBRA_ALLOWED_CLIENTS", "172.16.0.0/12")
    from_env = Settings.from_env()

    # A bare address is stored as the single-address network it denotes, so one
    # comparison serves both spellings at request time.
    assert from_file.allowed_clients == ("192.168.1.0/24", "10.0.0.9/32")
    assert from_env.allowed_clients == ("172.16.0.0/12",)


def test_a_cidr_allowed_host_is_normalized_and_host_bits_are_tolerated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DELIBRA_HOME", str(home))
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("DELIBRA_ALLOWED_HOSTS", "192.168.1.50/24, fd00::/8")

    # Writing the range as "my address slash prefix" is the common shorthand and
    # says exactly one thing, so it is accepted rather than refused.
    assert Settings.from_env().allowed_hosts == ("192.168.1.0/24", "fd00::/8")


@pytest.mark.parametrize(
    "value",
    ["192.168.1.0/33", "not-an-address", "192.168.1.0/24:8000", ""],
)
def test_an_unusable_client_entry_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"allowed_clients": [value]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DELIBRA_HOME", str(home))
    monkeypatch.delenv("DELIBRA_ALLOWED_CLIENTS", raising=False)

    with pytest.raises(ValueError, match="allowed client"):
        Settings.from_env()
