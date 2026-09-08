from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.health import ProviderHealth
from app.main import create_app
from app.models import (
    AutoRoundDescriptor,
    ContextObservation,
    RateLimitReading,
    RoundRecord,
    SessionConfig,
    SharedContextDescriptor,
    SourceDescriptor,
    TurnUsage,
)
from app.storage import CodexRolloutState, ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class ChatAdapter:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", self.mode],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "progress":
            return [AgentEvent("progress", "Chat progress")]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


def record(number: int, started_at: str) -> RoundRecord:
    return RoundRecord(
        n=number,
        status="complete",
        error=None,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at=started_at,
        finished_at=started_at,
        source=SourceDescriptor(type="user"),
    )


def auto_round(round_n: int, auto_id: str) -> RoundRecord:
    return RoundRecord(
        n=round_n,
        status="complete",
        error=None,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at=f"2026-01-01T00:00:0{round_n}Z",
        finished_at=f"2026-01-01T00:00:0{round_n}Z",
        source=SourceDescriptor(type="auto"),
        auto=AutoRoundDescriptor(
            auto_id=auto_id,
            phase="discussion",
            cycle=1,
            position=round_n - 1,
            context_file=f"inputs/round-{round_n:02d}/auto-context.md",
            context_sha256="0" * 64,
            verdict="continue",
        ),
    )


def billed_round(round_n: int, usage: TurnUsage | None) -> RoundRecord:
    return RoundRecord(
        n=round_n,
        status="complete",
        error=None,
        warnings=[],
        agent="fake",
        model="success",
        effort="low",
        started_at=f"2026-01-01T00:00:0{round_n}Z",
        finished_at=f"2026-01-01T00:00:0{round_n}Z",
        source=SourceDescriptor(type="user"),
        usage=usage,
    )


def session(
    session_id: str,
    name: str,
    *,
    agent: str = "fake",
    mode: str = "success",
    role_instructions: str = "Test",
    rounds: list[RoundRecord] | None = None,
) -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=name,
        agent=agent,
        model=mode,
        effort="low",
        role_instructions=role_instructions,
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=rounds or [],
    )


def composer_form(html: str) -> str:
    """The composer markup alone.

    The sidebar's create-agent form contains a textarea too, and it is rendered
    first, so an unscoped search matches the wrong control.
    """
    match = re.search(r'<form\s+id="chat-composer".*?</form>', html, re.S)
    assert match is not None, "chat page rendered no composer"
    return match.group()


def setup_project(
    tmp_path: Path,
    sessions: list[SessionConfig],
    *,
    project_name: str = "Chat",
):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_path = tmp_path / "project"
    project_path.mkdir()
    registry = RegistryStore(settings.home)
    settings.codex_home.mkdir(mode=0o700)
    (settings.codex_home / "auth.json").write_text("{}", encoding="utf-8")
    project = registry.register(project_name, project_path)
    store = ProjectStore(project)
    for config in sessions:
        store.create_session(config)
        for item in config.rounds:
            rounds = store.rounds_dir(config.id)
            (rounds / f"round-{item.n:02d}.prompt.md").write_text(
                f"Prompt {config.name}", encoding="utf-8"
            )
            (rounds / f"round-{item.n:02d}.md").write_text(
                f"Answer {config.name}", encoding="utf-8"
            )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=lambda config: ChatAdapter(config.model),
    )
    return app, project, store


def assert_synchronized_selection_fragment(
    response: Response,
    selected: SessionConfig,
) -> None:
    assert response.status_code == 200
    assert re.search(
        rf'class="agent-card selected"\s+data-session-id="{selected.id}"\s+'
        r'data-selected="true"',
        response.text,
    )
    assert (
        f'<input type="hidden" name="session_id" value="{selected.id}">'
        in response.text
    )
    assert response.text.count('id="agent-sidebar"') == 1
    assert response.text.count('id="chat-composer"') == 1
    assert response.text.count('class="agent-card selected"') == 1
    assert response.text.count('aria-current="true"') == 1
    assert response.text.count('<details open class="agent-details">') == 1
    assert 'hx-swap-oob="outerHTML:#chat-composer"' in response.text
    assert 'hx-swap-oob="outerHTML:#agent-sidebar"' not in response.text
    assert 'id="chat-agent-select"' not in response.text
    assert '<section class="chat-timeline"' not in response.text
    assert 'class="live-round"' not in response.text


def test_canonical_project_name_chat_urls(tmp_path: Path) -> None:
    alpha = session("a" * 32, "Alpha")
    app, _, _ = setup_project(
        tmp_path,
        [alpha],
        project_name="Route Matrix",
    )
    project_prefix = "/projects/Route%20Matrix"

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"{project_prefix}/chat")

    assert response.status_code == 200
    assert f'href="{project_prefix}/settings"' in response.text
    assert f'hx-get="{project_prefix}/files"' in response.text
    assert f'hx-post="{project_prefix}/sessions"' in response.text


def test_chat_merges_rounds_deterministically_with_unique_composite_fragments(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[record(1, "2026-07-17T00:00:02Z")],
    )
    beta = session(
        "b" * 32,
        "Beta",
        rounds=[record(1, "2026-07-17T00:00:01Z")],
    )
    gamma = session(
        "c" * 32,
        "Gamma",
        rounds=[record(1, "2026-07-17T00:00:02Z")],
    )
    gamma.created_at = "2020-01-01T00:00:00Z"
    app, project, _ = setup_project(tmp_path, [alpha, beta, gamma])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        fragment = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/rounds/1"
        )
        chat_fragment = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/rounds/1?view=chat"
        )
        session_page = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}"
        )

    assert response.status_code == 200
    alpha_dom_id = f"round-{alpha.id}-1"
    beta_dom_id = f"round-{beta.id}-1"
    gamma_dom_id = f"round-{gamma.id}-1"
    assert response.text.count(f'id="{alpha_dom_id}"') == 1
    assert response.text.count(f'id="{beta_dom_id}"') == 1
    assert response.text.count(f'id="{gamma_dom_id}"') == 1
    timeline = response.text.split('<section class="chat-timeline"', 1)[1]
    assert timeline.index("Answer Beta") < timeline.index("Answer Alpha")
    assert timeline.index("Answer Alpha") < timeline.index("Answer Gamma")
    assert "Beta · Round 1" in response.text
    assert "Alpha · Round 1" in response.text
    assert fragment.status_code == 200
    assert f'id="{alpha_dom_id}"' in fragment.text
    assert "Alpha · Round 1" in fragment.text
    alpha_bubble = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    gamma_bubble = re.search(
        rf'<article[^>]+id="round-{gamma.id}-1".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert alpha_bubble is not None
    assert gamma_bubble is not None
    assert '<details class="round-details">' in alpha_bubble.group()
    assert '<details open class="round-details">' not in alpha_bubble.group()
    assert alpha_bubble.group().index(">Focus</button>") < alpha_bubble.group().index(
        '<details class="round-details">'
    )
    assert '<details open class="round-details">' in gamma_bubble.group()
    assert response.text.count('<details open class="round-details">') == 1
    assert chat_fragment.status_code == 200
    assert '<details class="round-details">' in chat_fragment.text
    assert '<details open class="round-details">' not in chat_fragment.text
    assert chat_fragment.text.index(">Focus</button>") < chat_fragment.text.index(
        '<details class="round-details">'
    )
    assert session_page.status_code == 200
    assert '<details class="round-details">' not in fragment.text
    assert '<details class="round-details">' not in session_page.text


def test_message_markdown_links_open_owned_files_in_reader_and_focus(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[record(1, "2026-07-17T00:00:01Z")],
    )
    app, project, store = setup_project(tmp_path, [alpha])
    docs = Path(project.path) / "docs"
    docs.mkdir()
    linked = docs / "review.md"
    linked.write_text("# Linked review", encoding="utf-8")
    (store.rounds_dir(alpha.id) / "round-01.md").write_text(
        f"[Relative](docs/review.md) [Absolute]({linked}:12)",
        encoding="utf-8",
    )
    view_url = f"/projects/{quote(project.name, safe='')}/files/view?path=docs%2Freview.md"
    focus_url = f"/projects/{quote(project.name, safe='')}/files/focus?path=docs%2Freview.md"

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        session_page = client.get(f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}")
        focused = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/rounds/1/focus"
        )
        opened = client.get(view_url)

    assert page.status_code == 200
    assert page.text.count(f'href="{view_url}"') == 2
    assert page.text.count(f'hx-get="{view_url}"') == 2
    assert page.text.count('hx-target="#file-reader"') == 2
    assert session_page.status_code == 200
    assert session_page.text.count(f'href="{view_url}"') == 2
    assert f'hx-get="{view_url}"' not in session_page.text
    assert focused.status_code == 200
    assert focused.text.count(f'href="{focus_url}"') == 2
    assert focused.text.count(f'hx-get="{focus_url}"') == 2
    assert focused.text.count('hx-target="#focus-dialog-content"') == 2
    assert opened.status_code == 200
    assert "docs/review.md" in opened.text
    assert "<h1>Linked review</h1>" in opened.text


def test_chat_empty_project_has_an_explicit_empty_timeline(tmp_path: Path) -> None:
    app, project, _ = setup_project(tmp_path, [])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat")
    assert response.status_code == 200
    assert '<p data-conversation-empty>No conversation yet.</p>' in response.text
    assert "Oldest → newest" in response.text
    assert "Create agent" in response.text
    assert "No agents yet. Create an agent to begin." in response.text
    assert "right panel" not in response.text
    assert 'id="chat-agent-select"' not in response.text
    assert 'id="chat-composer"' in response.text
    assert re.search(r'<textarea name="prompt"[^>]+disabled', response.text)
    assert re.search(
        r'<button[^>]*type="submit"[^>]*disabled[^>]*>\s*Send</button>',
        response.text,
    )


def test_active_auto_blocks_composer_without_auto_ownership_markers(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    alpha = session("a" * 32, "Alpha")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    with TestClient(app, base_url="http://localhost") as client:
        reserve_auto_run(store)
        response = client.get(f"/projects/{project.id}/chat?agent={alpha.id}")

    composer = composer_form(response.text)
    textarea = re.search(r'<textarea name="prompt"[^>]*>', composer)
    send = re.search(
        r'<button[^>]*type="submit"[^>]*>\s*Send</button>', composer
    )
    auto = re.search(r'<button[^>]*data-auto-open[^>]*>', composer)
    assert textarea is not None
    assert send is not None
    assert auto is not None
    assert 'data-auto-active="true"' in response.text

    for control in (textarea.group(), send.group()):
        assert "disabled" in control
        assert "data-composer-send" in control
        assert "data-disable-during-auto" not in control
        assert "data-auto-disabled" not in control

    # The Auto button remains in syncAutoDisabledControls' ownership domain.
    assert "disabled" in auto.group()
    assert "data-disable-during-auto" in auto.group()
    assert 'data-auto-disabled="true"' in auto.group()


def test_timeline_reuses_projection_auto_numbers_and_degrades_unmapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserve_auto_run,
) -> None:
    mapped_id = "a" * 32
    unmapped_id = "f" * 32
    alpha = session(
        "1" * 32,
        "Alpha",
        rounds=[
            auto_round(1, mapped_id),
            auto_round(2, unmapped_id),
        ],
    )
    beta = session("2" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    auto = reserve_auto_run(store, auto_id=mapped_id)
    auto.status = "converged"
    auto.finished_at = "2026-01-01T00:01:00Z"
    auto.terminal_reason = "all agents agreed"
    store.save_auto_run(auto)
    store.clear_auto_reservation(auto.id)
    monkeypatch.setattr(
        ProjectStore,
        "auto_number_map_for_view",
        lambda self: pytest.fail(
            "full Chat must reuse Auto numbers from ProjectAutoProjection"
        ),
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert response.status_code == 200
    project_prefix = f"/projects/{quote(project.name, safe='')}"
    assert f'href="{project_prefix}/auto-runs/1">Auto 1</a>' in response.text
    assert "<span>Auto</span>" in response.text
    assert "Auto aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in response.text


def test_recorded_auto_message_links_title_and_boxes_collapsed_prompt(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    auto_id = "e" * 32
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[auto_round(1, auto_id)],
    )
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    auto = reserve_auto_run(store, auto_id=auto_id)
    auto.status = "converged"
    auto.finished_at = "2026-01-01T00:01:00Z"
    auto.terminal_reason = "all agents agreed"
    store.save_auto_run(auto)
    store.clear_auto_reservation(auto.id)
    project_prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"{project_prefix}/chat")
        focused = client.get(
            f"{project_prefix}/sessions/{alpha.id}/rounds/1/focus"
        )
        stylesheet = client.get("/static/app.css")

    bubble = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert bubble is not None
    contents = bubble.group()
    auto_link = f'href="{project_prefix}/auto-runs/{auto.number}">Auto {auto.number}</a>'
    header = re.search(
        r'<div class="round-header">(.*?)</div>',
        contents,
        flags=re.DOTALL,
    )
    assert header is not None
    assert auto_link in header.group(1)
    assert contents.count(auto_link) == 1
    provenance = re.search(
        r'<p class="provenance auto-provenance">(.*?)</p>',
        contents,
        flags=re.DOTALL,
    )
    assert provenance is not None
    assert "discussion cycle 1" in provenance.group(1)
    assert "position 1" in provenance.group(1)
    assert "verdict continue" in provenance.group(1)
    assert "<a " not in provenance.group(1)
    prompt = re.search(
        r'<details class="round-prompt"([^>]*)>(.*?)</details>',
        contents,
        flags=re.DOTALL,
    )
    assert prompt is not None
    assert "open" not in prompt.group(1)
    assert 'class="round-prompt-box markdown"' in prompt.group(2)
    assert "Prompt Alpha" in prompt.group(2)
    assert focused.status_code == 200
    assert 'class="round-prompt-box markdown"' in focused.text
    assert "Prompt Alpha" in focused.text
    assert ".round-prompt-box" in stylesheet.text
    assert "border: 1px solid #8885;" in stylesheet.text


def test_chat_places_the_sole_auto_status_in_the_right_top_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserve_auto_run,
) -> None:
    alpha = session("a" * 32, "Alpha")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    with TestClient(app, base_url="http://localhost") as client:
        auto_run = reserve_auto_run(store)
        scan_calls = 0
        original = ProjectStore._scan_auto_directories

        def counted_auto_directory_scan(self: ProjectStore):
            nonlocal scan_calls
            scan_calls += 1
            return original(self)

        monkeypatch.setattr(
            ProjectStore,
            "_scan_auto_directories",
            counted_auto_directory_scan,
        )
        response = client.get(
            f"/projects/{quote(project.name, safe='')}/chat?agent={alpha.id}"
        )
        stylesheet = client.get("/static/app.css")

    assert response.status_code == 200
    assert response.text.count('id="auto-status-host"') == 1
    top_start = response.text.index('data-conversation-top')
    top_end = response.text.index('id="auto-setup-host"')
    top = response.text[top_start:top_end]
    assert top.index('id="chat-composer"') < top.index('id="auto-panel"')
    assert 'id="auto-status-host"' in top
    assert 'id="auto-status"' in top
    assert 'id="auto-history-index"' in top
    assert 'id="auto-history-view"' in top
    assert 'aria-label="Auto status and history"' in top
    assert scan_calls == 1
    assert 'id="auto-history-view" tabindex="-1"' in top
    assert 'aria-live=' not in top
    assert (
        f'id="auto-history-status-{auto_run.number}"'
        in top
    )
    assert 'grid-template-columns: minmax(20rem, 3fr) minmax(14rem, 2fr);' in (
        stylesheet.text
    )
    panel_rule = re.search(
        r"#auto-panel \{([^}]*)\}",
        stylesheet.text,
    )
    assert panel_rule is not None
    assert "align-self: stretch;" in panel_rule.group(1)
    assert "box-sizing: border-box;" in panel_rule.group(1)
    assert "contain: size;" in panel_rule.group(1)
    assert "min-height: 6rem;" in panel_rule.group(1)
    assert "max-height: 80vh;" in panel_rule.group(1)
    assert "resize: vertical;" in panel_rule.group(1)
    assert "max-height: min(12rem, 18vh);" not in panel_rule.group(1)
    assert "font-size: .85rem;" in panel_rule.group(1)
    assert "padding: .5rem;" in panel_rule.group(1)
    assert "overflow: hidden auto;" in panel_rule.group(1)
    assert "overflow-y: auto;" not in panel_rule.group(1)
    assert (
        "#auto-panel :is(button, input, select, textarea) "
        "{ font: inherit; }"
        in stylesheet.text
    )
    assert (
        "#auto-panel :is(h2, h3, h4, p) { margin: .15rem 0; }"
        in stylesheet.text
    )
    assert (
        "#auto-panel form { gap: .35rem; margin: .25rem 0; }"
        in stylesheet.text
    )
    auto_status_rule = re.search(
        r"#auto-panel \.auto-status \{([^}]*)\}",
        stylesheet.text,
    )
    assert auto_status_rule is not None
    assert "border: 0" in auto_status_rule.group(1)
    assert "max-height: none" in auto_status_rule.group(1)
    assert "overflow: visible" in auto_status_rule.group(1)
    assert "padding: 0" in auto_status_rule.group(1)


def test_chat_workspace_renders_four_regions_and_full_agent_information(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        agent="claude",
        role_instructions="Be rigorous & challenge <claims>.",
        rounds=[record(1, "2026-07-17T00:00:01Z")],
    )
    beta = session(
        "b" * 32,
        "Beta",
        agent="codex",
        role_instructions="",
    )
    app, project, _ = setup_project(tmp_path, [alpha, beta])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={alpha.id}")
        stylesheet = client.get("/static/app.css")

    assert response.status_code == 200
    assert '<body class="chat-page">' in response.text
    for region in ("topic", "files", "conversation", "agents"):
        assert f'data-workspace-region="{region}"' in response.text
    assert 'id="file-browser-host"' in response.text
    assert 'id="file-reader"' in response.text
    assert 'id="chat-agent-select"' not in response.text
    assert response.text.count('<details open class="agent-details">') == 1
    assert response.text.count('<details class="agent-details">') == 1
    assert "Be rigorous &amp; challenge &lt;claims&gt;." in response.text
    assert "No role instructions" in response.text
    assert "Role instructions are fixed after the first round." not in response.text
    assert "body.chat-page { max-width: none" in stylesheet.text
    assert "grid-template-areas:" in stylesheet.text
    assert "#file-browser-host, #file-reader { min-height: 0; overflow: auto; }" in stylesheet.text


def test_chat_uses_compact_composer_and_wider_agent_rail(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", agent="claude")
    app, project, _ = setup_project(tmp_path, [alpha])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={alpha.id}")
        css = client.get("/static/app.css")

    assert response.status_code == 200
    assert css.status_code == 200
    assert 'class="conversation-controls"' not in response.text
    assert 'id="chat-agent-select"' not in response.text
    assert response.text.index('id="chat-composer"') < response.text.index(
        'id="chat-errors"'
    )
    assert 'name="prompt"' in response.text
    assert "body.chat-page { padding-top: .5rem; }" in css.text
    assert "body.chat-page > header { margin-bottom: .5rem; }" in css.text
    assert (
        ".workspace-topic h1, .workspace-topic p { margin: .1rem 0; }"
        in css.text
    )
    assert (
        "grid-template-columns: minmax(14rem, 1fr) minmax(36rem, 4fr) "
        "minmax(12rem, .7fr)" in css.text
    )
    assert (
        "grid-template-rows: minmax(32rem, calc(100vh - 4rem))"
        in css.text
    )
    assert "#chat-composer textarea { min-height: 3rem; }" in css.text
    assert (
        ".workspace-agents {\n"
        "  display: grid;\n"
        "  font-size: .85rem;" in css.text
    )
    assert (
        ".workspace-agents :is(input, select, textarea, button) { font: inherit; }"
        in css.text
    )


def test_chat_topic_header_omits_provider_app_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_health = [
        ProviderHealth(
            "claude", "claude", True, "2.1.202 (Claude Code)", None
        ),
        ProviderHealth("codex", "codex", True, "codex-cli 0.153.4", None),
    ]

    async def deterministic_probe_all(
        _commands: dict[str, str],
    ) -> list[ProviderHealth]:
        return provider_health

    monkeypatch.setattr("app.main.probe_all", deterministic_probe_all)
    alpha = session("a" * 32, "Alpha", agent="claude")
    app, project, _ = setup_project(tmp_path, [alpha])
    with TestClient(app, base_url="http://localhost") as client:
        app.state.health = provider_health
        response = client.get(f"/projects/{project.name}/chat")
        stylesheet = client.get("/static/app.css")

    topic_header = response.text.split('class="workspace-topic"', 1)[1].split(
        "</section>", 1
    )[0]
    footer = response.text.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'class="topic-health"' not in topic_header
    assert "2.1.202 (Claude Code)" not in topic_header
    assert "codex-cli 0.153.4" not in topic_header
    assert 'id="usage-badge"' in topic_header
    assert "2.1.202 (Claude Code)" in footer
    assert "codex-cli 0.153.4" in footer
    assert ".topic-health" not in stylesheet.text


def test_chat_uses_left_agent_selection_and_right_file_rail(tmp_path: Path) -> None:
    alpha = session("a" * 32, "Alpha", agent="claude")
    beta = session("b" * 32, "Beta", agent="codex")
    app, project, _ = setup_project(tmp_path, [alpha, beta])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={beta.id}")
        selected = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/select", params={"agent": alpha.id}
        )
        css = client.get("/static/app.css")

    assert response.status_code == 200
    assert css.status_code == 200
    agents = re.search(
        r'<section class="workspace-agents"[^>]*>(?P<body>.*?)</section>\s*'
        r'<section\s+class="workspace-conversation"',
        response.text,
        flags=re.DOTALL,
    )
    files = re.search(
        r'<aside\s+class="workspace-files"[^>]*>(?P<body>.*?)</aside>',
        response.text,
        flags=re.DOTALL,
    )
    assert agents is not None
    assert files is not None
    assert agents["body"].index('data-workspace-region="topic"') < agents[
        "body"
    ].index('id="agent-sidebar"')
    assert 'data-workspace-region="topic"' not in files["body"]
    assert files["body"].index('id="file-browser-host"') < files["body"].index(
        'id="file-reader"'
    )
    assert '<section id="file-reader" aria-label="File reader" tabindex="-1">' in files[
        "body"
    ]
    assert '<p class="file-reader-empty">' in files["body"]
    assert 'id="chat-agent-select"' not in response.text
    assert "Choose agent" not in response.text
    beta_card = re.search(
        rf'<article\s+class="agent-card selected"\s+'
        rf'data-session-id="{beta.id}".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert beta_card is not None
    assert f'id="agent-select-{beta.id}"' in beta_card.group()
    assert (
        f'hx-get="/projects/{quote(project.name, safe='')}/chat/select?agent={beta.id}"'
        in beta_card.group()
    )
    assert 'hx-target="#agent-sidebar"' in beta_card.group()
    assert 'aria-current="true"' in beta_card.group()
    assert '<details open class="agent-details">' in beta_card.group()
    assert response.text.count('<details open class="agent-details">') == 1
    assert (
        f'<input type="hidden" name="session_id" value="{beta.id}">'
        in response.text
    )
    assert_synchronized_selection_fragment(selected, alpha)
    assert selected.headers["hx-push-url"] == (
        f"/projects/{quote(project.name, safe='')}/chat?agent={alpha.id}"
    )
    assert '"agents conversation files";' in css.text
    assert (
        "grid-template-columns: minmax(14rem, 1fr) minmax(36rem, 4fr) "
        "minmax(12rem, .7fr)" in css.text
    )
    assert re.search(
        r"\.workspace-agents\s*\{[^}]*min-width:\s*0;",
        css.text,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.workspace-files\s*\{[^}]*min-width:\s*0;",
        css.text,
        flags=re.DOTALL,
    )
    assert "#chat-composer textarea { min-height: 3rem; }" in css.text
    assert ".file-reader-empty { opacity: .55; }" in css.text
    assert ".conversation-controls" not in css.text
    assert ".chat-agent-select" not in css.text


def test_file_browser_links_target_only_the_reader_for_display_errors(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha")
    app, project, _ = setup_project(tmp_path, [alpha])
    (Path(project.path) / "notes.txt").write_text("Read me", encoding="utf-8")
    (Path(project.path) / "archive.bin").write_bytes(b"not rendered")

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        listing = client.get(f"/projects/{quote(project.name, safe='')}/files")
        error = client.get(
            f"/projects/{quote(project.name, safe='')}/files/view",
            params={"path": "archive.bin"},
        )

    assert page.status_code == 200
    assert f'hx-get="/projects/{quote(project.name, safe='')}/files"' in page.text
    archive_link = re.search(
        rf'<a\s+href="([^"]*archive\.bin)".*?</a>',
        listing.text,
        flags=re.DOTALL,
    )
    assert archive_link is not None
    assert f'hx-get="{archive_link.group(1)}"' in archive_link.group()
    assert 'hx-target="#file-reader"' in archive_link.group()
    assert 'hx-swap="innerHTML"' in archive_link.group()
    assert error.status_code == 200
    assert 'class="file-error" role="status"' in error.text
    assert "This file type cannot be displayed." in error.text
    assert 'id="chat-errors"' not in error.text


def test_round_focus_fragment_is_static_and_keeps_complete_dom_ids_unique(
    tmp_path: Path,
) -> None:
    alpha_round = record(1, "2026-07-17T00:00:01Z")
    alpha_round.warnings = ["Fallback context was used."]
    alpha_round.shared_context = SharedContextDescriptor(
        path="docs/<brief>.md",
        staged_file="inputs/round-01/shared-context.md",
        sha256="b" * 64,
    )
    alpha = session("a" * 32, "Alpha", rounds=[alpha_round])
    beta = session("b" * 32, "Beta", mode="sleep")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    beta_base = f"/projects/{quote(project.name, safe='')}/sessions/{beta.id}"
    focus_url = (
        f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/rounds/1/focus"
    )

    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(f"{beta_base}/run", data={"prompt": "B"}).status_code == 202
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        focused = client.get(focus_url)
        missing_session = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{'c' * 32}/rounds/1/focus"
        )
        missing_round = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/rounds/2/focus"
        )
        assert store.load_session(beta.id).status == "running"
        assert client.post(f"{beta_base}/cancel").status_code == 200

    assert page.status_code == 200
    assert 'id="focus-dialog"' in page.text
    assert 'aria-label="Focused content"' in page.text
    assert 'id="focus-dialog-content"' in page.text
    assert "Select Focus on a recorded round or opened file to inspect it." in page.text
    assert f'id="round-{beta.id}-1"' in page.text
    alpha_bubble = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        page.text,
        flags=re.DOTALL,
    )
    assert alpha_bubble is not None
    assert f'hx-get="{focus_url}"' in alpha_bubble.group()
    assert 'hx-target="#focus-dialog-content"' in alpha_bubble.group()
    assert focused.status_code == 200
    assert f'id="focus-{alpha.id}-1"' in focused.text
    assert f'id="round-{alpha.id}-1"' not in focused.text
    assert "Prompt Alpha" in focused.text
    assert "Answer Alpha" in focused.text
    assert "Fallback context was used." in focused.text
    for forbidden in (
        "hx-ext=",
        "sse-connect=",
        "class=\"live-round\"",
        ">Focus<",
        ">Pass<",
        "Send to…",
    ):
        assert forbidden not in focused.text
    assert f'id="round-{beta.id}-1"' not in focused.text
    # A real id attribute is never preceded by "-" or a word character, so this
    # skips data-session-id and data-auto-id, whose values repeat by design.
    page_ids = re.findall(r'(?<![-\w])id="([^"]+)"', page.text)
    focus_ids = re.findall(r'(?<![-\w])id="([^"]+)"', focused.text)
    combined_ids = page_ids + focus_ids
    assert len(combined_ids) == len(set(combined_ids))
    for rendered in (page.text, focused.text):
        assert "docs/&lt;brief&gt;.md" in rendered
        assert "inputs/round-01/shared-context.md" in rendered
        assert "b" * 64 in rendered
        assert "docs/<brief>.md" not in rendered
    assert missing_session.status_code == 404
    assert missing_round.status_code == 404


def test_chat_retry_targets_timeline_only_for_error_records(tmp_path: Path) -> None:
    error_record = record(1, "2026-07-17T00:00:01Z")
    error_record.status = "error"
    error_record.error = "provider rejected request"
    cancelled_record = record(2, "2026-07-17T00:00:02Z")
    cancelled_record.status = "cancelled"
    cancelled_record.error = "cancelled by user"
    complete_record = record(3, "2026-07-17T00:00:03Z")
    config = session(
        "a" * 32,
        "Alpha",
        rounds=[error_record, cancelled_record, complete_record],
    )
    app, project, store = setup_project(tmp_path, [config])
    (store.rounds_dir(config.id) / "round-99.md").write_text(
        "orphan",
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    retry_url = (
        f"/projects/{quote(project.name, safe='')}/sessions/{config.id}/rounds/1/retry?view=chat"
    )
    assert page.status_code == 200
    assert f'action="{retry_url}"' in page.text
    assert f'hx-post="{retry_url}"' in page.text
    assert 'hx-target=".chat-timeline"' in page.text
    assert 'hx-swap="beforeend"' in page.text
    assert page.text.count(">Retry</button>") == 1


def test_chat_renders_two_concurrent_live_fragments_with_scoped_done_targets(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    beta = session("b" * 32, "Beta", mode="sleep")
    app, project, _ = setup_project(tmp_path, [alpha, beta])
    alpha_base = f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}"
    beta_base = f"/projects/{quote(project.name, safe='')}/sessions/{beta.id}"

    with TestClient(app, base_url="http://localhost") as client:
        alpha_started = client.post(f"{alpha_base}/run", data={"prompt": "A"})
        beta_started = client.post(f"{beta_base}/run", data={"prompt": "B"})
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat")

        alpha_dom_id = f"round-{alpha.id}-1"
        beta_dom_id = f"round-{beta.id}-1"
        assert f'id="{alpha_dom_id}"' in alpha_started.text
        assert f'hx-target="#{alpha_dom_id}"' in alpha_started.text
        assert f'id="{beta_dom_id}"' in beta_started.text
        assert f'hx-target="#{beta_dom_id}"' in beta_started.text
        assert response.text.count('class="live-round"') == 2
        assert response.text.count(f'id="{alpha_dom_id}"') == 1
        assert response.text.count(f'id="{beta_dom_id}"') == 1
        assert "Alpha · Round 1 · running" in response.text
        assert "Beta · Round 1 · running" in response.text
        assert response.text.count("Running…") == 2

        assert client.post(f"{alpha_base}/cancel").status_code == 200
        assert client.post(f"{beta_base}/cancel").status_code == 200


def test_chat_selection_is_deterministic_and_invalid_selection_is_rejected(
    tmp_path: Path,
) -> None:
    zulu = session("a" * 32, "Zulu")
    alpha = session("b" * 32, "alpha")
    app, project, _ = setup_project(tmp_path, [zulu, alpha])

    with TestClient(app, base_url="http://localhost") as client:
        default = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        explicit = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={zulu.id}")
        sidebar = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/sidebar?agent={zulu.id}"
        )
        malformed = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent=not-an-id")
        missing = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={'c' * 32}")

    for response, selected in ((default, alpha), (explicit, zulu)):
        assert response.text.count('id="agent-sidebar"') == 1
        assert response.text.count('id="chat-composer"') == 1
        assert response.text.count('class="agent-card selected"') == 1
        assert response.text.count('aria-current="true"') == 1
        assert response.text.count('<details open class="agent-details">') == 1
        assert re.search(
            rf'class="agent-card selected"\s+data-session-id="{selected.id}"\s+'
            r'data-selected="true"',
            response.text,
        )
        assert (
            f'<input type="hidden" name="session_id" value="{selected.id}">'
            in response.text
        )
        assert 'hx-swap-oob="outerHTML:#chat-composer"' not in response.text
        assert '<section class="chat-timeline"' in response.text
    assert malformed.status_code == 422
    assert missing.status_code == 404
    assert sidebar.status_code == 200
    assert sidebar.text.count('id="agent-sidebar"') == 1
    assert sidebar.text.count('id="chat-composer"') == 0
    assert sidebar.text.count('class="agent-card selected"') == 1
    assert sidebar.text.count('aria-current="true"') == 1
    assert sidebar.text.count('<details open class="agent-details">') == 1
    assert 'hx-swap-oob=' not in sidebar.text
    assert '<section class="chat-timeline"' not in sidebar.text
    assert 'class="live-round"' not in sidebar.text
    assert "No rounds yet." in sidebar.text


def test_selection_transaction_supports_both_providers_and_rejects_invalid_ids(
    tmp_path: Path,
) -> None:
    claude = session("a" * 32, "Claude researcher", agent="claude")
    codex = session("b" * 32, "Codex critic", agent="codex")
    app, project, _ = setup_project(tmp_path, [claude, codex])
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    other = RegistryStore(tmp_path / "home").register("Other", other_path)
    outsider = session("c" * 32, "Outsider", agent="codex")
    ProjectStore(other).create_session(outsider)

    with TestClient(app, base_url="http://localhost") as client:
        claude_selected = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/select",
            params={"agent": claude.id},
        )
        codex_selected = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/select",
            params={"agent": codex.id},
        )
        malformed = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/select",
            params={"agent": "bad"},
        )
        cross_project = client.get(
            f"/projects/{quote(project.name, safe='')}/chat/select",
            params={"agent": outsider.id},
        )

    for response, selected in (
        (claude_selected, claude),
        (codex_selected, codex),
    ):
        assert_synchronized_selection_fragment(response, selected)
        assert response.headers["hx-push-url"] == (
            f"/projects/{quote(project.name, safe='')}/chat?agent={selected.id}"
        )
        assert f"{selected.agent} · {selected.model}" in response.text
    assert malformed.status_code == 422
    assert cross_project.status_code == 404


def test_create_and_edit_keep_all_selection_projections_in_sync_during_live_runs(
    tmp_path: Path,
) -> None:
    claude = session("a" * 32, "Claude", agent="claude", mode="sleep")
    codex = session("b" * 32, "Codex", agent="codex", mode="sleep")
    observer = session("c" * 32, "Observer", agent="claude")
    app, project, store = setup_project(tmp_path, [claude, codex, observer])
    claude_base = f"/projects/{quote(project.name, safe='')}/sessions/{claude.id}"
    codex_base = f"/projects/{quote(project.name, safe='')}/sessions/{codex.id}"

    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(f"{claude_base}/run", data={"prompt": "A"}).status_code == 202
        assert client.post(f"{codex_base}/run", data={"prompt": "B"}).status_code == 202
        created = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions",
            headers={"HX-Request": "true"},
            data={
                "name": "New agent",
                "agent": "codex",
                "model": "success",
                "effort": "low",
                "role_instructions": "New role",
            },
        )
        new_agent = next(item for item in store.list_sessions() if item.name == "New agent")
        selected_edit = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{new_agent.id}/edit?agent={new_agent.id}",
            headers={"HX-Request": "true"},
            data={"model": "success", "effort": "medium"},
        )
        unselected_edit = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{observer.id}/edit?agent={new_agent.id}",
            headers={"HX-Request": "true"},
            data={"model": "success", "effort": "high"},
        )
        assert client.post(f"{claude_base}/cancel").status_code == 200
        assert client.post(f"{codex_base}/cancel").status_code == 200

    for response in (created, selected_edit, unselected_edit):
        assert_synchronized_selection_fragment(response, new_agent)
    assert created.headers["hx-push-url"] == (
        f"/projects/{quote(project.name, safe='')}/chat?agent={new_agent.id}"
    )
    assert '<p class="immutable-agent-name">New agent</p>' in selected_edit.text
    assert '<p class="immutable-agent-name">Observer</p>' in unselected_edit.text
    assert 'label>Name <input name="name"' not in selected_edit.text
    assert store.load_session(new_agent.id).effort == "medium"
    assert store.load_session(observer.id).effort == "high"
    assert store.load_session(claude.id).status == "idle"
    assert store.load_session(codex.id).status == "idle"


def test_chat_dispatch_validates_membership_and_runs_the_selected_agent(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    run_url = f"/projects/{quote(project.name, safe='')}/chat/run"
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    registry = RegistryStore(tmp_path / "home")
    other_project = registry.register("Other", other_path)
    outsider = session("c" * 32, "Outsider")
    ProjectStore(other_project).create_session(outsider)

    with TestClient(app, base_url="http://localhost") as client:
        invalid = client.post(run_url, data={"session_id": "bad", "prompt": "Hi"})
        missing = client.post(
            run_url,
            data={"session_id": outsider.id, "prompt": "Hi"},
        )
        started = client.post(
            run_url,
            data={"session_id": beta.id, "prompt": "Ask Beta"},
        )
        assert started.status_code == 202
        assert f'id="round-{beta.id}-1"' in started.text
        with client.stream(
            "GET",
            f"/projects/{quote(project.name, safe='')}/sessions/{beta.id}/rounds/1/stream",
        ) as response:
            response.read()

    assert invalid.status_code == 422
    assert missing.status_code == 422
    assert store.load_session(alpha.id).rounds == []
    assert store.load_session(beta.id).rounds[0].status == "complete"


def test_hx_create_selects_first_agent_and_updates_sidebar_composer_and_url(
    tmp_path: Path,
) -> None:
    app, project, store = setup_project(tmp_path, [])
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions",
            headers={"HX-Request": "true"},
            data={
                "name": "Researcher",
                "agent": "claude",
                "model": "sonnet",
                "effort": "low",
                "role_instructions": "Be rigorous.",
            },
            follow_redirects=False,
        )

    created = store.list_sessions()[0]
    assert_synchronized_selection_fragment(response, created)
    assert response.headers["hx-push-url"] == (
        f"/projects/{quote(project.name, safe='')}/chat?agent={created.id}"
    )
    assert 'name="prompt"' in response.text


def test_hx_edit_preserves_selection_without_replacing_another_live_stream(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    beta = session("b" * 32, "Beta", agent="claude")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    alpha_base = f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}"

    with TestClient(app, base_url="http://localhost") as client:
        assert client.post(f"{alpha_base}/run", data={"prompt": "Keep running"}).status_code == 202
        edited = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{beta.id}/edit?agent={beta.id}",
            headers={"HX-Request": "true"},
            data={"model": "success", "effort": "medium"},
            follow_redirects=False,
        )
        assert_synchronized_selection_fragment(edited, beta)
        assert '<p class="immutable-agent-name">Beta</p>' in edited.text
        assert store.load_session(beta.id).effort == "medium"
        assert store.load_session(alpha.id).status == "running"
        assert client.post(f"{alpha_base}/cancel").status_code == 200


def test_busy_and_validation_errors_have_safe_visible_chat_contract(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", mode="sleep")
    app, project, _ = setup_project(tmp_path, [alpha])
    run_url = f"/projects/{quote(project.name, safe='')}/chat/run"
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        assert 'id="chat-errors"' in page.text
        assert 'aria-live="polite"' in page.text
        assert client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "Keep running"},
        ).status_code == 202
        busy = client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "Again"},
        )
        invalid = client.post(
            run_url,
            data={"session_id": alpha.id, "prompt": "   "},
        )
        assert client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/cancel"
        ).status_code == 200

    assert busy.status_code == 409
    assert "running" in busy.json()["detail"]
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], str)


def test_sidebar_preview_is_plain_truncated_and_send_targets_exclude_source(
    tmp_path: Path,
) -> None:
    alpha_record = record(1, "2026-07-17T00:00:01Z")
    beta_record = record(1, "2026-07-17T00:00:02Z")
    beta_record.status = "error"
    beta_record.error = "provider failed"
    alpha = session("a" * 32, "Alpha", rounds=[alpha_record])
    beta = session("b" * 32, "Beta", rounds=[beta_record])
    app, project, store = setup_project(tmp_path, [alpha, beta])
    (store.rounds_dir(alpha.id) / "round-01.md").write_text(
        "<b>unsafe</b> " + "x" * 300,
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat?agent={alpha.id}")

    assert "&lt;b&gt;unsafe&lt;/b&gt;" in response.text
    assert "<b>unsafe</b>" not in response.text
    assert "…" in response.text
    beta_card = re.search(
        rf'<article\s+class="agent-card[^>]*"\s+data-session-id="{beta.id}".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert beta_card is not None
    assert "Latest round 1 · error" in beta_card.group()
    assert "Answer Beta" in beta_card.group()
    assert "provider failed" in beta_card.group()
    alpha_bubble = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        response.text,
        flags=re.DOTALL,
    )
    assert alpha_bubble is not None
    assert "Send to…" in alpha_bubble.group()
    assert 'hx-target=".chat-timeline"' in alpha_bubble.group()
    assert f'<option value="{alpha.id}">' not in alpha_bubble.group()
    assert f'<option value="{beta.id}">' in alpha_bubble.group()


def test_preparation_is_visible_in_chat_timeline_but_hidden_from_sidebar_preview(
    tmp_path: Path,
) -> None:
    manual = record(1, "2026-07-17T00:00:01Z")
    preparation = record(2, "2026-07-17T00:00:02Z")
    preparation.source = SourceDescriptor(type="auto")
    preparation.auto = AutoRoundDescriptor(
        auto_id="c" * 32,
        phase="preparation",
        cycle=None,
        position=0,
        context_file="inputs/round-02/auto-context.md",
        context_sha256="d" * 64,
    )
    alpha = session("a" * 32, "Alpha", rounds=[manual, preparation])
    app, project, store = setup_project(tmp_path, [alpha])
    rounds = store.rounds_dir(alpha.id)
    (rounds / "round-01.md").write_text("Public answer", encoding="utf-8")
    (rounds / "round-02.md").write_text(
        "Visible preparation",
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        detail = client.get(f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}")

    assert chat.text.index("Public answer") < chat.text.index(
        "Visible preparation"
    )
    assert "Auto preparation" in chat.text
    assert "Latest round 1" in chat.text
    assert "Visible preparation" in detail.text


def test_chat_shows_what_a_round_cost_and_stays_silent_when_nothing_was_reported(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[
            billed_round(
                1,
                TurnUsage(
                    input_tokens=32018,
                    output_tokens=5,
                    cache_read_tokens=31872,
                    total_cost_usd=0.0130533,
                ),
            ),
            billed_round(2, None),
        ],
    )
    app, project, _ = setup_project(tmp_path, [alpha])
    project_prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"{project_prefix}/chat")
        focused = client.get(f"{project_prefix}/sessions/{alpha.id}/rounds/1/focus")

    bubbles = {
        n: re.search(
            rf'<article[^>]+id="round-{alpha.id}-{n}".*?</article>',
            response.text,
            flags=re.DOTALL,
        )
        for n in (1, 2)
    }
    assert all(bubble is not None for bubble in bubbles.values())
    billed = bubbles[1].group()
    assert "32,018 in" in billed
    assert "5 out" in billed
    assert "31,872 cache read" in billed
    assert "$0.0131" in billed
    # Unreported figures are absent, never rendered as a fabricated zero.
    assert "cache write" not in billed
    assert 'class="provenance usage"' not in bubbles[2].group()
    assert "32,018 in" in focused.text


def quota_round(round_n: int, *, auto_id: str | None) -> RoundRecord:
    """A round the provider refused for quota: stored as an error, shown as a pause."""

    return RoundRecord(
        n=round_n,
        status="error",
        error="discussion provider failed: API Error: 429 rate limit exceeded",
        warnings=[],
        agent="claude",
        model="success",
        effort="low",
        started_at=f"2026-01-01T00:00:0{round_n}Z",
        finished_at=f"2026-01-01T00:00:0{round_n}Z",
        source=SourceDescriptor(type="auto" if auto_id else "user"),
        auto=(
            AutoRoundDescriptor(
                auto_id=auto_id,
                phase="discussion",
                cycle=1,
                position=0,
                context_file=f"inputs/round-{round_n:02d}/auto-context.md",
                context_sha256="0" * 64,
                verdict="continue",
            )
            if auto_id
            else None
        ),
        error_category="quota",
    )


def failed_round(round_n: int) -> RoundRecord:
    return RoundRecord(
        n=round_n,
        status="error",
        error="agent stream failed",
        warnings=[],
        agent="claude",
        model="success",
        effort="low",
        started_at=f"2026-01-01T00:00:0{round_n}Z",
        finished_at=f"2026-01-01T00:00:0{round_n}Z",
        source=SourceDescriptor(type="user"),
        error_category="permanent",
    )


def test_a_quota_round_renders_as_a_pause_while_other_failures_stay_red(
    tmp_path: Path,
) -> None:
    # The stored record must not change -- reconciliation and every status query
    # depend on `status == "error"` -- so only the presentation branches, on the
    # folded category.
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[quota_round(1, auto_id="c" * 32), failed_round(2)],
    )
    app, project, _ = setup_project(tmp_path, [alpha])
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"{prefix}/chat")
        focus = client.get(f"{prefix}/sessions/{alpha.id}/rounds/1/focus")

    paused = re.search(
        rf'<article[^>]+id="round-{alpha.id}-1".*?</article>',
        page.text,
        flags=re.DOTALL,
    ).group()
    crashed = re.search(
        rf'<article[^>]+id="round-{alpha.id}-2".*?</article>',
        page.text,
        flags=re.DOTALL,
    ).group()

    assert "quota-paused" in paused.split(">", 1)[0]
    assert "round error" not in paused
    assert '<p class="error">' not in paused
    assert '<p class="notice" role="status">' in paused
    assert "Auto paused: Claude quota limit reached." in paused
    # Nothing is hidden: the provider's own words stay, collapsed.
    assert "429 rate limit exceeded" in paused
    assert "<summary>Provider message</summary>" in paused
    # A genuine failure is untouched.
    assert "round error" in crashed.split(">", 1)[0]
    assert '<p class="error">agent stream failed</p>' in crashed
    # The focused view tells the same story.
    assert '<p class="notice" role="status">' in focus.text
    assert '<p class="error">' not in focus.text
    assert "quota-paused" in focus.text
    assert "429 rate limit exceeded" in focus.text


def test_a_manual_quota_round_does_not_claim_auto_was_paused(tmp_path: Path) -> None:
    # A 429 on a hand-sent prompt paused nothing; saying "Auto paused" there
    # would be a fabricated explanation.
    alpha = session("a" * 32, "Alpha", rounds=[quota_round(1, auto_id=None)])
    app, project, _ = setup_project(tmp_path, [alpha])

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert "Auto paused" not in page.text
    assert "Claude quota limit reached; this turn did not run." in page.text
    assert '<p class="notice" role="status">' in page.text
    # Retry stays available: this round is not Auto-owned.
    assert ">Retry</button>" in page.text


def test_chat_prompt_rereads_codex_quota_once_for_the_provider_it_spends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One check per prompt, scoped to the provider about to be spent: reading
    # the rollout stamps `observed_at` with the read time, so an unconditional
    # re-read would let a non-Codex prompt keep an ageing Codex figure alive.
    codex_agent = session("a" * 32, "Coder", agent="codex")
    other_agent = session("b" * 32, "Writer")
    app, project, _ = setup_project(tmp_path, [codex_agent, other_agent])
    run_url = f"/projects/{quote(project.name, safe='')}/chat/run"
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    reads: list[Path] = []

    def fake_scan(codex_home: Path, **_kwargs) -> CodexRolloutState:
        reads.append(codex_home)
        return CodexRolloutState(
            [
                RateLimitReading(
                    window="five_hour",
                    used_percent=64.0,
                    status="unknown",
                    resets_at=reset,
                    source="codex_rollout_token_count",
                )
            ]
        )

    monkeypatch.setattr("app.runner.read_latest_codex_rollout_state", fake_scan)

    with TestClient(app, base_url="http://localhost") as client:
        codex_run = client.post(
            run_url,
            data={"session_id": codex_agent.id, "prompt": "Ask the coder"},
        )
        with client.stream(
            "GET",
            f"/projects/{quote(project.name, safe='')}/sessions/{codex_agent.id}"
            "/rounds/1/stream",
        ) as stream:
            stream.read()
        after_codex = len(reads)
        other_run = client.post(
            run_url,
            data={"session_id": other_agent.id, "prompt": "Ask the writer"},
        )
        with client.stream(
            "GET",
            f"/projects/{quote(project.name, safe='')}/sessions/{other_agent.id}"
            "/rounds/1/stream",
        ) as stream:
            stream.read()

    assert codex_run.status_code == 202
    assert other_run.status_code == 202
    assert after_codex == 1
    assert len(reads) == 1
    # The check is only meaningful if it is visible, so the chat response
    # carries the refreshed badge out of band.
    assert 'id="usage-badge"' in codex_run.text
    assert 'hx-swap-oob="outerHTML"' in codex_run.text
    codex_row = codex_run.text.split('data-provider="codex"', 1)[1].split(
        "</tr>", 1
    )[0]
    assert 'data-window="five_hour">36% remaining' in codex_row


def test_session_view_prompt_carries_no_out_of_band_usage_badge(
    tmp_path: Path,
) -> None:
    # Only the chat view renders `#usage-badge`; an out-of-band swap on the
    # session view would log a missing-target error on every send.
    alpha = session("a" * 32, "Alpha")
    app, project, _ = setup_project(tmp_path, [alpha])

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}/run",
            data={"prompt": "Hello"},
        )
        with client.stream(
            "GET",
            f"/projects/{quote(project.name, safe='')}/sessions/{alpha.id}"
            "/rounds/1/stream",
        ) as stream:
            stream.read()

    assert response.status_code == 202
    assert "usage-badge" not in response.text


def test_agent_card_shows_last_observed_occupancy_and_forgets_retired_ones(
    tmp_path: Path,
) -> None:
    alpha = session("a" * 32, "Alpha", rounds=[record(1, "2026-01-01T00:00:01Z")])
    app, project, store = setup_project(tmp_path, [alpha])
    prefix = f"/projects/{quote(project.name, safe='')}"
    observation = ContextObservation(
        used_tokens=42_970,
        context_window=1_000_000,
        numerator_source="claude_final_assistant",
        resolved_model="claude-sonnet-5",
        round_n=1,
        observed_at="2026-01-01T00:00:02Z",
    )

    with TestClient(app, base_url="http://localhost") as client:
        config = store.load_session(alpha.id)
        config.context_observation = observation
        store.save_session(config)
        observed = client.get(f"{prefix}/chat")
        # Clear normally nulls the observation; a boundary that moved past it
        # by any other route must not resurrect a window that is gone.
        config = store.load_session(alpha.id)
        config.context_baseline_round = 1
        store.save_session(config)
        retired = client.get(f"{prefix}/chat")
        # A provider that reports no window shows the numerator, not a guess.
        config = store.load_session(alpha.id)
        config.context_baseline_round = 0
        config.context_observation = replace(observation, context_window=None)
        store.save_session(config)
        no_window = client.get(f"{prefix}/chat")

    flat = lambda response: " ".join(response.text.split())
    assert "42,970 / 1,000,000 tokens (4%) · last observed round 1" in flat(observed)
    assert "Window occupancy unknown" in flat(retired)
    assert "42,970" not in retired.text
    assert "42,970 tokens · window unknown" in flat(no_window)


def test_chat_offers_clear_and_compact_and_both_change_the_boundary(
    tmp_path: Path,
) -> None:
    alpha = session(
        "a" * 32,
        "Alpha",
        rounds=[record(1, "2026-01-01T00:00:01Z"), record(2, "2026-01-01T00:00:02Z")],
    )
    app, project, store = setup_project(tmp_path, [alpha])
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        before = client.get(f"{prefix}/chat")
        compacted = client.post(
            f"{prefix}/sessions/{alpha.id}/context/compact?view=chat"
        )
        with client.stream(
            "GET",
            f"{prefix}/sessions/{alpha.id}/rounds/3/stream",
        ) as stream:
            stream.read()
        after_compact = client.get(f"{prefix}/chat")
        summarized = store.load_session(alpha.id)
        cleared = client.post(
            f"{prefix}/sessions/{alpha.id}/context/clear?agent={alpha.id}"
        )

    assert ">Compact</button>" in before.text
    assert ">Clear context</button>" in before.text
    assert "Full conversation" in before.text

    assert compacted.status_code == 202
    assert summarized.context_summary is not None
    assert summarized.context_baseline_round == 3
    assert "Summarized through round 3" in after_compact.text

    assert cleared.status_code == 200
    assert "Cleared through round 3" in cleared.text
    emptied = store.load_session(alpha.id)
    assert emptied.context_summary is None
    assert emptied.context_baseline_round == 3


def test_chat_error_javascript_contract_runs_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node unavailable; chat error renderer unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_app_errors.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_auto_timeout_javascript_contract_runs_under_node() -> None:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node unavailable; Auto timeout unit test not run")
    result = subprocess.run(
        [node, "--test", "tests/js/test_auto_timeout.js"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_compaction_round_is_labelled_as_one(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    # Without a third branch it would read "discussion cycle N", which is the
    # one thing a compaction is not.
    auto_id = "e" * 32
    compaction = auto_round(1, auto_id)
    compaction.auto = replace(
        compaction.auto,
        phase="compaction",
        cycle=2,
        verdict=None,
    )
    alpha = session("a" * 32, "Alpha", rounds=[compaction])
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])
    auto = reserve_auto_run(store, auto_id=auto_id)
    auto.status = "converged"
    auto.finished_at = "2026-01-01T00:01:00Z"
    auto.terminal_reason = "all agents agreed"
    store.save_auto_run(auto)
    store.clear_auto_reservation(auto.id)

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    provenance = re.search(
        r'<p class="provenance auto-provenance">(.*?)</p>',
        response.text,
        flags=re.DOTALL,
    )
    assert provenance is not None
    assert "Auto compaction" in provenance.group(1)
    assert "discussion cycle" not in provenance.group(1)


def test_the_composer_refuses_a_send_to_an_agent_that_is_already_running(
    tmp_path: Path,
) -> None:
    # The runner answers 409 "session already has a running agent" for a busy
    # agent. Rendering an enabled Send is what turned that into a dead end
    # after an Auto run whose last turn was still finishing.
    alpha = session("a" * 32, "Alpha")
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])

    with TestClient(app, base_url="http://localhost") as client:
        # Inside the lifespan: startup reconciliation rewrites a session left
        # "running" to "error" (app/main.py:131), because at boot that can only
        # mean a crashed process. Marking it busy earlier would be erased.
        busy = store.load_session(alpha.id)
        busy.status = "running"
        store.save_session(busy)
        running = client.get(f"/projects/{project.id}/chat?agent={alpha.id}")
        available = client.get(f"/projects/{project.id}/chat?agent={beta.id}")

    busy_composer = composer_form(running.text)
    idle_composer = composer_form(available.text)
    busy_textarea = re.search(r'<textarea name="prompt"[^>]*>', busy_composer)
    busy_send = re.search(
        r'<button[^>]*type="submit"[^>]*disabled[^>]*>\s*Send</button>',
        busy_composer,
    )
    idle_textarea = re.search(r'<textarea name="prompt"[^>]*>', idle_composer)
    idle_send = re.search(
        r'<button[^>]*type="submit"[^>]*>\s*Send</button>', idle_composer
    )
    assert busy_textarea is not None
    assert busy_send is not None
    assert idle_textarea is not None
    assert idle_send is not None

    for control in (busy_textarea.group(), busy_send.group()):
        assert "disabled" in control
        assert "data-composer-send" in control
        assert "data-disable-during-auto" not in control
        assert "data-auto-disabled" not in control

    for control in (idle_textarea.group(), idle_send.group()):
        assert "disabled" not in control
        assert "data-composer-send" in control
        assert "data-disable-during-auto" not in control
        assert "data-auto-disabled" not in control

    assert f'data-session-id="{alpha.id}"' in busy_composer


def test_agent_card_status_and_preview_are_addressable_but_not_out_of_band(
    tmp_path: Path,
) -> None:
    # Later refreshes target these two ids. On a full page render they must
    # carry no hx-swap-oob: htmx applies OOB attributes found anywhere in a
    # swapped fragment, so a stray one would relocate the card.
    alpha = session("a" * 32, "Alpha", rounds=[record(1, "2026-01-01T00:00:01Z")])
    beta = session("b" * 32, "Beta")
    app, project, store = setup_project(tmp_path, [alpha, beta])

    with TestClient(app, base_url="http://localhost") as client:
        page = client.get(f"/projects/{project.id}/chat?agent={alpha.id}")

    assert f'id="agent-status-{alpha.id}"' in page.text
    assert f'id="agent-preview-{alpha.id}"' in page.text
    assert 'data-agent-status="idle"' in page.text
    assert "Latest round 1" in page.text
    assert "Answer Alpha" in page.text
    assert "hx-swap-oob" not in page.text
