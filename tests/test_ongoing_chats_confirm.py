"""When saving a setting that feeds the chat system part asks first (design 2026-11-19).

The confirm is wanted only while an affected agent holds a native chat session
(`cli_session_id`): that session keeps the system part it started with.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.models import SessionConfig
from app.storage import ProjectStore, RegistryStore

MARK = "data-confirm-ongoing-chats"
LOCAL = ("127.0.0.1", 5000)


def make_app(tmp_path: Path):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
    )
    return app, settings


def add_project(settings: Settings, tmp_path: Path, name: str, native: tuple[str | None, ...]):
    directory = tmp_path / name.lower()
    directory.mkdir()
    project = RegistryStore(settings.home).register(name, directory)
    store = ProjectStore(project)
    for index, cli_session_id in enumerate(native):
        store.create_session(
            SessionConfig(
                id=uuid.uuid4().hex,
                name=f"{name} agent {index}",
                agent="claude",
                model="",
                effort="low",
                role_instructions="Be brief.",
                cli_session_id=cli_session_id,
                status="idle",
                created_at="2026-11-19T00:00:00Z",
                rounds=[],
            )
        )
    return project, store


def form_tag(page: str, action_end: str) -> str:
    match = re.search(rf'<form[^>]*action="[^"]*{re.escape(action_end)}"[^>]*>', page)
    assert match, action_end
    return match.group(0)


def general_page(app, project) -> str:
    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        return client.get(f"/projects/{quote(project.name, safe='')}/settings").text


def test_general_forms_ask_only_while_an_agent_of_the_project_has_a_native_chat(tmp_path: Path) -> None:
    app, settings = make_app(tmp_path)
    ongoing, _ = add_project(settings, tmp_path, "Ongoing", ("native-1", None))
    fresh, _ = add_project(settings, tmp_path, "Fresh", (None, None))

    with_session = general_page(app, ongoing)
    without_session = general_page(app, fresh)

    for action in ("/global-instruction", "/chat-skills", "/download-settings"):
        assert MARK in form_tag(with_session, action)
        assert MARK not in form_tag(without_session, action)
    # Settings that do not feed the system part never ask.
    assert MARK not in form_tag(with_session, "/turn-timeout")


def test_the_global_instruction_asks_only_when_a_project_using_it_has_a_native_chat(tmp_path: Path) -> None:
    app, settings = make_app(tmp_path)
    _, store = add_project(settings, tmp_path, "Elsewhere", ("native-1",))
    add_project(settings, tmp_path, "Idle", (None,))

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        asked = client.get("/settings/instruction").text
        store.set_use_global_instruction(False)
        switched_off = client.get("/settings/instruction").text

    assert MARK in form_tag(asked, "/settings/global-instruction")
    # The only native chat is in a project that does not use the global instruction.
    assert MARK not in form_tag(switched_off, "/settings/global-instruction")


def test_the_skills_form_carries_the_saved_on_or_off_state_not_the_typed_text(tmp_path: Path) -> None:
    """Only turning skills on or off changes the chat system part (review F2): the page
    tells the browser whether the saved list is empty, even after a refused save."""

    app, settings = make_app(tmp_path)
    project, store = add_project(settings, tmp_path, "Skills", ("native-1",))
    skill = tmp_path / "skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: s\ndescription: d\n---\n# S\n")
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        off = client.get(f"{prefix}/settings").text
        refused = client.post(f"{prefix}/chat-skills", data={"chat_skills": "/no/such/skill/SKILL.md"})
        saved = client.post(f"{prefix}/chat-skills", data={"chat_skills": str(skill)}, follow_redirects=False)
        on = client.get(f"{prefix}/settings").text

    assert refused.status_code == 422 and saved.status_code == 303
    assert store.chat_skills() == [str(skill)]
    assert 'data-confirm-saved-on="false"' in form_tag(off, "/chat-skills")
    assert 'data-confirm-saved-on="false"' in form_tag(refused.text, "/chat-skills")
    assert 'data-confirm-saved-on="true"' in form_tag(on, "/chat-skills")


def _unreadable_sessions(store: ProjectStore) -> None:
    store.sessions_root.chmod(0)


def _malformed_config(store: ProjectStore) -> None:
    config = next(store.sessions_root.iterdir()) / "config.json"
    data = json.loads(config.read_text())
    data["rounds"] = "oops"
    config.write_text(json.dumps(data))


@pytest.mark.parametrize("break_project", [_unreadable_sessions, _malformed_config])
def test_a_broken_project_is_skipped_by_the_global_instruction_check(tmp_path: Path, break_project) -> None:
    """The Instruction tab never read projects before: a broken one must not take it down."""

    app, settings = make_app(tmp_path)
    _, broken = add_project(settings, tmp_path, "Broken", ("native-1",))
    add_project(settings, tmp_path, "Healthy", ("native-2",))
    try:
        with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
            # Broken while the server runs (startup migration reads every project).
            break_project(broken)
            page = client.get("/settings/instruction")
    finally:
        broken.sessions_root.chmod(0o700)

    assert page.status_code == 200
    # The healthy project still counts.
    assert MARK in form_tag(page.text, "/settings/global-instruction")


def test_a_refused_preset_save_keeps_the_real_state_and_the_field_filter(tmp_path: Path) -> None:
    """Only a refused instruction save puts unsaved text in the instruction box."""

    app, settings = make_app(tmp_path)
    _, store = add_project(settings, tmp_path, "Presets", ("native-1",))
    bad_preset = {"allowed_clients": "", "allowed_hosts": "", "preset_count": "1",
                  "preset_name_0": "x", "preset_files_0": "relative.md"}

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        with_chat = client.post("/settings/presets", data=bad_preset)
        store.set_use_global_instruction(False)
        without_chat = client.post("/settings/presets", data=bad_preset)
        store.set_use_global_instruction(True)
        refused_instruction = client.post("/settings/global-instruction", data={"global_instruction": "x" * 70000})

    assert with_chat.status_code == without_chat.status_code == refused_instruction.status_code == 422
    tag = form_tag(with_chat.text, "/settings/global-instruction")
    assert MARK in tag and "data-confirm-fields" in tag
    assert MARK not in form_tag(without_chat.text, "/settings/global-instruction")
    tag = form_tag(refused_instruction.text, "/settings/global-instruction")
    assert MARK in tag and "data-confirm-fields" not in tag


def test_a_refused_downloads_save_drops_the_filter_on_that_form_only(tmp_path: Path) -> None:
    app, settings = make_app(tmp_path)
    project, _ = add_project(settings, tmp_path, "Downloads", ("native-1",))
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        refused = client.post(f"{prefix}/download-settings", data={
            "downloads_enabled": "true", "downloads_folder": "../outside", "downloads_approval": "manual",
            "downloads_check_provider": "claude", "downloads_check_model": "haiku",
        })

    assert refused.status_code == 422
    assert "data-confirm-fields" not in form_tag(refused.text, "/download-settings")
    assert "data-confirm-fields" in form_tag(refused.text, "/global-instruction")
