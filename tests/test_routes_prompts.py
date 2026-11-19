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


def a_project(settings, tmp_path, name="P"):
    project_dir = tmp_path / name.lower()
    project_dir.mkdir()
    project = RegistryStore(settings.home).register(name, project_dir)
    return project, f"/projects/{quote(project.name, safe='')}"


def test_a_project_override_wins_and_use_global_removes_it(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        client.post("/settings/prompts/compact", data={"template": "Global summary.", "action": "save"})
        global_page = client.get(f"{prefix}/settings/prompts").text
        saved = client.post(f"{prefix}/prompts/compact", data={"template": "Project summary.", "action": "save"},
                            follow_redirects=False)
        page = client.get(f"{prefix}/settings/prompts").text
        back = client.post(f"{prefix}/prompts/compact", data={"action": "use_global"}, follow_redirects=False)

    assert ">Global</span>" in card(global_page, "compact", "stateless_claude")
    assert (saved.status_code, saved.headers["location"]) == (303, f"{prefix}/settings/prompts#prompt-compact")
    assert page.index('id="prompt-compact"') < page.index('id="prompt-pass"')  # overridden prompts first
    shown = page[page.index('id="prompt-compact"'):page.index('id="prompt-pass"')]
    assert "This project" in shown and ">Project summary.</textarea>" in shown and "Use global" in shown
    assert back.status_code == 303
    assert ProjectStore(project).prompt_overrides() == {}


def test_a_refused_project_prompt_pops_up_and_keeps_the_text(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    before = ProjectStore(project).manifest_path.read_bytes()
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(f"{prefix}/prompts/notes", data={"template": "Notes {sent} {nope}", "action": "save"})
        unknown = client.post(f"{prefix}/prompts/nope", data={"template": "x", "action": "save"})

    assert response.status_code == 422
    message = "Nothing was saved: Notes has an unknown placeholder {nope}"
    assert f"<div hidden data-page-alert>{escape(message)}</div>" in response.text
    assert ">Notes {sent} {nope}</textarea>" in response.text
    assert ProjectStore(project).manifest_path.read_bytes() == before
    assert unknown.status_code == 404


def test_an_invalid_project_override_is_shown_on_its_card(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    ProjectStore(project).set_prompt_override("compact", "{nope}")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get(f"{prefix}/settings/prompts").text

    assert "Compact summary prompt is invalid: Compact summary has an unknown placeholder {nope}" in page
    assert ">{nope}</textarea>" in page


def test_project_preview_renders_without_saving(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        shown = client.post(f"{prefix}/prompts/auto_discussion/preview", data={"template": "{context} {verdict_rules}"})
        local_only = client.post(f"{prefix}/prompts/build_handover/preview", data={"template": "{name} {reason}"})

    assert "/path/to/workspace/inputs/round-07/auto-context.md State your conclusion" in shown.text
    assert local_only.status_code == 403
    assert ProjectStore(project).prompt_overrides() == {}


def test_lan_clients_can_edit_project_prompts(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        page = client.get(f"{prefix}/settings/prompts").text
        saved = client.post(f"{prefix}/prompts/compact", data={"template": "Short.", "action": "save"},
                            follow_redirects=False)

    assert saved.status_code == 303
    assert ProjectStore(project).prompt_overrides() == {"compact": "Short."}
    assert "Save override" in page
    assert 'href="/settings/prompts"' not in page  # no link into the local-only global tab


@pytest.mark.parametrize("prompt_id", ["build_turn", "model_check", "notes", "stateless_claude"])
def test_lan_clients_cannot_change_prompts_that_reach_build_runs_or_downloads(tmp_path, prompt_id) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    before = ProjectStore(project).manifest_path.read_bytes()
    template = CATALOGUE[prompt_id].default
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        saved = client.post(f"{prefix}/prompts/{prompt_id}", data={"template": template, "action": "save"})
        reset = client.post(f"{prefix}/prompts/{prompt_id}", data={"action": "use_global"})

    assert (saved.status_code, reset.status_code) == (403, 403)
    assert "Only this machine can change this prompt" in saved.text
    assert ProjectStore(project).manifest_path.read_bytes() == before


@pytest.mark.parametrize("prompt_id", ["pass", "auto_preparation", "auto_discussion"])
def test_lan_clients_can_change_chat_pass_and_discussion_only_prompts(tmp_path, prompt_id) -> None:
    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    template = CATALOGUE[prompt_id].default + " Be brief."
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        saved = client.post(f"{prefix}/prompts/{prompt_id}", data={"template": template, "action": "save"},
                            follow_redirects=False)

    assert saved.status_code == 303
    assert ProjectStore(project).prompt_overrides() == {prompt_id: template}


def test_lan_viewers_see_local_only_prompts_read_only(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    _project, prefix = a_project(settings, tmp_path)
    with TestClient(app, base_url="http://localhost", client=LAN) as client:
        lan_page = client.get(f"{prefix}/settings/prompts").text
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        local_page = client.get(f"{prefix}/settings/prompts").text

    build = card(lan_page, "build_turn", "docker_lima")
    assert "<textarea name=\"template\" rows=\"6\" maxlength=\"10000\" readonly>" in build
    assert "Only this machine can change this prompt." in build
    assert "Save override" not in build and "Preview typed text" not in build
    discussion = card(lan_page, "auto_discussion", "auto_compaction")
    assert "Save override" in discussion and " readonly>" not in discussion
    assert "Only this machine can change this prompt." not in local_page


def test_an_invalid_stored_prompt_refuses_a_chat_message_with_its_reason(tmp_path) -> None:
    from app.models import SessionConfig

    app, settings = make_app(tmp_path)
    project, prefix = a_project(settings, tmp_path)
    store = ProjectStore(project)
    store.create_session(SessionConfig(
        id="1" * 32, name="Alpha", agent="claude", model="m", effort="low", role_instructions="",
        cli_session_id=None, status="idle", created_at="2026-11-19T00:00:00Z",
    ))
    store.set_prompt_override("notes", "{nope}")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post(f"{prefix}/chat/run", data={"session_id": "1" * 32, "prompt": "Hi"})

    assert response.status_code == 422
    assert "Notes prompt is invalid: Notes has an unknown placeholder {nope}; fix it in Settings" in response.text
    assert store.load_session("1" * 32).rounds == []


@pytest.mark.parametrize("damage", ["not json", "invalid overrides"])
def test_one_corrupt_project_does_not_stop_the_global_tab(tmp_path, damage) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha", "Beta"])
    manifest = stores["Beta"].manifest_path
    if damage == "not json":
        manifest.write_text("{not json")
    else:
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "prompt_overrides": 3}))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        page = client.get("/settings/prompts")

    assert page.status_code == 200
    shown = card(page.text, "compact", "stateless_claude")
    assert f'value="{stores["Alpha"].project.id}"' in shown
    assert f'value="{stores["Beta"].project.id}"' not in shown


def test_a_registered_project_that_cannot_be_changed_is_named(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha", "Beta"])
    beta = stores["Beta"]
    beta.manifest_path.write_text(json.dumps({**json.loads(beta.manifest_path.read_text()), "prompt_overrides": 3}))
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/prompts/compact", data={
            "template": "Global summary.", "action": "save",
            "replace": [stores["Alpha"].project.id, beta.project.id],
        })

    assert response.status_code == 200
    assert "Saved. The override was not replaced in Beta: project prompt overrides are invalid" in response.text
    assert stores["Alpha"].prompt_overrides() == {}


def test_a_refused_global_save_keeps_the_ticked_projects(tmp_path) -> None:
    app, settings = make_app(tmp_path)
    stores = overriding_projects(settings, ["Alpha", "Beta"])
    alpha, beta = stores["Alpha"].project.id, stores["Beta"].project.id
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        response = client.post("/settings/prompts/compact",
                               data={"template": "{nope}", "action": "save", "replace": [alpha]})

    assert response.status_code == 422
    shown = card(response.text, "compact", "stateless_claude")
    assert f'value="{alpha}" data-project-name="Alpha" checked>' in shown
    assert f'value="{beta}" data-project-name="Beta">' in shown
