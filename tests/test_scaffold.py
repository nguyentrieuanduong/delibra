from hashlib import sha256
from pathlib import Path


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
