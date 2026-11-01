"""Home page search: one request per typing pause, list filtered, URL kept."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.storage import RegistryStore
from tests.browser.conftest import LiveServer


@pytest.mark.browser
def test_typing_filters_after_a_pause_with_one_request(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    registry = RegistryStore(tmp_path / "home")
    for name in ("Alpha notes", "Beta notes"):
        path = tmp_path / name.replace(" ", "-")
        path.mkdir()
        registry.register(name, path)
    page.goto(f"{app['base_url']}/")
    searches: list[str] = []
    page.on("request", lambda request: searches.append(request.url) if "q=" in request.url else None)

    page.type('input[name="q"]', "alp", delay=60)
    page.wait_for_timeout(1200)

    assert len(searches) == 1, searches
    expected = {"q": ["alp"], "sort": ["activity-desc"]}
    assert parse_qs(urlsplit(searches[0]).query) == expected
    cards = page.locator("#project-list article.project-card h2").all_inner_texts()
    assert cards == ["Alpha notes"]
    assert parse_qs(urlsplit(page.url).query) == expected


@pytest.mark.browser
def test_back_restores_the_controls_with_the_list(
    live_server: LiveServer, page: Any, tmp_path: Path
) -> None:
    app = live_server(["success"], tmp_path)
    registry = RegistryStore(tmp_path / "home")
    for name in ("Alpha notes", "Beta notes"):
        path = tmp_path / name.replace(" ", "-")
        path.mkdir()
        registry.register(name, path)
    page.goto(f"{app['base_url']}/")
    cards = page.locator("#project-list article.project-card h2")

    page.select_option('select[name="sort"]', "name-asc")
    page.wait_for_url("**/?sort=name-asc*")
    page.select_option('select[name="sort"]', "name-desc")
    page.wait_for_url("**/?sort=name-desc*")
    page.fill('input[name="q"]', "beta")
    page.wait_for_url("**q=beta*")
    page.go_back()
    page.wait_for_url("**/?sort=name-desc*")

    assert page.locator('input[name="q"]').input_value() == ""
    assert page.locator('select[name="sort"]').input_value() == "name-desc"
    page.go_back()
    page.wait_for_url("**/?sort=name-asc*")

    assert cards.all_inner_texts() == ["Alpha notes", "Beta notes", "Verify"]
    assert page.locator('select[name="sort"]').input_value() == "name-asc"
