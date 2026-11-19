"""When saving a setting that feeds the chat system part asks first (design 2026-11-19).

The confirm is wanted only while an affected agent holds a native chat session
(`cli_session_id`): that session keeps the system part it started with.
"""

from __future__ import annotations

from pathlib import Path
import re
import uuid
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RoundRecord, SessionConfig, SourceDescriptor
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


def test_an_agent_edit_form_asks_only_for_the_agent_holding_a_native_chat(tmp_path: Path) -> None:
    app, settings = make_app(tmp_path)
    project, store = add_project(settings, tmp_path, "Agents", ("native-1", None))
    native_id, fresh_id = (session.id for session in store.list_sessions())
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        agents_tab = client.get(f"{prefix}/settings/agents").text
        chat_native = client.get(f"{prefix}/chat?agent={native_id}").text
        chat_fresh = client.get(f"{prefix}/chat?agent={fresh_id}").text

    assert MARK in form_tag(agents_tab, f"/sessions/{native_id}/edit")
    assert MARK not in form_tag(agents_tab, f"/sessions/{fresh_id}/edit")
    assert MARK in form_tag(chat_native, f"/sessions/{native_id}/edit?agent={native_id}")
    assert MARK not in form_tag(chat_fresh, f"/sessions/{fresh_id}/edit?agent={fresh_id}")


def test_an_agent_past_its_first_round_never_asks_since_its_role_is_fixed(tmp_path: Path) -> None:
    app, settings = make_app(tmp_path)
    project, store = add_project(settings, tmp_path, "Chatted", ("native-1",))
    config = store.list_sessions()[0]
    config.rounds = [
        RoundRecord(
            n=1, status="complete", error=None, warnings=[], agent="claude", model="",
            effort="low", started_at="2026-11-19T00:00:00Z", finished_at="2026-11-19T00:00:01Z",
            source=SourceDescriptor(type="user"),
        )
    ]
    store.save_session(config)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        agents_tab = client.get(f"{prefix}/settings/agents").text
        chat = client.get(f"{prefix}/chat?agent={config.id}").text

    # A model or roots edit does not touch the system part: no confirm.
    assert MARK not in form_tag(agents_tab, f"/sessions/{config.id}/edit")
    assert MARK not in form_tag(chat, f"/sessions/{config.id}/edit?agent={config.id}")


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


def test_a_refused_page_asks_on_every_save_of_these_forms(tmp_path: Path) -> None:
    """After a refusal the form shows unsaved typed values, so 'unchanged' proves nothing."""

    app, settings = make_app(tmp_path)
    project, _ = add_project(settings, tmp_path, "Refused", ("native-1",))
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost", client=LOCAL) as client:
        normal = client.get(f"{prefix}/settings").text
        refused = client.post(f"{prefix}/chat-skills", data={"chat_skills": "/no/such/skill/SKILL.md"})

    assert refused.status_code == 422
    assert "data-confirm-fields" in form_tag(normal, "/chat-skills")
    tag = form_tag(refused.text, "/chat-skills")
    assert MARK in tag and "data-confirm-fields" not in tag
