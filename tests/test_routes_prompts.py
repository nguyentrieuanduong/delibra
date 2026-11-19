"""The global and project Prompts tabs (design 2026-11-15, editable prompts)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from markupsafe import escape
import pytest

from app.config import Settings
from app.main import create_app
from app.prompts import CATALOGUE
from app.storage import ProjectStore, RegistryStore

LOCAL = ("127.0.0.1", 5000)
LAN = ("192.168.1.20", 5000)


@pytest.fixture(autouse=True)
def no_access_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELIBRA_ALLOWED_CLIENTS", raising=False)
    monkeypatch.delenv("DELIBRA_ALLOWED_HOSTS", raising=False)


def make_app(tmp_path: Path):
    # The LAN peer is an allowed client, so a 403 below comes from the route itself.
    settings = Settings(home=tmp_path / "home", run_timeout=2, allowed_clients=("192.168.1.0/24",))
    app = create_app(settings_override=settings,
                     provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"})
    return app, settings


def card(page: str, prompt_id: str, next_id: str) -> str:
    return page[page.index(f'id="prompt-{prompt_id}"'):page.index(f'id="prompt-{next_id}"')]


def test_the_global_prompts_tab_lists_every_prompt_with_locked_text_and_preview(tmp_path) -> None:
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/prompts").text

    for spec in CATALOGUE.values():
        assert f'id="prompt-{spec.id}"' in page
    assert "Hội tụ" in page  # the locked verdict rules are shown read-only
    assert "/path/to/workspace/inputs/round-07/auto-context.md" in page  # preview sample
    assert '<a href="/settings/prompts" aria-current="page">Prompts</a>' in page
    tabs = page[page.index('class="settings-tabs"'):page.index("</nav>")]
    assert [label for label in ("Access", "Instruction", "Prompts", "Agents", "Converter") if f">{label}</a>" in tabs] \
        == ["Access", "Instruction", "Prompts", "Agents", "Converter"]
    assert tabs.index(">Instruction<") < tabs.index(">Prompts<") < tabs.index(">Agents<")


def test_saving_and_resetting_a_global_prompt(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        saved = client.post("/settings/prompts/compact",
                            data={"template": "Short summary.", "action": "save"},
                            follow_redirects=False)
        stored = json.loads((settings.home / "prompts.json").read_text())["prompts"]
        page = client.get("/settings/prompts").text
        reset = client.post("/settings/prompts/compact", data={"action": "reset"},
                            follow_redirects=False)

    assert (saved.status_code, saved.headers["location"]) == (303, "/settings/prompts#prompt-compact")
    assert stored == {"compact": "Short summary."}
    assert "Changed from built-in" in card(page, "compact", "stateless_claude")
    assert reset.status_code == 303
    assert json.loads((settings.home / "prompts.json").read_text())["prompts"] == {}


def test_a_refused_global_prompt_pops_up_and_keeps_the_text(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/prompts/auto_discussion",
                               data={"template": "Read it. {verdict_rules}", "action": "save"})

    assert response.status_code == 422
    message = "Nothing was saved: Discussion turn must contain {context} exactly once"
    assert f"<div hidden data-page-alert>{escape(message)}</div>" in response.text
    assert ">Read it. {verdict_rules}</textarea>" in response.text
    assert not (settings.home / "prompts.json").exists()


def test_an_unknown_prompt_or_action_is_not_found(tmp_path) -> None:
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        assert client.post("/settings/prompts/nope", data={"template": "x", "action": "save"}).status_code == 404
        assert client.post("/settings/prompts/compact", data={"template": "x", "action": "drop"}).status_code == 404


def test_preview_renders_typed_text_without_saving(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        preview = client.post("/settings/prompts/questions_read/preview",
                              data={"template": "Answers live in {view_path}."})
        bad = client.post("/settings/prompts/questions_read/preview", data={"template": "x"})

    assert "Answers live in /path/to/questions.json." in preview.text
    assert "must contain {view_path} exactly once" in bad.text
    assert not (settings.home / "prompts.json").exists()


def test_a_broken_prompts_file_is_shown_and_its_bytes_kept(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    settings.home.mkdir(parents=True, exist_ok=True)
    (settings.home / "prompts.json").write_bytes(b"{not json")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/prompts")
        saved = client.post("/settings/prompts/compact", data={"template": "Short.", "action": "save"})

    assert page.status_code == 200 and "prompts.json is not valid JSON; fix or remove" in page.text
    assert saved.status_code == 422 and "Nothing was saved: Prompts.json is not valid JSON" in saved.text
    assert (settings.home / "prompts.json").read_bytes() == b"{not json"


def test_the_global_tab_lists_projects_that_override_a_prompt(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    project = RegistryStore(settings.home).register("Overrider", project_dir)
    ProjectStore(project).set_prompt_override("compact", "Mine.")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/prompts").text

    shown = card(page, "compact", "stateless_claude")
    assert "Overridden in" in shown
    assert f'href="/projects/{quote(project.name, safe="")}/settings/prompts#prompt-compact"' in shown


def test_lan_clients_cannot_open_or_save_global_prompts(tmp_path) -> None:
    app, _settings = make_app(tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        assert client.get("/settings/prompts").status_code == 403
        assert client.post("/settings/prompts/compact", data={"template": "x", "action": "save"}).status_code == 403
        assert client.post("/settings/prompts/compact/preview", data={"template": "x"}).status_code == 403


def overriding_projects(settings, names):
    stores = {}
    for name in names:
        project_dir = settings.home.parent / name
        project_dir.mkdir()
        stores[name] = ProjectStore(RegistryStore(settings.home).register(name, project_dir))
        stores[name].set_prompt_override("compact", f"{name}'s summary.")
    return stores


def test_saving_a_global_prompt_replaces_only_the_chosen_overrides(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha", "Beta"])
    alpha_id = stores["Alpha"].project.id
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/prompts").text
        saved = client.post("/settings/prompts/compact",
                            data={"template": "Global summary.", "action": "save", "replace": [alpha_id]},
                            follow_redirects=False)

    shown = card(page, "compact", "stateless_claude")
    assert f'name="replace" value="{alpha_id}"' in shown and "Compare" in shown
    assert 'class="auto-choice"' in shown  # checkbox on the same row as its text
    assert 'onsubmit="return confirmPromptReplace(this, event)"' in shown
    assert saved.status_code == 303
    assert stores["Alpha"].prompt_overrides() == {}
    assert stores["Beta"].prompt_overrides() == {"compact": "Beta's summary."}


def test_resetting_a_global_prompt_can_replace_overrides_too(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha"])
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        reset = client.post("/settings/prompts/compact",
                            data={"action": "reset", "replace": [stores["Alpha"].project.id]},
                            follow_redirects=False)

    assert reset.status_code == 303
    assert stores["Alpha"].prompt_overrides() == {}


def test_a_refused_global_save_replaces_nothing(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha"])
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/prompts/auto_discussion",
                               data={"template": "No placeholder", "action": "save",
                                     "replace": [stores["Alpha"].project.id]})

    assert response.status_code == 422
    assert stores["Alpha"].prompt_overrides() == {"compact": "Alpha's summary."}


def test_a_project_that_cannot_be_changed_is_named_and_the_rest_still_happen(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha"])
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/prompts/compact",
                               data={"template": "Global summary.", "action": "save",
                                     "replace": ["f" * 32, stores["Alpha"].project.id]})

    assert response.status_code == 200
    assert "<div hidden data-page-alert>Saved. The override was not replaced in an unknown project: " in response.text
    assert stores["Alpha"].prompt_overrides() == {}
    assert json.loads((settings.home / "prompts.json").read_text())["prompts"] == {"compact": "Global summary."}


def test_compare_shows_the_project_version_against_the_typed_global_text(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha"])
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        shown = client.post("/settings/prompts/compact/compare",
                            data={"template": "Global summary.", "project": stores["Alpha"].project.id})
        same = client.post("/settings/prompts/compact/compare",
                           data={"template": "Alpha's summary.", "project": stores["Alpha"].project.id})
        missing = client.post("/settings/prompts/compact/compare",
                              data={"template": "x", "project": "f" * 32})
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        lan = client.post("/settings/prompts/compact/compare",
                          data={"template": "x", "project": stores["Alpha"].project.id})

    assert '<span class="compare-project">− Alpha&#39;s summary.</span>' in shown.text
    assert '<span class="compare-global">+ Global summary.</span>' in shown.text
    assert "They are identical." in same.text
    assert not (settings.home / "prompts.json").exists()  # comparing never saves
    assert missing.status_code == 404
    assert lan.status_code == 403

