import hashlib
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.urls import project_url, static_url


def test_project_url_encodes_exactly_one_path_component() -> None:
    assert project_url("a" * 32) == f"/projects/{'a' * 32}"
    assert project_url("50% #?&+ off", "/chat") == (
        "/projects/50%25%20%23%3F%26%2B%20off/chat"
    )


def test_project_url_rejects_a_non_path_suffix() -> None:
    try:
        project_url("Project", "chat")
    except ValueError as exc:
        assert str(exc) == "project URL suffix must start with '/' or '?'"
    else:
        raise AssertionError("non-path suffix was accepted")


def test_static_url_tags_the_asset_with_its_content_hash(tmp_path: Path) -> None:
    asset = tmp_path / "app.css"
    asset.write_text("body { color: red; }", encoding="utf-8")
    digest = hashlib.sha256(b"body { color: red; }").hexdigest()[:12]

    assert static_url("app.css", tmp_path) == f"/static/app.css?v={digest}"


def test_static_url_changes_when_the_asset_is_edited_without_a_restart(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "app.css"
    asset.write_text("body { color: red; }", encoding="utf-8")
    before = static_url("app.css", tmp_path)
    asset.write_text("body { color: blue; margin: 0; }", encoding="utf-8")

    assert static_url("app.css", tmp_path) != before


def test_static_url_rejects_a_path_outside_the_static_root(tmp_path: Path) -> None:
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the static root"):
        static_url("../secret.txt", root)


def test_pages_link_every_static_asset_by_version(tmp_path: Path) -> None:
    app = create_app(
        settings_override=Settings(home=tmp_path / "home"),
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get("/")
        assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', page.text)
        served = [client.get(url).status_code for url in assets]

    assert sorted(url.split("?")[0] for url in assets) == [
        "/static/app.css",
        "/static/app.js",
        "/static/htmx.min.js",
        "/static/sse.js",
    ]
    assert all(re.search(r"\?v=[0-9a-f]{12}$", url) for url in assets)
    assert served == [200] * len(assets)
