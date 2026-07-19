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
