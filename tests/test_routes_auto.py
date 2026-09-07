from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.main import create_app
from app.models import (
    AutoArtifact,
    AutoRoundDescriptor,
    AutoRunRecord,
    RoundRecord,
    SessionConfig,
    SourceDescriptor,
    TurnUsage,
)
from app.storage import ProjectStore, RegistryStore


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class AutoRouteAdapter:
    EFFORT_LEVELS = ["low"]

    def __init__(self, output: str, *, sleep: bool = False) -> None:
        self.output = output
        self.sleep = sleep
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        return Command(
            [
                sys.executable,
                str(FAKE_CLI),
                "--mode",
                "sleep" if self.sleep else "success",
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
            self._final = self.output
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
    assert setup.text.count('class="auto-choice"') == len(sessions) + 3
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
            data={"max_cycles": "2", "turn_timeout_seconds": "120"},
        )
        final = wait_for_auto(store, terminal=True)

    assert started.status_code == 202
    # The control is offered on a terminal run with no Auto active project-wide,
    # bounded by the reconstructed cycle rather than 1..20.
    assert "Continue Auto" in status.text
    assert re.search(
        r'name="max_cycles"[^>]*min="2"[^>]*max="100"[^>]*value="2"',
        status.text,
    )
    assert resumed.status_code == 202
    reloaded = store.load_auto_run(record.id)
    assert reloaded.max_cycles == 2
    assert reloaded.future_turn_timeout_seconds == 120
    assert len(reloaded.resumptions) == 1
    assert final.current_cycle >= 2


def test_auto_resume_route_rejects_a_cycle_limit_below_the_parked_cycle(
    tmp_path: Path,
) -> None:
    app, _, project, store, sessions, _ = auto_route_app(tmp_path)
    base = f"/projects/{quote(project.name, safe='')}"

    with TestClient(app, base_url="http://localhost") as client:
        start_auto(client, project.id, [s.id for s in sessions], cycles=1)
        record = wait_for_auto(store, terminal=True)
        rejected = client.post(
            f"{base}/auto-runs/{record.number}/resume",
            data={"max_cycles": "1", "turn_timeout_seconds": "120"},
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
    assert status.text.count('hx-trigger="sse:status, sse:reset"') == 2


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
