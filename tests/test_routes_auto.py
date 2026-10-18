from __future__ import annotations

from dataclasses import replace
import functools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.auto import AutoManager
from app.coding import BUILD_VERIFIED_VERSIONS, CodingEnvState
from app.models import AutoBuildGit, AutoHandover
from app.config import Settings
from app.main import create_app
from app.models import (
    AutoArtifact,
    AutoCompactionAttempt,
    AutoContextPolicy,
    AutoQuotaPause,
    AutoRoundDescriptor,
    AutoRunRecord,
    AutoSummary,
    RateLimitObservation,
    RoundRecord,
    SessionConfig,
    SourceDescriptor,
    TurnUsage,
    auto_trigger_key,
)
from app.questions import ProposedQuestion, QuestionStore
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class AutoRouteAdapter:
    EFFORT_LEVELS = ["low"]

    def __init__(self, output: str, *, sleep: bool = False) -> None:
        self.output = output
        self.sleep = sleep
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        mode = "sleep" if self.sleep else "success"
        if self.output == "QUOTA":
            mode = "quota-stderr"
        if self.output.startswith("WRITE ") and context.coding is not None:
            # Stands in for an agent's edit: it lands in the project during the turn.
            (context.coding.project_root / "built.txt").write_text("built\n")
        return Command(
            [
                sys.executable,
                str(FAKE_CLI),
                "--mode",
                mode,
                "--delay",
                "0.03",
            ],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id="ignored-auto-id")]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "result":
            self._final = self.output.removeprefix("WRITE ")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


class AutoRouteFactory:
    def __init__(
        self,
        outputs: list[str],
        *,
        sleep: bool = False,
        sleep_at: set[int] | None = None,
    ) -> None:
        self.outputs = outputs
        self.sleep = sleep
        self.sleep_at = sleep_at or set()
        self.created = 0

    def __call__(self, _config: SessionConfig) -> AutoRouteAdapter:
        index = self.created
        output = self.outputs[min(index, len(self.outputs) - 1)]
        self.created += 1
        return AutoRouteAdapter(
            output,
            sleep=self.sleep or index in self.sleep_at,
        )


def auto_route_app(
    tmp_path: Path,
    *,
    outputs: list[str] | None = None,
    sleep: bool = False,
    sleep_at: set[int] | None = None,
    project_name: str = "Auto routes",
):
    settings = Settings(home=tmp_path / "home", run_timeout=2)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(settings.home)
    project = registry.register(project_name, project_dir)
    store = ProjectStore(project)
    sessions = [
        SessionConfig(
            id="b" * 32,
            name="Beta",
            agent="fake",
            model="success",
            effort="low",
            role_instructions="Second",
            cli_session_id=None,
            status="idle",
            created_at="2026-07-19T00:00:00Z",
            rounds=[],
        ),
        SessionConfig(
            id="a" * 32,
            name="alpha",
            agent="fake",
            model="success",
            effort="low",
            role_instructions="First",
            cli_session_id=None,
            status="idle",
            created_at="2026-07-19T00:00:00Z",
            rounds=[],
        ),
    ]
    for session in sessions:
        store.create_session(session)
    factory = AutoRouteFactory(
        outputs or ["unused"],
        sleep=sleep,
        sleep_at=sleep_at,
    )
    app = create_app(
        settings_override=settings,
        provider_commands={"claude": "/missing/claude", "codex": "/missing/codex"},
        adapter_factory_override=factory,
    )
    return app, settings, project, store, sessions, factory


def add_completed_user_round(
    store: ProjectStore,
    session: SessionConfig,
    *,
    prompt: str,
    output: str,
) -> None:
    config = store.load_session(session.id)
    round_n = len(config.rounds) + 1
    config.rounds.append(
        RoundRecord(
            n=round_n,
            status="complete",
            error=None,
            warnings=[],
            agent=config.agent,
            model=config.model,
            effort=config.effort,
            started_at=f"2026-07-19T00:00:{round_n:02d}Z",
            finished_at=f"2026-07-19T00:00:{round_n:02d}Z",
            source=SourceDescriptor(type="user"),
        )
    )
    store.save_session(config)
    rounds = store.rounds_dir(session.id)
    (rounds / f"round-{round_n:02d}.prompt.md").write_text(
        prompt,
        encoding="utf-8",
    )
    (rounds / f"round-{round_n:02d}.md").write_text(
        output,
        encoding="utf-8",
    )


def convert_auto_to_legacy(
    store: ProjectStore,
    record: AutoRunRecord,
) -> AutoRunRecord:
    assert record.number is not None
    number = record.number
    record.number = None
    store.save_auto_run(record)
    (store.auto_runs_root / str(number)).rename(
        store.auto_runs_root / record.id
    )
    return record


def finish_reserved_auto(
    store: ProjectStore,
    record: AutoRunRecord,
    *,
    status: str,
    preparation_enabled: bool,
) -> AutoRunRecord:
    record.status = status
    record.preparation_enabled = preparation_enabled
    record.finished_at = "2026-07-19T00:01:00Z"
    record.terminal_reason = f"Auto ended with {status}"
    store.save_auto_run(record)
    store.clear_auto_reservation(record.id)
    return record


def start_auto(
    client: TestClient,
    project_id: str,
    session_ids: list[str],
    *,
    policy: str = "first_agree",
    cycles: int = 1,
    prepare_first: bool = False,
    turn_timeout_seconds: int = 2,
):
    data = {
        "topic": "<unsafe topic>",
        "participant_id": session_ids,
        "agreement_policy": policy,
        "max_cycles": str(cycles),
        "turn_timeout_seconds": str(turn_timeout_seconds),
    }
    if prepare_first:
        data["prepare_first"] = "true"
    return client.post(
        f"/projects/{project_id}/auto-runs",
        data=data,
    )


def wait_for_auto(store: ProjectStore, *, terminal: bool):
    for _ in range(300):
        records = store.list_auto_runs()
        if records:
            record = records[-1]
            active = record.status in {"preparing", "discussing"}
            if terminal and not active:
                return record
            if not terminal and active and record.active_key is not None:
                return record
        time.sleep(0.01)
    raise AssertionError("Auto run did not reach the expected state")


def wait_for_auto_status(store: ProjectStore, status: str):
    for _ in range(300):
        records = store.list_auto_runs()
        if records and records[-1].status == status and records[-1].active_key is not None:
            return records[-1]
        time.sleep(0.01)
    raise AssertionError(f"Auto run did not reach {status}")


def parse_sse(response) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    data: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if current:
                current["data"] = "\n".join(data)
                events.append(current)
            current = {}
            data = []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data.append(value)
        elif field != "":
            current[field] = value
    return events


def opening_tag(contents: str, element_id: str) -> str:
    match = re.search(
        rf'<[^>]+\bid="{re.escape(element_id)}"[^>]*>',
        contents,
    )
    assert match is not None
    return match.group(0)


def named_input(contents: str, name: str, value: str | None = None) -> str:
    for match in re.finditer(r"<input\b[^>]*>", contents):
        tag = match.group(0)
        if f'name="{name}"' not in tag:
            continue
        if value is None or f'value="{value}"' in tag:
            return tag
    raise AssertionError(f"input {name!r} with value {value!r} not found")


@pytest.mark.parametrize("reference", ["0", "01", "+1", "1.0", " 1", "1e0"])
def test_auto_route_rejects_noncanonical_numbers(
    tmp_path: Path,
    reference: str,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=["CONVERGED", "CONVERGED"],
    )
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            quote(project.name, safe=""),
            [session.id for session in sessions],
        )
        assert started.status_code == 202
        wait_for_auto(store, terminal=True)
        response = client.get(
            f"/projects/{quote(project.name, safe='')}"
            f"/auto-runs/{quote(reference, safe='')}",
            follow_redirects=False,
        )
    assert response.status_code == 422


def test_auto_route_treats_a_32_digit_canonical_reference_as_a_number(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, _, project, store, _, _ = auto_route_app(tmp_path)
    record = reserve_auto_run(store)
    store.clear_auto_reservation(record.id)
    number = int("1" * 32)
    source = store.auto_runs_root / "1"
    record.number = number
    (source / "config.json").write_text(
        json.dumps(record.to_dict()),
        encoding="utf-8",
    )
    source.rename(store.auto_runs_root / str(number))

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{number}",
            follow_redirects=False,
        )

    assert response.status_code == 200, response.text
    assert f"Auto {number}" in response.text


def test_canonical_project_name_auto_setup_urls(tmp_path: Path) -> None:
    app, _, _, _, _, _ = auto_route_app(
        tmp_path,
        project_name="Route Matrix",
    )
    project_prefix = "/projects/Route%20Matrix"

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"{project_prefix}/auto/setup")

    assert response.status_code == 200
    assert f'action="{project_prefix}/auto-runs"' in response.text
    assert f'hx-post="{project_prefix}/auto-runs"' in response.text


def test_auto_setup_uses_durable_prefill_stable_agents_and_no_topic_query(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    beta = store.load_session(sessions[0].id)
    beta.rounds.append(
        RoundRecord(
            n=1,
            status="complete",
            error=None,
            warnings=[],
            agent=beta.agent,
            model=beta.model,
            effort=beta.effort,
            started_at="2026-07-19T00:00:00Z",
            finished_at="2026-07-19T00:00:01Z",
            source=SourceDescriptor(type="user"),
        )
    )
    store.save_session(beta)
    (store.rounds_dir(beta.id) / "round-01.prompt.md").write_text(
        "Durable <topic>",
        encoding="utf-8",
    )
    (store.rounds_dir(beta.id) / "round-01.md").write_text(
        "Answer",
        encoding="utf-8",
    )

    with TestClient(app, base_url="http://localhost") as client:
        setup = client.get(
            f"/projects/{quote(project.name, safe='')}/auto/setup",
            params={"topic": "must not leak"},
        )
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert setup.status_code == 200
    assert "Durable &lt;topic&gt;" in setup.text
    assert "must not leak" not in setup.text
    assert 'data-topic-source="durable"' in setup.text
    assert setup.text.index("alpha") < setup.text.index("Beta")
    assert setup.text.count('name="participant_id"') == 2
    assert setup.text.count(" checked") >= 2
    assert 'value="all_agree"' in setup.text
    assert 'value="first_agree"' in setup.text
    prepare_tag = named_input(setup.text, "prepare_first")
    first_tag = named_input(setup.text, "agreement_policy", "first_agree")
    all_tag = named_input(setup.text, "agreement_policy", "all_agree")
    assert 'value="true"' in prepare_tag
    assert "checked" not in prepare_tag
    assert "checked" in first_tag
    assert "checked" not in all_tag
    # One row per agent, plus prepare-first, pause-on-blocking-questions, two
    # agreement radios, and the three context-management modes.
    assert setup.text.count('class="auto-choice"') == len(sessions) + 7
    assert "enabling preparation adds 2 calls" in setup.text
    assert re.search(
        r'<input[^>]*name="max_cycles"[^>]*min="1"[^>]*max="20"[^>]*value="3"',
        setup.text,
    )
    # Seconds end to end: the fixture's 2-second run_timeout must survive as the
    # default, and the bound is the settings cap, not a minute-rounded value.
    assert re.search(
        r'<input[^>]*name="turn_timeout_seconds"[^>]*min="1"'
        r'[^>]*max="14400"[^>]*value="2"',
        setup.text,
    )
    assert f'hx-get="/projects/{quote(project.name, safe='')}/auto/setup"' in chat.text
    assert "?topic=" not in chat.text


def test_project_turn_timeout_prefills_new_auto_runs(tmp_path: Path) -> None:
    app, settings, project, store, _sessions, _factory = auto_route_app(tmp_path)
    store.set_turn_timeout_seconds(7, maximum=settings.max_run_timeout)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        fragment = client.get(f"{prefix}/auto/setup")
        page = client.get(f"{prefix}/chat?auto_setup=true")

    for contents in (fragment.text, page.text):
        timeout_tag = named_input(contents, "turn_timeout_seconds")
        assert 'min="1"' in timeout_tag
        assert 'max="14400"' in timeout_tag
        assert 'value="7"' in timeout_tag


def test_an_explicit_auto_turn_timeout_outranks_the_project_default(
    tmp_path: Path,
) -> None:
    app, settings, project, store, sessions, _factory = auto_route_app(
        tmp_path,
        outputs=["CONVERGED", "CONVERGED"],
    )
    store.set_turn_timeout_seconds(7, maximum=settings.max_run_timeout)

    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            quote(project.name, safe=""),
            [session.id for session in sessions],
            turn_timeout_seconds=3,
        )
        assert started.status_code == 202
        record = wait_for_auto(store, terminal=True)

    assert record.future_turn_timeout_seconds == 3


def test_chat_auto_setup_deep_link_reuses_fragment_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    add_completed_user_round(
        store,
        sessions[0],
        prompt="Continue the durable conversation",
        output="Current answer",
    )
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        scan_calls = 0
        original = ProjectStore._scan_auto_directories

        def counted_scan(self: ProjectStore):
            nonlocal scan_calls
            scan_calls += 1
            return original(self)

        monkeypatch.setattr(
            ProjectStore,
            "_scan_auto_directories",
            counted_scan,
        )
        scan_calls = 0
        fragment = client.get(f"{prefix}/auto/setup")
        assert scan_calls == 1
        scan_calls = 0
        page = client.get(f"{prefix}/chat?auto_setup=true")
        assert scan_calls == 1

    assert fragment.status_code == 200
    assert page.status_code == 200
    assert page.text.count("data-auto-setup-dialog") == 1
    assert 'data-topic-source="durable"' in page.text
    assert "Continue the durable conversation" in fragment.text
    assert "Continue the durable conversation" in page.text
    assert page.text.index("alpha") < page.text.index("Beta")
    assert 'value="first_agree" checked' in page.text
    assert 'name="max_cycles" type="number" min="1" max="20" value="3"' in page.text
    # The chat page renders the setup fragment through its own {% with %} unpack,
    # which must carry the new bounds too.
    for text in (fragment.text, page.text):
        assert re.search(
            r'name="turn_timeout_seconds"\s+type="number"\s+min="1"\s+'
            r'max="14400"\s+value="2"',
            text,
        )


def test_chat_auto_setup_deep_link_uses_projection_migration_gate(
    tmp_path: Path,
) -> None:
    app, _, project, store, _, _ = auto_route_app(tmp_path)
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        malformed = store.auto_runs_root / ("f" * 32)
        malformed.mkdir()
        (malformed / "config.json").write_text("{", encoding="utf-8")
        page = client.get(f"{prefix}/chat?auto_setup=true")

    assert page.status_code == 200
    assert "data-auto-setup-dialog" not in page.text
    assert "Auto migration is blocked." in page.text


def test_completed_round_continue_auto_controls_cover_both_pass_states(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    add_completed_user_round(
        store,
        sessions[0],
        prompt="Conversation prompt",
        output="Conversation answer",
    )
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        chat = client.get(f"{prefix}/chat")
        session_page = client.get(f"{prefix}/sessions/{sessions[0].id}")

    for page, summary in ((chat, "Send to…"), (session_page, "Pass to…")):
        article = re.search(
            rf'<article[^>]+id="round-{sessions[0].id}-1".*?</article>',
            page.text,
            flags=re.DOTALL,
        )
        assert article is not None
        round_html = article.group()
        controls = re.findall(
            r'<(?:button|a)\b[^>]*data-continue-auto[^>]*>',
            round_html,
        )
        assert len(controls) == 2
        collapsed = next(
            tag
            for tag in controls
            if 'data-continue-auto-placement="collapsed"' in tag
        )
        expanded = next(
            tag
            for tag in controls
            if 'data-continue-auto-placement="expanded"' in tag
        )
        summary_index = round_html.index(f">{summary}</summary>")
        collapsed_index = round_html.index(collapsed)
        pass_index = round_html.index(">Pass</button>")
        expanded_index = round_html.index(expanded)
        form_close = round_html.index("</form>", pass_index)
        assert summary_index < collapsed_index
        assert pass_index < expanded_index < form_close
        adjacency = re.search(
            r'</details>\s*<(?:button|a)\b'
            r'(?=[^>]*data-continue-auto-placement="collapsed")[^>]*>',
            round_html,
        )
        assert adjacency is not None
        assert collapsed in adjacency.group(0)
        for control in (collapsed, expanded):
            assert sessions[0].id not in control
            assert "source_round" not in control

    chat_controls = re.findall(
        r'<button\b[^>]*data-continue-auto[^>]*>',
        chat.text,
    )
    assert len(chat_controls) == 2
    for control in chat_controls:
        assert f'hx-get="{prefix}/auto/setup"' in control
        assert 'hx-target="#auto-setup-host"' in control
        assert 'hx-params="none"' in control

    session_controls = re.findall(
        r'<a\b[^>]*data-continue-auto[^>]*>',
        session_page.text,
    )
    assert len(session_controls) == 2
    for control in session_controls:
        assert f'href="{prefix}/chat?auto_setup=true"' in control


def test_continue_auto_requires_two_sessions(tmp_path: Path) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    add_completed_user_round(
        store,
        sessions[0],
        prompt="Only conversation",
        output="Only answer",
    )
    store.delete_session(sessions[1].id)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        chat = client.get(f"{prefix}/chat")
        session_page = client.get(f"{prefix}/sessions/{sessions[0].id}")
        deep_link = client.get(f"{prefix}/chat?auto_setup=true")

    assert "data-continue-auto" not in chat.text
    assert "data-continue-auto" not in session_page.text
    assert "data-auto-setup-dialog" not in deep_link.text


def test_continue_auto_controls_disable_for_active_reservation(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    add_completed_user_round(
        store,
        sessions[0],
        prompt="Existing conversation topic",
        output="Completed before Auto",
    )
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        reserve_auto_run(store, auto_id="f" * 32)
        chat = client.get(f"{prefix}/chat")
        session_page = client.get(f"{prefix}/sessions/{sessions[0].id}")

    for page in (chat, session_page):
        controls = re.findall(
            r'<button\b[^>]*data-continue-auto[^>]*>',
            page.text,
        )
        assert len(controls) == 2
        assert all("disabled" in control for control in controls)
        assert all('data-auto-disabled="true"' in control for control in controls)


def test_fresh_auto_setup_defers_topic_to_composer_and_get_starts_no_work(
    tmp_path: Path,
) -> None:
    app, _, project, store, _, factory = auto_route_app(tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        setup = client.get(f"/projects/{quote(project.name, safe='')}/auto/setup")
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert setup.status_code == 200
    assert 'data-topic-source="composer"' in setup.text
    assert "First agree, after everyone speaks once" in setup.text
    assert (
        "Every selected agent completes one discussion turn before First agree"
        " can stop."
    ) in setup.text
    assert '<textarea name="topic" maxlength="100000" required></textarea>' in setup.text
    assert store.list_auto_runs() == []
    assert factory.created == 0
    assert re.search(r'<button[^>]*data-auto-open(?![^>]*disabled)', chat.text)


def test_auto_start_rejects_invalid_participants_policy_and_cycles_before_creation(
    tmp_path: Path,
) -> None:
    app, settings, project, store, sessions, _ = auto_route_app(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = RegistryStore(settings.home).register("Other", other_dir)
    foreign = SessionConfig(
        id="c" * 32,
        name="Foreign",
        agent="fake",
        model="success",
        effort="low",
        role_instructions="",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-19T00:00:00Z",
        rounds=[],
    )
    ProjectStore(other).create_session(foreign)
    base = f"/projects/{quote(project.name, safe='')}/auto-runs"
    valid_ids = [session.id for session in sessions]
    invalid_forms = [
        (valid_ids[:1], "all_agree", "3", "2"),
        ([valid_ids[0], valid_ids[0]], "all_agree", "3", "2"),
        ([valid_ids[0], foreign.id], "all_agree", "3", "2"),
        (valid_ids, "majority", "3", "2"),
        (valid_ids, "all_agree", "0", "2"),
        (valid_ids, "all_agree", "21", "2"),
        (valid_ids, "all_agree", "3", "0"),
        (valid_ids, "all_agree", "3", "-1"),
        (valid_ids, "all_agree", "3", str(settings.max_run_timeout + 1)),
    ]

    with TestClient(app, base_url="http://localhost") as client:
        statuses = [
            client.post(
                base,
                data={
                    "topic": "Validate",
                    "participant_id": participant_ids,
                    "agreement_policy": policy,
                    "max_cycles": cycles,
                    "turn_timeout_seconds": timeout,
                },
            ).status_code
            for participant_ids, policy, cycles, timeout in invalid_forms
        ]

    assert all(status == 422 for status in statuses)
    assert store.list_auto_runs() == []


def test_auto_resume_route_continues_the_run_and_offers_the_control(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(client, project.id, [s.id for s in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        status = client.get(f"{base}/auto-runs/{record.number}")
        resumed = client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={"additional_cycles": "1", "turn_timeout_seconds": "120"},
        )
        final = wait_for_auto(store, terminal=True)

    assert started.status_code == 202
    # The control is offered on a terminal run with no Auto active project-wide,
    # asking for cycles to add rather than for a cumulative limit to restate.
    assert "Continue Auto" in status.text
    assert re.search(
        r'name="additional_cycles"[^>]*min="1"[^>]*max="99"[^>]*value="1"',
        status.text,
    )
    assert resumed.status_code == 202
    reloaded = store.load_auto_run(record.id)
    assert reloaded.max_cycles == 2
    assert reloaded.future_turn_timeout_seconds == 120
    assert len(reloaded.resumptions) == 1
    assert final.current_cycle >= 2


def test_auto_resume_route_accepts_the_remaining_lifetime_allowance(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, factory = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [s.id for s in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        factory.sleep = True
        accepted = client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={"additional_cycles": "99", "turn_timeout_seconds": "120"},
        )
        reloaded = store.load_auto_run(record.id)
        stopped = client.post(f"{base}/auto-runs/{record.number}/stop")
        terminal = wait_for_auto(store, terminal=True)

    assert accepted.status_code == 202
    assert stopped.status_code == 200
    assert terminal.status == "stopped"
    assert reloaded.max_cycles == 100
    assert reloaded.resumptions[-1].max_cycles == 100


def test_auto_resume_route_counts_a_preparation_failure_from_cycle_one(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, _, project, store, _, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"
    record = finish_reserved_auto(
        store,
        reserve_auto_run(store),
        status="stopped",
        preparation_enabled=True,
    )

    with TestClient(app, base_url="http://localhost") as client:
        status = client.get(f"{base}/auto-runs/{record.number}")
        resumed = client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={"additional_cycles": "1", "turn_timeout_seconds": "120"},
        )
        final = wait_for_auto(store, terminal=True)

    # A run that never reached discussion resumes counting at cycle 1, so the
    # whole lifetime allowance is still on offer.
    assert re.search(
        r'name="additional_cycles"[^>]*min="1"[^>]*max="100"[^>]*value="1"',
        status.text,
    )
    assert resumed.status_code == 202
    assert final.max_cycles == 1
    assert final.current_cycle == 1


def test_quota_paused_continue_discloses_the_window_and_spending_choice(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"
    now = datetime.now(timezone.utc)
    reset = now + timedelta(hours=1)

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [session.id for session in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        record.status = "stopped"
        record.terminal_reason = "paused for quota"
        record.quota_pause = AutoQuotaPause(
            observation=RateLimitObservation(
                provider="codex",
                account_key="default",
                window="five_hour",
                used_percent=94.0,
                status="unknown",
                resets_at=reset,
                observed_at=now,
                source="codex_rollout_token_count",
            ),
            paused_at=now,
        )
        store.save_auto_run(record)
        status = client.get(f"{base}/auto-runs/{record.number}")
        history = client.get(f"{base}/auto-runs/{record.number}/history")
        chat = client.get(f"{base}/chat")

    for response in (status, history, chat):
        assert response.status_code == 200
        assert "Codex 5-hour quota" in response.text
        assert "6% remaining" in response.text
        assert f'datetime="{reset.isoformat()}"' in response.text
        assert "keep spending this quota until it resets" in response.text
        assert ">Continue and spend quota<" in response.text
    # A pause is not a crash: the run carries the same amber tier as the round
    # that stopped it, never the failure class.
    for response in (status, chat):
        assert "auto-status stopped quota-paused" in response.text
    assert "auto-history-detail quota-paused" in history.text


def test_a_run_that_failed_without_quota_keeps_the_plain_status_class(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [session.id for session in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        record.status = "error"
        record.terminal_reason = "discussion provider failed"
        store.save_auto_run(record)
        status = client.get(f"{base}/auto-runs/{record.number}")

    assert 'class="auto-status error"' in status.text
    assert "quota-paused" not in status.text


@pytest.mark.parametrize("additional_cycles", ["0", "100"])
def test_auto_resume_route_rejects_an_invalid_additional_cycle_count(
    tmp_path: Path,
    additional_cycles: str,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [s.id for s in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        rejected = client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={
                "additional_cycles": additional_cycles,
                "turn_timeout_seconds": "120",
            },
        )

    assert rejected.status_code == 422
    reloaded = store.load_auto_run(record.id)
    assert reloaded.max_cycles == 1
    assert reloaded.resumptions == []
    assert reloaded.finished_at is not None


def test_auto_status_hides_continue_while_a_run_is_active(tmp_path: Path) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path, sleep=True)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [s.id for s in sessions])
        record = wait_for_auto(store, terminal=False)
        status = client.get(f"{base}/auto-runs/{record.number}")
        client.post(f"{base}/auto-runs/{record.number}/stop")
        wait_for_auto(store, terminal=True)

    assert "Stop Auto" in status.text
    assert "Continue Auto" not in status.text


def test_auto_start_round_trips_turn_timeout_seconds_without_minute_rounding(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            turn_timeout_seconds=90,
        )

    assert started.status_code == 202
    records = store.list_auto_runs()
    assert len(records) == 1
    assert records[0].future_turn_timeout_seconds == 90


def test_auto_start_rejects_busy_project_before_creating_auto_directory(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path, sleep=True)
    first = sessions[0]
    with TestClient(app, base_url="http://localhost") as client:
        running = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{first.id}/run",
            data={"prompt": "Busy"},
        )
        blocked = start_auto(client, project.id, [session.id for session in sessions])
        cancelled = client.post(
            f"/projects/{quote(project.name, safe='')}/sessions/{first.id}/cancel"
        )

    assert running.status_code == 202
    assert blocked.status_code == 409
    assert cancelled.status_code == 200
    assert store.list_auto_runs() == []


def test_auto_start_skips_preparation_when_checkbox_is_missing(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, factory = auto_route_app(
        tmp_path,
        outputs=[
            "Direct answer\nCONVERGED",
            "Second participant objects",
        ],
    )
    project_prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
        )
        terminal = wait_for_auto(store, terminal=True)
        detail = client.get(
            f"{project_prefix}/auto-runs/{terminal.number}/history"
        )
        status = client.get(
            f"{project_prefix}/auto-runs/{terminal.number}"
        )

    assert started.status_code == 202
    assert terminal.status == "converged"
    assert terminal.preparation_enabled is False
    assert terminal.preparations == []
    assert factory.created == 2
    assert "preparation skipped" in started.text
    for response in (detail, status):
        assert response.status_code == 200
        assert "Preparation was skipped." in response.text


def test_auto_choice_css_aligns_controls_without_changing_global_labels() -> None:
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert "label { display: grid; gap: .25rem; }" in css
    assert (
        ".auto-setup-dialog .auto-choice { align-items: center; "
        "display: flex; gap: .35rem; }"
    ) in css


def test_active_auto_status_reload_disables_mutations_and_stop_reenables_auto(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, factory = auto_route_app(tmp_path, sleep=True)
    session_ids = [session.id for session in sessions]
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            session_ids,
            prepare_first=True,
        )
        active = wait_for_auto(store, terminal=False)
        chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")
        status = client.get(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{active.number}"
        )
        session_page = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{active.active_key.session_id}"
        )
        settings_page = client.get(f"/projects/{quote(project.name, safe='')}/settings")
        index = client.get("/")
        active_deep_link = client.get(
            f"/projects/{quote(project.name, safe='')}/chat?auto_setup=true"
        )

        stopped = client.post(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{active.id}/stop"
        )
        terminal_chat = client.get(f"/projects/{quote(project.name, safe='')}/chat")

    assert started.status_code == 202
    assert f'data-auto-id="{active.id}"' in started.text
    assert status.status_code == 200
    assert "Stop Auto" in status.text
    assert "&lt;unsafe topic&gt;" in status.text
    assert f'data-auto-id="{active.id}"' in chat.text
    assert re.search(
        r'<button[^>]*type="submit"[^>]*disabled[^>]*>\s*Send</button>',
        chat.text,
    )
    assert re.search(r'<button[^>]*data-auto-open[^>]*disabled', chat.text)
    assert f'data-auto-id="{active.id}"' in active_deep_link.text
    assert "data-auto-setup-dialog" not in active_deep_link.text
    assert f"Auto {active.number}" in session_page.text
    assert re.search(r'>Cancel</button>', session_page.text)
    assert re.search(r'<button[^>]*disabled[^>]*>Cancel</button>', session_page.text)
    assert re.search(r'>Create session</button>', settings_page.text)
    assert re.search(
        r'<button[^>]*disabled[^>]*>Create session</button>',
        settings_page.text,
    )
    assert re.search(r'<button[^>]*disabled[^>]*>Unregister</button>', index.text)
    assert stopped.status_code == 200
    assert f"Auto {active.number}" in stopped.text
    assert "stopped" in stopped.text
    assert "Stop Auto" not in stopped.text
    assert re.search(r'<button[^>]*data-auto-open(?![^>]*disabled)', terminal_chat.text)
    assert factory.created == 1


def test_stop_disclosure_offers_both_modes_and_the_pending_notice_is_withdrawable(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, factory = auto_route_app(tmp_path, sleep=True)
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [session.id for session in sessions])
        active = wait_for_auto(store, terminal=False)

        idle = client.get(f"{prefix}/auto-runs/{active.number}")
        pending = client.post(
            f"{prefix}/auto-runs/{active.number}/stop-after-turn"
        )
        pending_record = store.load_auto_run(active.id)
        withdrawn = client.post(
            f"{prefix}/auto-runs/{active.number}/stop-after-turn/cancel"
        )
        withdrawn_record = store.load_auto_run(active.id)
        client.post(f"{prefix}/auto-runs/{active.number}/stop")

    assert idle.status_code == 200
    assert "Stop Auto" in idle.text
    assert ">Stop now</button>" in idle.text
    assert ">Stop after this turn</button>" in idle.text
    assert 'data-auto-stop-after-turn="false"' in idle.text

    assert pending.status_code == 200
    assert pending_record.stop_after_turn is True
    assert pending_record.status == "discussing"
    assert 'data-auto-stop-after-turn="true"' in pending.text
    assert "Stopping when the current turn finishes." in pending.text
    # Escalation and withdrawal are the only two moves left while pending.
    assert ">Stop now</button>" in pending.text
    assert ">Keep going</button>" in pending.text
    assert ">Stop after this turn</button>" not in pending.text

    assert withdrawn.status_code == 200
    assert withdrawn_record.stop_after_turn is False
    assert 'data-auto-stop-after-turn="false"' in withdrawn.text
    assert ">Stop after this turn</button>" in withdrawn.text
    assert factory.created == 1


def test_withdrawing_a_stop_after_turn_on_a_finished_run_renders_the_stopped_panel(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=["Alpha", "Beta"],
    )
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [session.id for session in sessions])
        active = wait_for_auto(store, terminal=False)
        number = active.number
        client.post(f"{prefix}/auto-runs/{number}/stop")
        wait_for_auto(store, terminal=True)

        late = client.post(f"{prefix}/auto-runs/{number}/stop-after-turn/cancel")

    assert late.status_code == 200
    assert "Stop Auto" not in late.text
    assert "Continue Auto" in late.text


def test_terminal_legacy_auto_status_redirects_and_escapes_preparations_streams_late(
    tmp_path: Path,
) -> None:
    outputs = [
        '<prep alpha>\n[DELIBRA_AUTO run="old" decision="agree"]',
        "<prep beta>",
        "<discussion>\nCONVERGED",
        "<discussion beta>",
    ]
    app, _, project, store, sessions, factory = auto_route_app(
        tmp_path,
        outputs=outputs,
    )
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            policy="first_agree",
            cycles=20,
            prepare_first=True,
        )
        assert started.status_code == 202
        terminal = wait_for_auto(store, terminal=True)
        terminal.terminal_reason = '<error data-value="unsafe">'
        terminal.discussion[0].warning = '<warning data-value="unsafe">'
        store.save_auto_run(terminal)
        status = client.get(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{terminal.number}"
        )
        legacy_url = (
            f"/projects/{quote(project.name, safe='')}/auto-runs/{terminal.id}"
        )
        legacy_status = client.get(legacy_url, follow_redirects=False)
        legacy_head = client.head(legacy_url, follow_redirects=False)
        fallback_setup = client.get(f"/projects/{quote(project.name, safe='')}/auto/setup")
        timeline = client.get(f"/projects/{quote(project.name, safe='')}/chat/timeline")
        preparation_session = client.get(
            f"/projects/{quote(project.name, safe='')}/sessions/{terminal.preparations[0].session_id}"
        )
        stream_url = f"/projects/{quote(project.name, safe='')}/auto-runs/{terminal.id}/stream"
        with client.stream("GET", stream_url) as response:
            late = parse_sse(response)
        with client.stream(
            "GET",
            stream_url,
            headers={"Last-Event-ID": "999999"},
        ) as response:
            reset = parse_sse(response)

    assert terminal.status == "converged"
    assert status.status_code == 200
    assert legacy_status.status_code == 302
    assert legacy_status.headers["location"] == (
        f"/projects/{quote(project.name, safe='')}/auto-runs/{terminal.number}"
    )
    assert legacy_head.status_code == 302
    assert legacy_head.headers["location"] == legacy_status.headers["location"]
    assert "&lt;prep alpha&gt;" in status.text
    assert "&lt;prep beta&gt;" in status.text
    assert "<prep alpha>" not in status.text
    assert (
        "[DELIBRA_AUTO run=&#34;old&#34; decision=&#34;agree&#34;]"
        in status.text
    )
    assert "&lt;error data-value=&#34;unsafe&#34;&gt;" in status.text
    assert "&lt;warning data-value=&#34;unsafe&#34;&gt;" in status.text
    assert "Preparations" in status.text
    assert 'data-topic-source="durable"' in fallback_setup.text
    assert "&lt;unsafe topic&gt;" in fallback_setup.text
    assert "&lt;discussion&gt;" in timeline.text
    assert timeline.text.index("&lt;prep alpha&gt;") < timeline.text.index(
        "&lt;prep beta&gt;"
    )
    assert timeline.text.index("&lt;prep beta&gt;") < timeline.text.index(
        "&lt;discussion&gt;"
    )
    assert timeline.text.count("Auto preparation") == 2
    assert "Auto preparation" in preparation_session.text
    assert (
        f"/projects/{quote(project.name, safe='')}/auto-runs/{terminal.number}"
        in preparation_session.text
    )
    assert "Retry" not in preparation_session.text
    assert [event["event"] for event in late] == ["status"]
    assert [event["event"] for event in reset] == ["reset", "status"]
    assert all(json.loads(event["data"])["auto_id"] == terminal.id for event in reset)
    assert f"auto-status-stream-{terminal.id}" not in status.text
    assert "sse-connect" not in opening_tag(status.text, "auto-status")
    assert "First agree, after everyone speaks once" in status.text
    assert factory.created == 4


def test_live_preparation_uses_timeline_message_and_timeout_scopes(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path, sleep=True)
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            prepare_first=True,
        )
        assert started.status_code == 202
        active = wait_for_auto(store, terminal=False)
        key = active.active_key
        assert key is not None
        timeout_path = (
            f"/projects/{quote(project.name, safe='')}/sessions/{key.session_id}"
            f"/rounds/{key.round_n}/timeout"
        )
        extension_path = f"{timeout_path}/extend"

        status = client.get(f"/projects/{quote(project.name, safe='')}/auto-runs/{active.id}")
        timeline = client.get(f"/projects/{quote(project.name, safe='')}/chat/timeline")
        timeout = client.get(timeout_path)
        auto_events_before = len(
            app.state.auto_manager._events[(project.id, active.id)].replay
        )
        extended = client.post(
            extension_path,
            data={
                "minutes": "1",
                "scope": "current_and_future_auto",
                "expected_timeout_version": "0",
            },
        )
        refreshed_status = client.get(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{active.id}"
        )
        auto_events_after_extension = len(
            app.state.auto_manager._events[(project.id, active.id)].replay
        )
        stale = client.post(
            extension_path,
            data={
                "minutes": "1",
                "scope": "current_and_future_auto",
                "expected_timeout_version": "0",
            },
        )
        client.post(f"/projects/{quote(project.name, safe='')}/auto-runs/{active.id}/stop")

    assert f'hx-get="{timeout_path}"' in status.text
    assert 'class="auto-preparation-timeout"' in status.text
    assert 'name="scope" value="current_and_future_auto"' in timeout.text
    assert "Extend current + future Auto turns" in timeout.text
    assert extended.status_code == 200
    assert 'name="expected_timeout_version" value="1"' in extended.text
    assert "future Auto turns 62s" in extended.text
    assert "future turn budget 62s" in refreshed_status.text
    assert auto_events_after_extension == auto_events_before + 1
    assert stale.status_code == 409
    assert stale.headers["HX-Trigger"] == "timeout-refresh"
    status_tag = opening_tag(status.text, "auto-status")
    stream_tag = opening_tag(status.text, f"auto-status-stream-{active.id}")
    timeout_tag = opening_tag(
        status.text,
        f"auto-preparation-timeout-{key.session_id}-{key.round_n}",
    )
    assert "sse-connect" not in status_tag
    assert 'hx-preserve="true"' in stream_tag
    assert 'hx-ext="sse"' in stream_tag
    assert (
        f'sse-connect="/projects/{quote(project.name, safe='')}/auto-runs/{active.number}/stream"'
        in stream_tag
    )
    assert 'hx-preserve="true"' in timeout_tag
    live_tag = opening_tag(
        timeline.text,
        f"round-{key.session_id}-{key.round_n}",
    )
    assert 'class="live-round"' in live_tag
    assert (
        f'sse-connect="/projects/{quote(project.name, safe='')}/sessions/{key.session_id}'
        f'/rounds/{key.round_n}/stream"'
    ) in live_tag
    assert f"Auto {active.number}" in timeline.text
    assert f'hx-get="{timeout_path}"' in timeline.text
    assert status.text.count(
        f'sse-connect="/projects/{quote(project.name, safe='')}/auto-runs/{active.number}/stream"'
    ) == 1
    # Status, timeline, and the Questions for you append.
    assert status.text.count('hx-trigger="sse:status, sse:reset"') == 3


def test_discussion_places_current_timeout_in_timeline_not_auto_status(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=["Preparation A", "Preparation B", "Discussion"],
        sleep_at={2},
    )
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            prepare_first=True,
        )
        assert started.status_code == 202
        discussing = wait_for_auto_status(store, "discussing")
        key = discussing.active_key
        assert key is not None
        timeout_path = (
            f"/projects/{quote(project.name, safe='')}/sessions/{key.session_id}"
            f"/rounds/{key.round_n}/timeout"
        )
        status = client.get(f"/projects/{quote(project.name, safe='')}/auto-runs/{discussing.id}")
        timeline = client.get(f"/projects/{quote(project.name, safe='')}/chat/timeline")
        client.post(f"/projects/{quote(project.name, safe='')}/auto-runs/{discussing.id}/stop")

    assert "future turn budget 2s" in status.text
    assert "auto-preparation-timeout" not in status.text
    assert f'hx-get="{timeout_path}"' in timeline.text
    live_tag = opening_tag(timeline.text, f"round-{key.session_id}-{key.round_n}")
    assert 'hx-preserve="true"' in live_tag


def test_chat_auto_history_is_newest_first_lazy_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserve_auto_run,
) -> None:
    app, _, project, store, _, _ = auto_route_app(tmp_path)
    project_prefix = f"/projects/{quote(project.name, safe='')}"
    artifact_reads: list[str] = []
    original = ProjectStore.load_auto_artifact

    def counted_load_auto_artifact(
        self: ProjectStore,
        auto_id: str,
        artifact: AutoArtifact,
        maximum_bytes: int,
    ) -> bytes:
        artifact_reads.append(auto_id)
        return original(self, auto_id, artifact, maximum_bytes)

    monkeypatch.setattr(
        ProjectStore,
        "load_auto_artifact",
        counted_load_auto_artifact,
    )
    with TestClient(app, base_url="http://localhost") as client:
        first = finish_reserved_auto(
            store,
            reserve_auto_run(store, auto_id="c" * 32),
            status="converged",
            preparation_enabled=False,
        )
        second = reserve_auto_run(store, auto_id="d" * 32)
        chat = client.get(f"{project_prefix}/chat")
        assert artifact_reads == [second.id]
        detail = client.get(
            f"{project_prefix}/auto-runs/{first.number}/history"
        )
        active_detail = client.get(
            f"{project_prefix}/auto-runs/{second.number}/history"
        )
        legacy = client.get(
            f"{project_prefix}/auto-runs/{first.id}/history",
            follow_redirects=False,
        )
        removed_index = client.get(f"{project_prefix}/auto/history")

    index = re.search(
        r'<nav[^>]+id="auto-history-index".*?</nav>',
        chat.text,
        flags=re.DOTALL,
    )
    assert index is not None
    index_html = index.group()
    assert index_html.index(f"Auto {second.number}") < index_html.index(
        f"Auto {first.number}"
    )
    assert f'data-auto-history-number="{second.number}"' in index_html
    assert f'data-auto-history-number="{first.number}"' in index_html
    assert detail.status_code == 200
    assert "Original topic" in detail.text
    assert "Preparation was skipped." in detail.text
    assert 'id="auto-status"' not in detail.text
    assert 'data-auto-active=' not in detail.text
    assert "Discussion verdicts" not in detail.text
    assert active_detail.status_code == 200
    assert "No preparation has completed." in active_detail.text
    assert 'id="auto-status"' not in active_detail.text
    assert 'data-auto-active=' not in active_detail.text
    assert artifact_reads == [second.id, first.id, second.id]
    assert legacy.status_code == 302
    assert legacy.headers["location"] == (
        f"{project_prefix}/auto-runs/{first.number}/history"
    )
    assert removed_index.status_code == 404


def test_auto_history_reads_unnumbered_legacy_without_migration_gate(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    app, _, project, store, _, _ = auto_route_app(tmp_path)
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        legacy = finish_reserved_auto(
            store,
            reserve_auto_run(store, auto_id="c" * 32),
            status="converged",
            preparation_enabled=False,
        )
        numbered = finish_reserved_auto(
            store,
            reserve_auto_run(store, auto_id="d" * 32),
            status="stopped",
            preparation_enabled=False,
        )
        legacy.created_at = "2026-01-01T00:00:00Z"
        convert_auto_to_legacy(store, legacy)
        numbered.created_at = "2026-01-02T00:00:00Z"
        store.save_auto_run(numbered)
        chat = client.get(f"{prefix}/chat")
        detail = client.get(
            f"{prefix}/auto-runs/{legacy.id}/history",
            follow_redirects=False,
        )
        live_status = client.get(
            f"{prefix}/auto-runs/{legacy.id}",
            follow_redirects=False,
        )

    index = re.search(
        r'<nav[^>]+id="auto-history-index".*?</nav>',
        chat.text,
        flags=re.DOTALL,
    )
    assert index is not None
    assert index.group().index(f"Auto {numbered.number}") < (
        index.group().index("Legacy Auto")
    )
    legacy_label = re.search(
        rf'<button[^>]+auto-runs/{legacy.id}/history[^>]*>'
        rf'.*?<strong>(.*?)</strong>',
        index.group(),
        flags=re.DOTALL,
    )
    assert legacy_label is not None
    assert "Legacy Auto" in legacy_label.group(1)
    assert legacy.id not in legacy_label.group(1)
    assert detail.status_code == 200
    assert "Legacy Auto" in detail.text
    assert legacy.id not in detail.text
    assert live_status.status_code == 409


def test_auto_history_preparations_are_escaped_focusable_and_refresh_oob(
    tmp_path: Path,
) -> None:
    outputs = [
        "<prep alpha>",
        "<prep beta>",
        "<discussion>\nCONVERGED",
        "<discussion beta>",
    ]
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=outputs,
    )
    project_prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            policy="first_agree",
            cycles=1,
            prepare_first=True,
        )
        terminal = wait_for_auto(store, terminal=True)
        detail = client.get(
            f"{project_prefix}/auto-runs/{terminal.number}/history"
        )
        status = client.get(
            f"{project_prefix}/auto-runs/{terminal.number}"
        )

    assert detail.status_code == 200
    assert "&lt;unsafe topic&gt;" in detail.text
    assert "&lt;prep alpha&gt;" in detail.text
    assert "&lt;prep beta&gt;" in detail.text
    assert "<prep alpha>" not in detail.text
    assert "&lt;discussion&gt;" not in detail.text
    assert "Discussion verdicts" not in detail.text
    assert detail.text.count('class="auto-preparation-summary"') == 2
    for turn in terminal.preparations:
        focus_url = (
            f"{project_prefix}/sessions/{turn.session_id}"
            f"/rounds/{turn.round_n}/focus"
        )
        assert f'hx-get="{focus_url}"' in detail.text
    assert detail.text.count('hx-target="#focus-dialog-content"') == 2
    assert detail.text.count('aria-haspopup="dialog"') == 2
    assert 'id="auto-history-index"' in started.text
    assert 'hx-swap-oob="outerHTML"' in started.text
    assert f"Auto {terminal.number}" in started.text
    assert 'id="auto-history-index"' not in status.text
    assert (
        f'id="auto-history-status-{terminal.number}"'
        in status.text
    )
    assert 'hx-swap-oob="outerHTML"' in status.text
    assert f"{terminal.status} · {terminal.created_at}" in status.text


def test_auto_status_updates_only_history_row_without_directory_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=["Prep A", "Prep B", "Talk A\nCONVERGED", "Talk B"],
    )
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            prepare_first=True,
        )
        terminal = wait_for_auto(store, terminal=True)
        monkeypatch.setattr(
            ProjectStore,
            "_scan_auto_directories",
            lambda self: pytest.fail(
                "status polling must not scan Auto directories"
            ),
        )
        response = client.get(
            f"{prefix}/auto-runs/{terminal.number}"
        )

    assert response.status_code == 200
    assert 'id="auto-history-index"' not in response.text
    assert (
        f'id="auto-history-status-{terminal.number}"'
        in response.text
    )
    assert 'hx-swap-oob="outerHTML"' in response.text


def test_auto_history_keeps_persisted_preparation_after_session_delete(
    tmp_path: Path,
) -> None:
    outputs = [
        "Preparation A",
        "Preparation B",
        "Discussion A\nCONVERGED",
        "Discussion B",
    ]
    app, settings, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=outputs,
    )
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            policy="first_agree",
            cycles=1,
            prepare_first=True,
        )
        terminal = wait_for_auto(store, terminal=True)
        deleted = terminal.preparations[0]
        surviving = terminal.preparations[1]
        persisted = store.load_auto_preparation(
            terminal.id,
            deleted.session_id,
            deleted.output_sha256,
            settings.captured_output_limit,
        ).decode("utf-8")
        deleted_root = store.rounds_dir(deleted.session_id)
        store.delete_session(deleted.session_id)
        auto_root = store.auto_run_dir(terminal.id)
        (auto_root / terminal.topic.path).unlink()
        detail = client.get(
            f"{prefix}/auto-runs/{terminal.number}/history"
        )
        status = client.get(f"{prefix}/auto-runs/{terminal.number}")

    deleted_focus = (
        f"{prefix}/sessions/{deleted.session_id}"
        f"/rounds/{deleted.round_n}/focus"
    )
    surviving_focus = (
        f"{prefix}/sessions/{surviving.session_id}"
        f"/rounds/{surviving.round_n}/focus"
    )
    for response in (detail, status):
        assert response.status_code == 200
        assert persisted in response.text
        assert "Topic unavailable." in response.text
        assert deleted_focus not in response.text
        assert surviving_focus in response.text
        assert str(deleted_root) not in response.text
        assert str(auto_root) not in response.text


def test_auto_history_rejects_tampered_copy_and_ignores_live_round_drift(
    tmp_path: Path,
) -> None:
    outputs = [
        "Persisted Alpha",
        "Persisted Beta",
        "Discussion A\nCONVERGED",
        "Discussion B",
    ]
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=outputs,
    )
    prefix = f"/projects/{quote(project.name, safe='')}"
    with TestClient(app, base_url="http://localhost") as client:
        start_auto(
            client,
            project.id,
            [session.id for session in sessions],
            policy="first_agree",
            cycles=1,
            prepare_first=True,
        )
        terminal = wait_for_auto(store, terminal=True)
        drifted, tampered = terminal.preparations
        (store.rounds_dir(drifted.session_id) /
         f"round-{drifted.round_n:02d}.md").write_text(
            "Changed live round",
            encoding="utf-8",
        )
        (store.auto_run_dir(terminal.id) / "preparations" /
         f"{tampered.session_id}.md").write_text(
            "Tampered Auto copy",
            encoding="utf-8",
        )
        detail = client.get(
            f"{prefix}/auto-runs/{terminal.number}/history"
        )

    drifted_focus = (
        f"{prefix}/sessions/{drifted.session_id}"
        f"/rounds/{drifted.round_n}/focus"
    )
    tampered_focus = (
        f"{prefix}/sessions/{tampered.session_id}"
        f"/rounds/{tampered.round_n}/focus"
    )
    assert detail.status_code == 200
    assert "Persisted Alpha" in detail.text
    assert "Changed live round" not in detail.text
    assert detail.text.count("Preparation output unavailable.") == 1
    assert "Tampered Auto copy" not in detail.text
    assert drifted_focus not in detail.text
    assert tampered_focus not in detail.text


def test_auto_run_total_counts_every_round_including_failed_attempts(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    """A retried turn bills for each attempt, so a total that omitted the
    failures would understate the run every time Phase 4's retry fires."""

    app, _, project, store, _, _ = auto_route_app(tmp_path)
    record = reserve_auto_run(store)
    billed = {
        record.participants[0].session_id: [
            ("preparation", "complete", TurnUsage(input_tokens=100, total_cost_usd=0.5)),
            ("discussion", "error", TurnUsage(input_tokens=7, total_cost_usd=0.25)),
        ],
        record.participants[1].session_id: [
            ("discussion", "complete", TurnUsage(input_tokens=20, output_tokens=3)),
        ],
    }
    for session_id, rounds in billed.items():
        config = store.load_session(session_id)
        for index, (phase, status, usage) in enumerate(rounds, start=1):
            config.rounds.append(
                RoundRecord(
                    n=index,
                    status=status,
                    error=None if status == "complete" else "transient",
                    warnings=[],
                    agent=config.agent,
                    model=config.model,
                    effort=config.effort,
                    started_at=f"2026-09-07T00:00:0{index}Z",
                    finished_at=f"2026-09-07T00:00:0{index}Z",
                    source=SourceDescriptor(type="auto"),
                    auto=AutoRoundDescriptor(
                        auto_id=record.id,
                        phase=phase,
                        cycle=None if phase == "preparation" else 1,
                        position=0,
                        context_file=f"inputs/round-{index:02d}/auto-context.md",
                        context_sha256="0" * 64,
                    ),
                    usage=usage,
                )
            )
        store.save_session(config)
    # A manual round in the same project must not be counted.
    other = store.load_session(record.participants[1].session_id)
    other.rounds.append(
        RoundRecord(
            n=len(other.rounds) + 1,
            status="complete",
            error=None,
            warnings=[],
            agent=other.agent,
            model=other.model,
            effort=other.effort,
            started_at="2026-09-07T00:00:09Z",
            finished_at="2026-09-07T00:00:09Z",
            source=SourceDescriptor(type="user"),
            usage=TurnUsage(input_tokens=9_000_000),
        )
    )
    store.save_session(other)
    finish_reserved_auto(
        store,
        record,
        status="converged",
        preparation_enabled=True,
    )
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        detail = client.get(f"{prefix}/auto-runs/{record.number}/history")

    assert detail.status_code == 200
    assert "127 in" in detail.text
    assert "3 out" in detail.text
    assert "$0.7500" in detail.text


def test_auto_run_total_renders_on_both_status_paths(
    tmp_path: Path,
    reserve_auto_run,
) -> None:
    """chat.html unpacks the status context field by field, so a key added to
    auto_status_context reaches the fragment and silently misses the page."""

    app, _, project, store, _, _ = auto_route_app(tmp_path)
    record = reserve_auto_run(store)
    config = store.load_session(record.participants[0].session_id)
    # Two rounds, so the total is a number no single round's own usage line
    # can supply -- otherwise the timeline would answer for the panel.
    for index, tokens in enumerate((4242, 1000), start=1):
        config.rounds.append(
            RoundRecord(
                n=index,
                status="complete",
                error=None,
                warnings=[],
                agent=config.agent,
                model=config.model,
                effort=config.effort,
                started_at=f"2026-09-07T00:00:0{index}Z",
                finished_at=f"2026-09-07T00:00:0{index}Z",
                source=SourceDescriptor(type="auto"),
                auto=AutoRoundDescriptor(
                    auto_id=record.id,
                    phase="preparation",
                    cycle=None,
                    position=0,
                    context_file=f"inputs/round-{index:02d}/auto-context.md",
                    context_sha256="0" * 64,
                ),
                usage=TurnUsage(input_tokens=tokens),
            )
        )
    store.save_session(config)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        fragment = client.get(f"{prefix}/auto-runs/{record.number}")
        page = client.get(f"{prefix}/chat")

    assert "5,242 in" in fragment.text
    assert "5,242 in" in page.text


def test_auto_setup_offers_context_management_with_its_documented_defaults(
    tmp_path: Path,
) -> None:
    # Off is the default mode. Interval, unit, summarizer, and threshold defaults
    # remain available when an operator opts into context management.
    app, _, project, _store, sessions, _ = auto_route_app(tmp_path)
    prefix = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        fragment = client.get(f"{prefix}/auto/setup")
        page = client.get(f"{prefix}/chat?auto_setup=true")

    for text in (fragment.text, page.text):
        compact_tag = named_input(text, "context_mode", "compact")
        off_tag = named_input(text, "context_mode", "off")
        assert "checked" not in compact_tag
        assert "checked" in off_tag
        assert re.search(
            r'<input[^>]*name="context_interval"[^>]*min="1"[^>]*max="20"'
            r'[^>]*value="3"',
            text,
        )
        assert re.search(
            r'<input[^>]*name="context_threshold_percent"[^>]*min="10"'
            r'[^>]*max="95"[^>]*value="70"',
            text,
        )
        assert 'value="next" selected' in text
        # Rendered bytes, not a provider token window: the label must not
        # invite the user to read it as Phase 6's context occupancy.
        assert "Auto prompt byte budget" in text
        for session in sessions:
            assert f'value="{session.id}"' in text


def test_starting_auto_carries_the_chosen_context_policy(tmp_path: Path) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    session_ids = [session.id for session in sessions]

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{project.id}/auto-runs",
            data={
                "topic": "Policy topic",
                "participant_id": session_ids,
                "agreement_policy": "all_agree",
                "max_cycles": "1",
                "turn_timeout_seconds": "2",
                "context_mode": "compact",
                "context_unit": "context",
                "context_interval": "5",
                "context_threshold_percent": "40",
                "context_summarizer": session_ids[1],
            },
        )
        record = wait_for_auto(store, terminal=True)

    assert response.status_code == 202
    assert record.context_policy == AutoContextPolicy(
        mode="compact",
        unit="context",
        interval=5,
        threshold_percent=40,
        summarizer=session_ids[1],
    )


def test_a_summarizer_outside_the_chosen_participants_is_refused(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    session_ids = [session.id for session in sessions]

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            f"/projects/{project.id}/auto-runs",
            data={
                "topic": "Rejected policy topic",
                "participant_id": session_ids,
                "agreement_policy": "all_agree",
                "max_cycles": "1",
                "turn_timeout_seconds": "2",
                "context_mode": "compact",
                "context_unit": "cycles",
                "context_interval": "3",
                "context_threshold_percent": "70",
                "context_summarizer": "d" * 32,
            },
        )

    assert response.status_code == 422
    assert "summarizer" in response.json()["detail"]
    assert store.list_auto_runs() == []


def test_the_status_panel_states_the_policy_its_summary_and_its_warnings(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    prefix = f"/projects/{quote(project.name, safe='')}"
    session_ids = [session.id for session in sessions]

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, session_ids)
        record = wait_for_auto(store, terminal=True)
        summary_body = b"Summary shown in the panel"
        (store.rounds_dir(session_ids[0]) / "round-90.md").write_bytes(summary_body)
        digest = store.copy_auto_summary(
            record.id,
            1,
            store.rounds_dir(session_ids[0]) / "round-90.md",
            store.rounds_dir(session_ids[0]),
        )
        record.context_policy = AutoContextPolicy(
            mode="compact",
            unit="cycles",
            interval=3,
        )
        record.summaries = [
            AutoSummary(
                path="summaries/01.md",
                sha256=digest,
                created_at="2026-09-07T12:00:00Z",
                cycle=1,
                round_n=90,
                session_id=session_ids[0],
                retired_baseline_count=0,
                retired_discussion_count=1,
            )
        ]
        record.retired_discussion_count = 1
        record.compaction_attempts = [
            AutoCompactionAttempt(
                trigger_key=auto_trigger_key("cycles", record.current_cycle, 1),
                unit="cycles",
                cycle=record.current_cycle,
                discussion_len=1,
                attempted_at="2026-09-07T12:00:00Z",
                outcome="summarized",
                round_n=90,
                session_id=session_ids[0],
                summary_index=0,
                warning="1 Auto entries were dropped",
            )
        ]
        record.compaction_disabled_reason = "3 consecutive compaction failures"
        store.save_auto_run(record)
        status = client.get(f"{prefix}/auto-runs/{record.number}")
        history = client.get(f"{prefix}/auto-runs/{record.number}/history")

    for text in (status.text, history.text):
        assert "compact every 3 cycles" in text
        assert "Summary shown in the panel" in text
        assert "1 Auto entries were dropped" in text
        # A disablement is a notice, not a failure: the run kept discussing.
        assert '<p class="notice"' in text
        assert "3 consecutive compaction failures" in text
    assert 'class="error"' not in status.text



def test_a_terminal_auto_run_refreshes_participant_cards_out_of_band(
    tmp_path: Path,
) -> None:
    # The SSE terminal event refetches this fragment. Without the OOB fields
    # every participant card stays on "running" until a full reload.
    app, _, project, store, sessions, _ = auto_route_app(
        tmp_path,
        outputs=["I agree with the plan."],
    )
    with TestClient(app, base_url="http://localhost") as client:
        started = start_auto(
            client,
            project.id,
            [item.id for item in sessions],
        )
        assert started.status_code == 202
        terminal = wait_for_auto(store, terminal=True)
        fragment = client.get(
            f"/projects/{project.id}/auto-runs/{terminal.number}"
        )
        page = client.get(f"/projects/{project.id}/chat")

    assert fragment.status_code == 200
    # Counting hx-swap-oob would also catch _auto_history_status.html, which
    # _status_response refreshes too, so assert per element instead.
    for participant in sessions:
        assert re.search(
            rf'id="agent-status-{participant.id}"\s+'
            rf'data-agent-status="idle"\s+hx-swap-oob="outerHTML"',
            fragment.text,
        )
        assert re.search(
            rf'id="agent-preview-{participant.id}"\s+hx-swap-oob="outerHTML"',
            fragment.text,
        )
    assert 'data-auto-active="false"' in fragment.text
    # The full page render must not carry OOB attributes.
    assert "hx-swap-oob" not in page.text


LOCAL = ("127.0.0.1", 5000)
LAN = ("192.168.1.20", 5000)


async def _verified_fake_version(self, agent: str) -> str | None:
    return "1"


async def _working_env_python(self, project_path) -> str | None:
    return "3.12.1"


def build_ready(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.set_coding_enabled(True)
    store.set_coding_env(CodingEnvState(status="ready", tool="existing"))
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "fake", "1")
    monkeypatch.setattr(AutoManager, "_build_version", _verified_fake_version)
    monkeypatch.setattr(AutoManager, "_env_python", _working_env_python)


def build_form(session_ids: list[str], *, kind: str = "build") -> dict:
    return {
        "topic": "Build it",
        "participant_id": session_ids,
        "agreement_policy": "first_agree",
        "max_cycles": "1",
        "turn_timeout_seconds": "2",
        "kind": kind,
    }


def test_build_start_is_loopback_only(tmp_path: Path, monkeypatch) -> None:
    app, _settings, project, store, sessions, _factory = auto_route_app(tmp_path, outputs=["One", "Two"])
    build_ready(store, monkeypatch)
    ids = [session.id for session in sessions]
    # One client per peer, sequentially: each `with` owns the app's lifespan and
    # event loop. A second client used inside another's block would run its
    # requests on a short-lived loop that strands the Auto task and its locks.
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        refused = lan.post(f"/projects/{project.id}/auto-runs", data=build_form(ids))
        assert refused.status_code == 403
        assert store.list_auto_runs() == []
        discussion = lan.post(f"/projects/{project.id}/auto-runs", data=build_form(ids, kind="discussion"))
        assert discussion.status_code == 202
        wait_for_auto(store, terminal=True)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        started = local.post(f"/projects/{project.id}/auto-runs", data=build_form(ids))
        assert started.status_code == 202
        record = wait_for_auto(store, terminal=True)

    assert record.kind == "build"


def test_build_resume_is_loopback_only(tmp_path: Path, monkeypatch) -> None:
    app, _settings, project, store, sessions, _factory = auto_route_app(tmp_path, outputs=["One", "Two"])
    build_ready(store, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        assert local.post(
            f"/projects/{project.id}/auto-runs", data=build_form([s.id for s in sessions])
        ).status_code == 202
        record = wait_for_auto(store, terminal=True)
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        resumed = lan.post(
            f"/projects/{project.id}/auto-runs/{record.number}/resume",
            data={"additional_cycles": "1", "turn_timeout_seconds": "2"},
        )

    assert record.status == "limit_reached"
    assert resumed.status_code == 403


def test_build_choice_only_for_a_local_client_with_a_ready_project(tmp_path: Path, monkeypatch) -> None:
    app, _settings, project, store, _sessions, _factory = auto_route_app(tmp_path)
    url = f"/projects/{project.id}/auto/setup"
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        before = local.get(url).text
        build_ready(store, monkeypatch)
        ready_local = local.get(url).text
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        ready_lan = lan.get(url).text

    assert 'name="kind" value="build"' not in before
    assert 'name="kind" value="build"' in ready_local
    assert 'name="kind" value="build"' not in ready_lan


def test_chat_page_setup_dialog_offers_build_when_ready(tmp_path: Path, monkeypatch) -> None:
    # The chat page passes setup values through a `{% with %}` block, so a
    # value it does not list never reaches _auto_setup.html.
    app, _settings, project, store, _sessions, _factory = auto_route_app(tmp_path)
    build_ready(store, monkeypatch)
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        page = local.get(f"/projects/{quote(project.name, safe='')}/chat?auto_setup=1").text

    assert 'name="kind" value="build"' in page


def _finished_run(tmp_path: Path):
    app, _settings, project, store, sessions, _factory = auto_route_app(
        tmp_path, outputs=["One", "Two"]
    )
    return app, project, store, sessions


def test_build_status_shows_kind_git_and_handovers(tmp_path: Path) -> None:
    app, project, store, sessions = _finished_run(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert start_auto(client, project.id, [s.id for s in sessions]).status_code == 202
        record = wait_for_auto(store, terminal=True)
        record.kind = "build"
        record.git = AutoBuildGit(
            enabled=False, reason="working tree has uncommitted changes", branch=None, head=None
        )
        record.handovers = [
            AutoHandover(sessions[1].id, sessions[0].id, "quota", 1, None),
            AutoHandover(sessions[0].id, None, "retries exhausted", 1, None),
        ]
        store.save_auto_run(record)
        page = client.get(f"/projects/{quote(project.name, safe='')}/auto-runs/{record.number}")

    assert page.status_code == 200
    assert "Build · " in page.text
    assert "Commits off: working tree has uncommitted changes" in page.text
    assert "Cycle 1 · alpha · handed over to Beta (quota)" in page.text
    assert "Cycle 1 · Beta · no agent available (retries exhausted)" in page.text


def test_build_status_shows_commit_branch(tmp_path: Path) -> None:
    app, project, store, sessions = _finished_run(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert start_auto(client, project.id, [s.id for s in sessions]).status_code == 202
        record = wait_for_auto(store, terminal=True)
        record.kind = "build"
        record.git = AutoBuildGit(enabled=True, reason=None, branch="main", head="abc123")
        store.save_auto_run(record)
        page = client.get(f"/projects/{quote(project.name, safe='')}/auto-runs/{record.number}")

    assert "Commits on main" in page.text


def test_discussion_status_has_no_build_markup(tmp_path: Path) -> None:
    app, project, store, sessions = _finished_run(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert start_auto(client, project.id, [s.id for s in sessions]).status_code == 202
        record = wait_for_auto(store, terminal=True)
        page = client.get(f"/projects/{quote(project.name, safe='')}/auto-runs/{record.number}")

    assert "Build · " not in page.text
    assert "auto-build-git" not in page.text
    assert "handed over" not in page.text


def test_build_acceptance_enable_handover_commit_and_lan_guard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("app.routes.projects.private_resolution_warnings", lambda hosts: [])
    monkeypatch.setitem(BUILD_VERIFIED_VERSIONS, "fake", "1")
    monkeypatch.setattr(AutoManager, "_build_version", _verified_fake_version)
    app, _settings, project, store, sessions, _factory = auto_route_app(
        tmp_path, outputs=["QUOTA", "WRITE Built it.\nConverged"]
    )
    root = store.project_path
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "T"),
                 ("config", "user.email", "t@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    (root / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "initial"], check=True)
    # An existing ./envs is adopted, so no real conda/venv runs in the test.
    python = root / "envs" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho Python 3.12.1\n")
    python.chmod(0o755)
    prefix = f"/projects/{quote(project.name, safe='')}"
    alpha, beta = sessions[1], sessions[0]
    form = build_form([alpha.id, beta.id])
    form["max_cycles"] = "2"

    # One client per peer, sequentially (amendment A4).
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        assert lan.post(f"{prefix}/coding", data={"enabled": "true"}).status_code == 403
        assert lan.post(f"/projects/{project.id}/auto-runs", data=form).status_code == 403
    assert store.coding_policy().enabled is False
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        assert local.post(
            f"{prefix}/coding",
            data={"enabled": "true", "network_allowlist": "pypi.org"},
            follow_redirects=False,
        ).status_code == 303
        for _ in range(100):
            if store.coding_policy().env.status == "ready":
                break
            time.sleep(0.05)
        assert store.coding_policy().env.tool == "existing"
        assert local.post(f"/projects/{project.id}/auto-runs", data=form).status_code == 202
        wait_for_auto(store, terminal=True)

    on_disk = json.loads((root / ".delibra" / "auto-runs" / "1" / "config.json").read_text())
    assert (on_disk["kind"], on_disk["status"]) == ("build", "converged")
    assert [(h["from_session_id"], h["to_session_id"], h["reason"]) for h in on_disk["handovers"]] == [
        (alpha.id, beta.id, "quota")
    ]
    assert on_disk["git"]["enabled"] is True
    subjects = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    assert subjects == ["Auto build 1 · cycle 1 · Beta", "initial"]
    committed = subprocess.run(
        ["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert committed == ["built.txt"]


def test_app_gives_the_auto_manager_the_cli_commands(tmp_path: Path) -> None:
    commands = {"claude": "/opt/x/claude", "codex": "/opt/x/codex"}
    app = create_app(
        settings_override=Settings(home=tmp_path / "home"),
        provider_commands=commands,
    )
    with TestClient(app, base_url="http://localhost"):
        assert app.state.auto_manager.provider_commands == commands


def test_setup_dialog_previews_why_commits_would_be_off(tmp_path: Path, monkeypatch) -> None:
    app, _settings, project, store, _sessions, _factory = auto_route_app(tmp_path)
    build_ready(store, monkeypatch)
    name = quote(project.name, safe="")
    with TestClient(app, base_url="http://localhost", client=LOCAL) as local:
        dialog = local.get(f"/projects/{project.id}/auto/setup").text
        page = local.get(f"/projects/{name}/chat?auto_setup=1").text
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        remote = lan.get(f"/projects/{project.id}/auto/setup").text

    for html in (dialog, page):
        assert "Commits would be off: project is not a git repository" in html
    assert "Commits would be" not in remote


def test_build_stop_works_from_a_lan_client(tmp_path: Path, monkeypatch) -> None:
    app, _settings, project, store, sessions, _factory = auto_route_app(
        tmp_path, outputs=["One", "Two"], sleep=True
    )
    build_ready(store, monkeypatch)
    ids = [session.id for session in sessions]
    with TestClient(app, base_url="http://localhost", client=LAN) as lan:
        # Starting needs loopback (tested above), so start through the
        # manager; the stop request is this test's only LAN action.
        record = lan.portal.call(
            functools.partial(
                app.state.auto_manager.create,
                project.id,
                topic="Build it",
                participant_ids=ids,
                agreement_policy="first_agree",
                max_cycles=1,
                preparation_enabled=False,
                kind="build",
            )
        )
        stopped = lan.post(
            f"/projects/{quote(project.name, safe='')}/auto-runs/{record.number}/stop"
        )
        terminal = wait_for_auto(store, terminal=True)

    assert stopped.status_code == 200
    assert terminal.kind == "build"
    assert terminal.status == "stopped"


def test_start_route_records_the_blocking_pause_choice(tmp_path: Path) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    data = {
        "topic": "Pause choice",
        "participant_id": [s.id for s in sessions],
        "agreement_policy": "first_agree",
        "max_cycles": "1",
        "turn_timeout_seconds": "2",
    }

    with TestClient(app, base_url="http://localhost") as client:
        client.post(f"/projects/{project.id}/auto-runs", data={**data, "pause_on_blocking_questions": "true"})
        first = wait_for_auto(store, terminal=True)
        client.post(f"/projects/{project.id}/auto-runs", data=data)
        for _ in range(300):
            second = store.list_auto_runs()[-1]
            if second.id != first.id and second.status not in {"preparing", "discussing"}:
                break
            time.sleep(0.01)
        setup = client.get(f"/projects/{quote(project.name, safe='')}/auto/setup")

    assert first.pause_on_blocking_questions is True
    assert second.pause_on_blocking_questions is False
    assert 'name="pause_on_blocking_questions" value="true" checked' in setup.text


def test_resume_form_offers_the_pause_choice_and_lists_waiting_questions(tmp_path: Path) -> None:
    app, settings, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [s.id for s in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        QuestionStore(store.root, settings.auto_questions_view_limit).add(
            [ProposedQuestion("Deploy where?", "", True)],
            asked_by=sessions[0].id,
            asked_by_name=sessions[0].name,
            auto_run=record.number,
            cycle=1,
            round_n=1,
        )
        record.discussion[0] = replace(record.discussion[0], asked=["Q1"])
        store.save_auto_run(record)
        status = client.get(f"{base}/auto-runs/{record.number}")
        client.post(f"{base}/questions/Q1/answer", data={"answer": "Staging"})
        answered_status = client.get(f"{base}/auto-runs/{record.number}")
        client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={
                "additional_cycles": "1",
                "turn_timeout_seconds": "2",
                "pause_on_blocking_questions": "true",
            },
        )
        resumed = store.load_auto_run(record.id)

    # start_auto posts no pause field, so the run started with pausing off
    # and the resume form reflects that before the user turns it back on.
    assert 'name="pause_on_blocking_questions" value="true">' in status.text
    assert "Q1: Deploy where?" in status.text
    assert "Q1: Deploy where? (answered)" in answered_status.text
    assert "asked Q1" in status.text
    assert "1 open question for you from this run" in status.text
    assert resumed.pause_on_blocking_questions is True
