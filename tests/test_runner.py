from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Callable

import pytest

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.models import (
    AutoArtifact,
    AutoParticipant,
    AutoRoundDescriptor,
    AutoRunRecord,
    RoundRecord,
    SessionConfig,
    SharedContextDescriptor,
    SourceDescriptor,
)
from app.runner import (
    STATELESS_CONTINUATION_WARNING,
    AutoRunRequest,
    RunManager,
    SessionBusy,
)
from app.storage import (
    ConflictError,
    LockCoordinator,
    ProjectFileDisplayError,
    ProjectStore,
    RegistryStore,
    StorageError,
    atomic_write_text,
)


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class FakeAdapter:
    EFFORT_LEVELS = ["low"]

    def __init__(self, mode: str, contexts: list[RunContext] | None = None) -> None:
        self.mode = mode
        self.contexts = contexts
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        if self.contexts is not None:
            self.contexts.append(context)
        if self.mode == "spawn-failure":
            return Command(["/definitely/missing/delibra-cli"], context.user_prompt)
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", self.mode],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return [AgentEvent("error", "fake malformed JSONL")]
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if kind == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if kind == "progress":
            return [AgentEvent("progress", "Fake progress")]
        if kind == "error":
            return [AgentEvent("error", payload.get("text", "fake provider error"))]
        if kind == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return [AgentEvent("warning", "fake unknown event")]

    def final_text(self) -> str:
        return self._final


class AdapterFactory:
    def __init__(self, contexts: list[RunContext] | None = None) -> None:
        self.contexts = contexts

    def __call__(self, config: SessionConfig) -> FakeAdapter:
        return FakeAdapter(config.model, self.contexts)


class AutoVerdictAdapter(FakeAdapter):
    def __init__(
        self,
        final_line: str,
        contexts: list[RunContext],
    ) -> None:
        super().__init__("success", contexts)
        self.content = "x" * 600
        self.final_line = final_line
        self.response = f"{self.content}\n{self.final_line}"

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        kind = payload.get("kind")
        if kind == "init":
            return [AgentEvent("init", cli_session_id="must-be-ignored")]
        if kind == "delta" and payload.get("text") == "Hel":
            return [AgentEvent("text_delta", self.content)]
        if kind == "delta":
            return [AgentEvent("text_delta", f"\n{self.final_line}")]
        if kind == "result":
            self._final = self.response
            return [AgentEvent("result", self._final)]
        if kind == "progress":
            return [AgentEvent("progress", "Fake progress")]
        return []


def session(session_id: str = "a" * 32, *, mode: str = "success") -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=f"runner test {session_id[:4]}",
        agent="fake",
        model=mode,
        effort="low",
        role_instructions="Test role",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )


def setup_manager(
    tmp_path: Path,
    *,
    mode: str = "success",
    settings_transform: Callable[[Settings], Settings] | None = None,
    contexts: list[RunContext] | None = None,
    final_writer=atomic_write_text,
) -> tuple[RunManager, str, str, ProjectStore]:
    home = tmp_path / "home"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = RegistryStore(home)
    project = registry.register("Runner", project_dir)
    store = ProjectStore(project)
    config = session(mode=mode)
    store.create_session(config)
    app_settings = Settings(home=home, run_timeout=2)
    if settings_transform:
        app_settings = settings_transform(app_settings)
    manager = RunManager(
        registry=registry,
        locks=LockCoordinator(),
        settings=app_settings,
        adapter_factory=AdapterFactory(contexts),
        final_writer=final_writer,
    )
    return manager, project.id, config.id, store


def create_runner_auto_record(
    store: ProjectStore,
    session_id: str,
    *,
    status: str,
    shared: bytes | None = None,
) -> AutoRunRecord:
    second_id = "b" * 32
    store.create_session(session(second_id))
    topic = b"Frozen Auto context"
    record = AutoRunRecord(
        id="c" * 32,
        project_id=store.project.id,
        number=None,
        status=status,
        agreement_policy="all_agree",
        preparation_enabled=True,
        max_cycles=3,
        current_cycle=1 if status == "discussing" else 0,
        next_participant=0,
        participants=[
            AutoParticipant(
                session_id,
                f"runner test {session_id[:4]}",
                "fake",
                "success",
                "low",
            ),
            AutoParticipant(
                second_id,
                f"runner test {second_id[:4]}",
                "fake",
                "success",
                "low",
            ),
        ],
        topic=AutoArtifact("topic.md", sha256(topic).hexdigest()),
        baseline=AutoArtifact("baseline.md", sha256(b"").hexdigest()),
        baseline_entries=[],
        shared_context=(
            AutoArtifact("shared-context.md", sha256(shared).hexdigest())
            if shared is not None
            else None
        ),
        shared_context_source="brief.md" if shared is not None else None,
        preparations=[],
        discussion=[],
        active_key=None,
        future_turn_timeout_seconds=2,
        active_timeout=None,
        stop_requested=False,
        created_at="2026-07-19T00:00:00Z",
        started_at="2026-07-19T00:00:01Z",
        finished_at=None,
        terminal_reason=None,
    )
    record.number = store.reserve_auto_run_number()
    store.create_auto_run(
        record,
        topic=topic,
        baseline=b"",
        shared_context=shared,
    )
    store.publish_auto_reservation(record.id)
    return record


def auto_run_request(
    store: ProjectStore,
    record: AutoRunRecord,
    *,
    phase: str,
) -> AutoRunRequest:
    run_dir = store.auto_run_dir(record.id)
    return AutoRunRequest(
        auto_id=record.id,
        phase=phase,
        cycle=1 if phase == "discussion" else None,
        position=0,
        context_source=run_dir / "topic.md",
        context_root=run_dir,
        context_sha256=record.topic.sha256,
        shared_source=(run_dir / "shared-context.md" if record.shared_context else None),
        shared_root=(run_dir if record.shared_context else None),
        shared_path=record.shared_context_source,
        shared_sha256=(
            record.shared_context.sha256 if record.shared_context is not None else None
        ),
        execution_prompt="Read inputs/round-01/auto-context.md and respond.",
        initial_timeout_seconds=2,
        preserve_native_session=True,
        ignore_returned_session=True,
    )


@pytest.mark.asyncio
async def test_auto_run_stages_only_verified_frozen_inputs_and_preserves_native_id(
    tmp_path: Path,
) -> None:
    contexts: list[RunContext] = []
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        contexts=contexts,
    )
    config = store.load_session(session_id)
    config.cli_session_id = "preserved-native-session"
    store.save_session(config)
    frozen_shared = b"Frozen shared bytes"
    auto_record = create_runner_auto_record(
        store,
        session_id,
        status="preparing",
        shared=frozen_shared,
    )
    current_shared = store.project_path / "brief.md"
    current_shared.write_bytes(b"Changed project bytes")
    store.select_shared_markdown("brief.md", manager.settings.file_view_limit)

    async with manager.locks.registry_project_sessions(project_id, [session_id]):
        key = await manager.start_auto_locked(
            project_id,
            session_id,
            auto_run_request(store, auto_record, phase="preparation"),
        )
    record = await manager.wait(key)

    staged_context = Path("inputs/round-01/auto-context.md")
    staged_shared = Path("inputs/round-01/shared-context.md")
    assert contexts[0].resume_strategy == "stateless"
    assert contexts[0].resume_id is None
    assert contexts[0].staged_history == []
    assert contexts[0].staged_source == staged_context
    assert (store.workspace_dir(session_id) / staged_context).read_bytes() == (
        b"Frozen Auto context"
    )
    assert (store.workspace_dir(session_id) / staged_shared).read_bytes() == frozen_shared
    assert record.source.type == "auto"
    assert record.auto == AutoRoundDescriptor(
        auto_id=auto_record.id,
        phase="preparation",
        cycle=None,
        position=0,
        context_file=staged_context.as_posix(),
        context_sha256=auto_record.topic.sha256,
        verdict=None,
    )
    assert STATELESS_CONTINUATION_WARNING not in record.warnings
    assert "provider did not return a native session id" not in record.warnings
    assert store.load_session(session_id).cli_session_id == "preserved-native-session"


@pytest.mark.asyncio
async def test_auto_discussion_streams_and_persists_complete_agreement_response(
    tmp_path: Path,
) -> None:
    contexts: list[RunContext] = []
    manager, project_id, session_id, store = setup_manager(tmp_path)
    auto_record = create_runner_auto_record(store, session_id, status="discussing")
    manager.adapter_factory = lambda _config: AutoVerdictAdapter(
        "CONVERGED",
        contexts,
    )

    async with manager.locks.registry_project_sessions(project_id, [session_id]):
        key = await manager.start_auto_locked(
            project_id,
            session_id,
            auto_run_request(
                store,
                auto_record,
                phase="discussion",
            ),
        )
    record = await manager.wait(key)
    events = await collect(manager, key, 0)
    output_path = store.rounds_dir(session_id) / "round-01.md"
    expected_response = f"{'x' * 600}\nCONVERGED"

    assert record.status == "complete"
    assert record.auto is not None and record.auto.verdict == "agree"
    assert output_path.read_text() == expected_response
    assert (
        "".join(event.data for event in events if event.kind == "text_delta")
        == expected_response
    )
    assert not any(event.kind in {"reset", "snapshot"} for event in events)


@pytest.mark.asyncio
async def test_auto_discussion_preserves_continue_response_without_warning(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    auto_record = create_runner_auto_record(store, session_id, status="discussing")
    manager.adapter_factory = lambda _config: AutoVerdictAdapter(
        "Continue",
        [],
    )

    async with manager.locks.registry_project_sessions(project_id, [session_id]):
        key = await manager.start_auto_locked(
            project_id,
            session_id,
            auto_run_request(
                store,
                auto_record,
                phase="discussion",
            ),
        )
    record = await manager.wait(key)
    output = (store.rounds_dir(session_id) / "round-01.md").read_text()

    assert record.auto is not None and record.auto.verdict == "continue"
    assert output == f"{'x' * 600}\nContinue"
    assert not any("verdict" in warning.casefold() for warning in record.warnings)


@pytest.mark.asyncio
async def test_auto_run_rejects_context_digest_change_before_round_allocation(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    auto_record = create_runner_auto_record(store, session_id, status="preparing")
    request = replace(
        auto_run_request(store, auto_record, phase="preparation"),
        context_sha256="f" * 64,
    )

    with pytest.raises(ConflictError, match="context digest"):
        async with manager.locks.registry_project_sessions(project_id, [session_id]):
            await manager.start_auto_locked(project_id, session_id, request)

    assert store.load_session(session_id).rounds == []
    assert not (store.workspace_dir(session_id) / "inputs/round-01").exists()


@pytest.mark.asyncio
async def test_auto_owner_can_start_only_the_expected_participant(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    auto_record = create_runner_auto_record(store, session_id, status="preparing")
    unexpected_session_id = auto_record.participants[1].session_id

    with pytest.raises(ConflictError, match="participant configuration changed"):
        async with manager.locks.registry_project_sessions(
            project_id,
            [unexpected_session_id],
        ):
            await manager.start_auto_locked(
                project_id,
                unexpected_session_id,
                auto_run_request(store, auto_record, phase="preparation"),
            )

    assert store.load_session(unexpected_session_id).rounds == []


def test_generic_retry_rejects_auto_rounds() -> None:
    config = session()
    config.rounds.append(
        RoundRecord(
            n=1,
            status="error",
            error="provider failed",
            warnings=[],
            agent="fake",
            model="success",
            effort="low",
            started_at="2026-07-19T00:00:00Z",
            finished_at="2026-07-19T00:00:01Z",
            source=SourceDescriptor(type="auto"),
        )
    )

    with pytest.raises(ConflictError, match="unsupported"):
        RunManager._retry_record(config, 1)


@pytest.mark.asyncio
async def test_active_auto_reservation_blocks_manual_start_and_retry(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    config = store.load_session(session_id)
    rounds = store.rounds_dir(session_id)
    (rounds / "round-01.prompt.md").write_text("Retry prompt")
    (rounds / "round-01.md").write_text("Failed output")
    config.rounds.append(
        RoundRecord(
            n=1,
            status="error",
            error="provider failed",
            warnings=[],
            agent="fake",
            model="success",
            effort="low",
            started_at="2026-07-19T00:00:00Z",
            finished_at="2026-07-19T00:00:01Z",
            source=SourceDescriptor(type="user"),
        )
    )
    store.save_session(config)
    create_runner_auto_record(store, session_id, status="preparing")

    with pytest.raises(ConflictError, match="active Auto"):
        await manager.start(project_id, session_id, "Manual request")
    with pytest.raises(ConflictError, match="active Auto"):
        await manager.retry(project_id, session_id, 1)


@pytest.mark.asyncio
async def test_active_auto_reservation_blocks_public_cancel(tmp_path: Path) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path, mode="sleep")
    key = await manager.start(project_id, session_id, "Keep running")
    auto_record = create_runner_auto_record(store, session_id, status="preparing")

    with pytest.raises(ConflictError, match="active Auto"):
        await manager.cancel(key)

    assert manager.active_key(project_id, session_id) == key
    store.clear_auto_reservation(auto_record.id)
    await manager.cancel(key)


@pytest.mark.asyncio
async def test_run_stages_and_records_selected_shared_context(tmp_path: Path) -> None:
    contexts: list[RunContext] = []
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        contexts=contexts,
    )
    source = store.project_path / "brief.md"
    source.write_text("Standing rule\n", encoding="utf-8")
    store.select_shared_markdown("brief.md", manager.settings.file_view_limit)

    record = await manager.wait(
        await manager.start(project_id, session_id, "Question")
    )

    staged = Path("inputs/round-01/shared-context.md")
    assert contexts[0].staged_shared_context == staged
    assert (store.workspace_dir(session_id) / staged).read_bytes() == b"Standing rule\n"
    assert record.shared_context == SharedContextDescriptor(
        path="brief.md",
        staged_file=staged.as_posix(),
        sha256=sha256(b"Standing rule\n").hexdigest(),
    )


@pytest.mark.asyncio
async def test_missing_selected_context_creates_no_round_or_input(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    source = store.project_path / "brief.md"
    source.write_text("context", encoding="utf-8")
    store.select_shared_markdown("brief.md", manager.settings.file_view_limit)
    source.unlink()

    with pytest.raises(ProjectFileDisplayError):
        await manager.start(project_id, session_id, "Question")

    assert store.load_session(session_id).rounds == []
    assert not (store.workspace_dir(session_id) / "inputs" / "round-01").exists()
    rounds = store.rounds_dir(session_id)
    assert not (rounds / "round-01.prompt.md").exists()
    assert not (rounds / "round-01.partial.md").exists()


@pytest.mark.asyncio
async def test_retry_appends_stateless_round_and_clears_ambiguous_native_state(
    tmp_path: Path,
) -> None:
    contexts: list[RunContext] = []
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        mode="provider-error",
        contexts=contexts,
    )
    shared_file = store.project_path / "brief.md"
    shared_file.write_text("old requirements", encoding="utf-8")
    store.select_shared_markdown("brief.md", manager.settings.file_view_limit)
    failed = await manager.wait(
        await manager.start(project_id, session_id, "Original")
    )
    assert failed.status == "error"
    persisted = store.load_session(session_id)
    assert persisted.cli_session_id == "fake-native-session"
    persisted.model = "success"
    store.save_session(persisted)
    current = store.read_selected_shared_markdown(manager.settings.file_view_limit)
    assert current is not None
    store.save_shared_markdown(
        "brief.md",
        current.sha256,
        "new requirements",
        manager.settings.file_view_limit,
    )

    retried = await manager.wait(
        await manager.retry(project_id, session_id, failed.n)
    )

    assert retried.status == "complete"
    assert retried.retry_of == failed.n
    assert retried.source.type == "user"
    assert contexts[-1].user_prompt == "Original"
    assert contexts[-1].resume_strategy == "stateless"
    assert contexts[-1].resume_id is None
    assert all("round-01" not in path.name for path in contexts[-1].staged_history)
    assert (
        store.rounds_dir(session_id) / "round-01.prompt.md"
    ).read_text() == "Original"
    shared_snapshot = (
        store.workspace_dir(session_id) / "inputs/round-02/shared-context.md"
    )
    assert shared_snapshot.read_text(encoding="utf-8") == "new requirements"
    assert retried.shared_context is not None
    assert retried.shared_context.sha256 == sha256(b"new requirements").hexdigest()


def test_subprocess_environment_does_not_inherit_server_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELIBRA_SERVER_SECRET", "must-not-reach-agent")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-agent")
    manager, _, session_id, store = setup_manager(tmp_path)
    config = store.load_session(session_id)
    environment = manager._subprocess_environment(
        config, store.workspace_dir(session_id)
    )
    assert "DELIBRA_SERVER_SECRET" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert set(environment) <= {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
    }


@pytest.mark.asyncio
async def test_symlinked_private_tmp_becomes_visible_error_without_outside_write(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    private_tmp = store.workspace_dir(session_id) / ".tmp"
    private_tmp.rmdir()
    outside = tmp_path / "outside-tmp"
    outside.mkdir()
    private_tmp.symlink_to(outside, target_is_directory=True)

    record = await manager.wait(
        await manager.start(project_id, session_id, "Question")
    )
    assert record.status == "error"
    assert "symlink" in (record.error or "")
    assert not list(outside.iterdir())


def test_codex_auth_symlink_is_rejected(tmp_path: Path) -> None:
    manager, _, _, _ = setup_manager(tmp_path)
    manager.settings.codex_home.mkdir(mode=0o700)
    outside = tmp_path / "outside-auth.json"
    outside.write_text('{"token":"outside"}', encoding="utf-8")
    (manager.settings.codex_home / "auth.json").symlink_to(outside)

    with pytest.raises(StorageError, match="symlink"):
        manager._prepare_codex_home()


def test_codex_auth_home_never_copies_ambient_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = tmp_path / "ambient"
    (ambient / ".codex").mkdir(parents=True)
    (ambient / ".codex" / "auth.json").write_text(
        "ambient-auth-sentinel",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(ambient))
    manager, _, _, _ = setup_manager(tmp_path)

    with pytest.raises(StorageError, match="isolated login"):
        manager._prepare_codex_home()

    assert not (manager.settings.codex_home / "auth.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["codex-auth-event", "codex-auth-stderr"])
async def test_codex_auth_errors_are_normalized_before_replay_and_persistence(
    tmp_path: Path,
    mode: str,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path, mode=mode)
    config = store.load_session(session_id)
    config.agent = "codex"
    store.save_session(config)
    manager.settings.codex_home.mkdir(mode=0o700)
    (manager.settings.codex_home / "auth.json").write_text("{}", encoding="utf-8")

    key = await manager.start(project_id, session_id, "Retry me")
    record = await manager.wait(key)
    events = await collect(manager, key, 0)
    persisted_output = (
        store.rounds_dir(session_id) / f"round-{record.n:02d}.md"
    ).read_text(encoding="utf-8")
    combined = (record.error or "") + " " + " ".join(
        event.data for event in events
    ) + " " + persisted_output

    assert record.status == "error"
    assert "refresh token was revoked" not in combined
    assert "codex login --device-auth" in combined
    assert f"CODEX_HOME={shlex.quote(str(manager.settings.codex_home))}" in combined
    assert (manager.settings.codex_home / "auth.json").read_text(
        encoding="utf-8"
    ) == "{}"


@pytest.mark.asyncio
async def test_codex_auth_missing_isolated_login_can_retry_after_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    manager, project_id, session_id, store = setup_manager(tmp_path)
    config = store.load_session(session_id)
    config.agent = "codex"
    store.save_session(config)

    failed = await manager.wait(
        await manager.start(project_id, session_id, "Retry me")
    )
    assert failed.status == "error"
    assert "codex login --device-auth" in (failed.error or "")

    manager.settings.codex_home.mkdir(mode=0o700, exist_ok=True)
    (manager.settings.codex_home / "auth.json").write_text(
        "{}",
        encoding="utf-8",
    )
    retried = await manager.wait(
        await manager.retry(project_id, session_id, failed.n)
    )

    assert retried.status == "complete"
    assert retried.retry_of == failed.n


@pytest.mark.asyncio
async def test_full_subscriber_queue_disconnects_only_that_subscriber(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, _ = setup_manager(tmp_path, mode="sleep")
    key = await manager.start(project_id, session_id, "Question")
    active = manager._active[key]
    queue = asyncio.Queue(maxsize=1)
    active.subscribers.add(queue)

    manager._publish(active, "progress", "first")
    manager._publish(active, "progress", "second")

    assert queue not in active.subscribers
    assert await queue.get() is None
    await manager.cancel(key)


async def collect(manager: RunManager, key, last_event_id: int | str | None = None):
    return [event async for event in manager.subscribe(key, last_event_id)]


@pytest.mark.asyncio
async def test_success_persists_final_config_and_removes_partial_after_both(tmp_path: Path) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    key = await manager.start(project_id, session_id, "Question")
    record = await manager.wait(key)

    config = store.load_session(session_id)
    assert record.status == "complete"
    assert config.status == "idle"
    assert config.cli_session_id == "fake-native-session"
    assert config.rounds[0].status == "complete"
    rounds = store.rounds_dir(session_id)
    assert (rounds / "round-01.prompt.md").read_text() == "Question"
    assert (rounds / "round-01.md").read_text() == "Hello"
    assert not (rounds / "round-01.partial.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("malformed", "malformed"),
        ("provider-error", "provider rejected"),
        ("empty-result", "empty final"),
        ("nonzero", "exited with code 7"),
    ],
)
async def test_terminal_precedence_records_visible_errors_and_partial(
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path, mode=mode)
    key = await manager.start(project_id, session_id, "Question")
    record = await manager.wait(key)

    assert record.status == "error"
    assert message in (record.error or "")
    assert store.load_session(session_id).status == "error"
    assert (store.rounds_dir(session_id) / "round-01.md").read_text().startswith("Hel")
    if mode == "nonzero":
        assert "deliberate stderr tail" in (record.error or "")


@pytest.mark.asyncio
async def test_missing_session_id_is_success_with_warning(tmp_path: Path) -> None:
    manager, project_id, session_id, _ = setup_manager(tmp_path, mode="no-session")
    record = await manager.wait(await manager.start(project_id, session_id, "Question"))
    assert record.status == "complete"
    assert any("session id" in warning for warning in record.warnings)


@pytest.mark.asyncio
async def test_final_write_failure_marks_error_and_preserves_partial(tmp_path: Path) -> None:
    def failing_final_writer(path: Path, contents: str) -> None:
        if path.name == "round-01.md":
            raise StorageError("disk unavailable")
        atomic_write_text(path, contents)

    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        final_writer=failing_final_writer,
    )
    key = await manager.start(project_id, session_id, "Question")
    record = await manager.wait(key)
    assert record.status == "error"
    assert "persist final output" in (record.error or "")
    assert (store.rounds_dir(session_id) / "round-01.partial.md").read_text() == "Hello"


@pytest.mark.asyncio
async def test_auto_discussion_final_write_error_has_no_verdict(
    tmp_path: Path,
) -> None:
    def failing_final_writer(path: Path, contents: str) -> None:
        if path.name == "round-01.md":
            raise StorageError("disk unavailable")
        atomic_write_text(path, contents)

    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        final_writer=failing_final_writer,
    )
    auto_record = create_runner_auto_record(
        store,
        session_id,
        status="discussing",
    )
    manager.adapter_factory = lambda _config: AutoVerdictAdapter("CONVERGED", [])

    async with manager.locks.registry_project_sessions(
        project_id,
        [session_id],
    ):
        key = await manager.start_auto_locked(
            project_id,
            session_id,
            auto_run_request(store, auto_record, phase="discussion"),
        )
    record = await manager.wait(key)

    assert record.status == "error"
    assert record.auto is not None
    assert record.auto.verdict is None
    assert "persist final output" in (record.error or "")


@pytest.mark.asyncio
async def test_total_metadata_loss_still_emits_error_and_keeps_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_save = ProjectStore.save_session

    def fail_terminal_save(self: ProjectStore, config: SessionConfig) -> None:
        if config.status != "running":
            raise StorageError("metadata volume unavailable")
        original_save(self, config)

    monkeypatch.setattr(ProjectStore, "save_session", fail_terminal_save)
    manager, project_id, session_id, store = setup_manager(tmp_path)
    key = await manager.start(project_id, session_id, "Question")
    record = await manager.wait(key)
    events = await collect(manager, key, 0)
    assert record.status == "error"
    assert "terminal metadata" in (record.error or "")
    assert any(event.kind == "error" for event in events)
    assert events[-1].kind == "done"
    assert (store.rounds_dir(session_id) / "round-01.partial.md").exists()


@pytest.mark.asyncio
async def test_auto_discussion_metadata_error_has_no_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    auto_record = create_runner_auto_record(
        store,
        session_id,
        status="discussing",
    )
    manager.adapter_factory = lambda _config: AutoVerdictAdapter("CONVERGED", [])
    original_save = ProjectStore.save_session

    def fail_terminal_save(
        self: ProjectStore,
        config: SessionConfig,
    ) -> None:
        if config.status != "running":
            raise StorageError("metadata volume unavailable")
        original_save(self, config)

    monkeypatch.setattr(ProjectStore, "save_session", fail_terminal_save)
    async with manager.locks.registry_project_sessions(
        project_id,
        [session_id],
    ):
        key = await manager.start_auto_locked(
            project_id,
            session_id,
            auto_run_request(store, auto_record, phase="discussion"),
        )
    record = await manager.wait(key)

    assert record.status == "error"
    assert record.auto is not None
    assert record.auto.verdict is None
    assert "terminal metadata" in (record.error or "")


@pytest.mark.asyncio
async def test_line_and_output_caps_kill_child_and_persist_prefix(tmp_path: Path) -> None:
    limited = lambda settings: replace(
        settings,
        stdout_line_limit=200,
        captured_output_limit=40,
    )
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        mode="huge-line",
        settings_transform=limited,
    )
    record = await manager.wait(await manager.start(project_id, session_id, "Question"))
    assert record.status == "error"
    assert "line limit" in (record.error or "")

    second_id = "b" * 32
    store.create_session(session(second_id, mode="flood"))
    record = await manager.wait(await manager.start(project_id, second_id, "Question"))
    assert record.status == "error"
    assert "output limit" in (record.error or "")
    persisted = (store.rounds_dir(second_id) / "round-01.md").read_bytes()
    assert 0 < len(persisted) <= 40


@pytest.mark.asyncio
async def test_timeout_cancel_and_shutdown_finalize_without_orphan_processes(tmp_path: Path) -> None:
    short = lambda settings: replace(settings, run_timeout=1)
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        mode="sleep",
        settings_transform=short,
    )
    timeout_record = await manager.wait(
        await manager.start(project_id, session_id, "Question")
    )
    assert timeout_record.status == "error"
    assert "timed out" in (timeout_record.error or "")

    cancel_id = "b" * 32
    store.create_session(session(cancel_id, mode="spawn-child"))
    cancel_key = await manager.start(project_id, cancel_id, "Question")
    await asyncio.sleep(0.1)
    await manager.cancel(cancel_key)
    assert (await manager.wait(cancel_key)).status == "cancelled"

    shutdown_id = "c" * 32
    store.create_session(session(shutdown_id, mode="sleep"))
    shutdown_key = await manager.start(project_id, shutdown_id, "Question")
    await manager.shutdown()
    assert (await manager.wait(shutdown_key)).status == "cancelled"


@pytest.mark.asyncio
async def test_timeout_extension_adds_to_old_deadline_and_persists_before_wake(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        mode="sleep",
        settings_transform=lambda value: replace(
            value,
            run_timeout=1,
            max_run_timeout=180,
        ),
    )
    key = await manager.start(project_id, session_id, "Question")
    before = manager.timeout_snapshot(key)

    result = await manager.extend_timeout(key, 1, "current", before.version)

    assert result.timeout.effective_seconds == before.effective_seconds + 60
    assert result.timeout.version == 1
    persisted = store.load_session(session_id).rounds[-1].timeout
    assert persisted == result.timeout
    await manager.cancel(key)


@pytest.mark.asyncio
async def test_timeout_extension_rejects_stale_over_cap_and_claimed_runs(
    tmp_path: Path,
) -> None:
    manager, project_id, session_id, _ = setup_manager(
        tmp_path,
        mode="sleep",
        settings_transform=lambda value: replace(
            value,
            run_timeout=1,
            max_run_timeout=61,
        ),
    )
    key = await manager.start(project_id, session_id, "Question")
    initial = manager.timeout_snapshot(key)
    try:
        await manager.extend_timeout(key, 1, "current", initial.version)
        with pytest.raises(ConflictError, match="version"):
            await manager.extend_timeout(key, 1, "current", initial.version)
        with pytest.raises(ConflictError, match="maximum"):
            await manager.extend_timeout(key, 1, "current", 1)
        manager._active[key].timeout_claimed = True
        with pytest.raises(ConflictError, match="no longer"):
            await manager.extend_timeout(key, 1, "current", 1)
    finally:
        await manager.cancel(key)


@pytest.mark.asyncio
async def test_spawn_failure_is_an_error_round_and_session_is_not_stuck(tmp_path: Path) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path, mode="spawn-failure")
    key = await manager.start(project_id, session_id, "Question")
    record = await manager.wait(key)
    assert record.status == "error"
    assert "spawn" in (record.error or "")
    assert store.load_session(session_id).status == "error"


@pytest.mark.asyncio
async def test_busy_session_two_subscribers_replay_and_late_done(tmp_path: Path) -> None:
    manager, project_id, session_id, _ = setup_manager(tmp_path)
    key = await manager.start(project_id, session_id, "Question")
    with pytest.raises(SessionBusy):
        await manager.start(project_id, session_id, "Another")

    first, second = await asyncio.gather(collect(manager, key), collect(manager, key))
    await manager.wait(key)
    assert [event.kind for event in first] == [event.kind for event in second]
    assert first[-1].kind == "done"
    assert all(event.event_id > 0 for event in first)

    pivot = first[1].event_id
    replay = await collect(manager, key, pivot)
    assert all(event.event_id > pivot for event in replay if event.kind != "reset")
    assert replay[-1].kind == "done"
    late = await collect(manager, key)
    assert late[-1].kind == "done"


@pytest.mark.asyncio
async def test_replay_gap_emits_reset_snapshot_then_authoritative_done(tmp_path: Path) -> None:
    tiny_replay = lambda settings: replace(settings, replay_limit=90)
    manager, project_id, session_id, _ = setup_manager(
        tmp_path,
        settings_transform=tiny_replay,
    )
    key = await manager.start(project_id, session_id, "Question")
    await manager.wait(key)
    events = await collect(manager, key, 0)
    assert events[0].kind == "reset"
    assert any(event.kind == "snapshot" for event in events)
    assert events[-1].kind == "done"
    for invalid in ("not-an-id", -1, 999_999):
        invalid_events = await collect(manager, key, invalid)
        assert invalid_events[0].kind == "reset"
        assert invalid_events[-1].kind == "done"


@pytest.mark.asyncio
async def test_cancel_exit_race_finalizes_exactly_once(tmp_path: Path) -> None:
    manager, project_id, session_id, store = setup_manager(tmp_path)
    key = await manager.start(project_id, session_id, "Question")
    await asyncio.gather(manager.cancel(key), manager.wait(key))
    record = await manager.wait(key)
    assert record.status in {"complete", "cancelled"}
    assert len(store.load_session(session_id).rounds) == 1
    done = [event for event in await collect(manager, key, 0) if event.kind == "done"]
    assert len(done) == 1


@pytest.mark.asyncio
async def test_stateless_history_is_newest_bounded_chronological_and_oversize_is_clean(
    tmp_path: Path,
) -> None:
    contexts: list[RunContext] = []
    bounded = lambda settings: replace(
        settings,
        stateless_round_limit=2,
        stateless_history_limit=200,
    )
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        contexts=contexts,
        settings_transform=bounded,
    )
    config = store.load_session(session_id)
    config.cli_session_id = None
    rounds = store.rounds_dir(session_id)
    for number in (1, 2, 3):
        (rounds / f"round-{number:02d}.prompt.md").write_text(
            f"prompt {number}", encoding="utf-8"
        )
        (rounds / f"round-{number:02d}.md").write_text(
            f"answer {number}", encoding="utf-8"
        )
        config.rounds.append(
            RoundRecord(
                n=number,
                status="complete",
                error=None,
                warnings=[],
                agent="fake",
                model="success",
                effort="low",
                started_at="2026-07-17T00:00:00Z",
                finished_at="2026-07-17T00:00:01Z",
                source=SourceDescriptor(type="user"),
            )
        )
    store.save_session(config)

    await manager.wait(await manager.start(project_id, session_id, "Continue"))
    staged = contexts[-1].staged_history
    assert [path.name for path in staged] == [
        "round-02.prompt.md",
        "round-02.md",
        "round-03.prompt.md",
        "round-03.md",
    ]
    manifest = json.loads((store.workspace_dir(session_id) / "inputs/round-04/history/manifest.json").read_text())
    assert manifest["round_limit"] == 2
    assert manifest["byte_limit"] == 200
    assert manifest["omitted_rounds"] == [1]

    oversize_id = "b" * 32
    oversized = session(oversize_id)
    oversized.rounds.append(config.rounds[0])
    store.create_session(oversized)
    oversized_rounds = store.rounds_dir(oversize_id)
    (oversized_rounds / "round-01.prompt.md").write_text("p" * 300)
    (oversized_rounds / "round-01.md").write_text("a" * 300)
    with pytest.raises(StorageError, match="newest history round exceeds"):
        await manager.start(project_id, oversize_id, "Continue")
    assert store.load_session(oversize_id).rounds == oversized.rounds
    assert not (store.workspace_dir(oversize_id) / "inputs/round-02").exists()


def test_stateless_history_excludes_auto_preparation_rounds(tmp_path: Path) -> None:
    manager, _, session_id, store = setup_manager(tmp_path)
    config = store.load_session(session_id)
    rounds = store.rounds_dir(session_id)
    for number, phase in ((1, None), (2, "preparation")):
        (rounds / f"round-{number:02d}.prompt.md").write_text(f"prompt {number}")
        (rounds / f"round-{number:02d}.md").write_text(f"answer {number}")
        config.rounds.append(
            RoundRecord(
                n=number,
                status="complete",
                error=None,
                warnings=[],
                agent="fake",
                model="success",
                effort="low",
                started_at=f"2026-07-17T00:00:0{number}Z",
                finished_at=f"2026-07-17T00:00:0{number}Z",
                source=SourceDescriptor(type="auto" if phase else "user"),
                auto=(
                    AutoRoundDescriptor(
                        auto_id="c" * 32,
                        phase=phase,
                        cycle=None,
                        position=0,
                        context_file=(
                            f"inputs/round-{number:02d}/auto-context.md"
                        ),
                        context_sha256="d" * 64,
                    )
                    if phase
                    else None
                ),
            )
        )
    store.save_session(config)

    input_root, staged = manager._stage_history(store, config, 3)

    assert [path.name for path in staged] == ["round-01.prompt.md", "round-01.md"]
    manager._cleanup_input_root(input_root)


@pytest.mark.asyncio
async def test_migrated_legacy_workspace_continues_statelessly(
    tmp_path: Path,
) -> None:
    contexts: list[RunContext] = []
    manager, project_id, session_id, store = setup_manager(
        tmp_path,
        contexts=contexts,
    )
    named_directory = store.session_dir(session_id)
    os.replace(named_directory, store.sessions_root / session_id)
    store._invalidate_session_directory_cache()

    first_key = await manager.start(project_id, session_id, "First")
    first = await manager.wait(first_key)
    assert first.status == "complete"
    assert store.load_session(session_id).cli_session_id == "fake-native-session"

    migrated = store.migrate_session_directories()
    assert migrated.complete
    assert store.load_session(session_id).cli_session_id is None

    second_key = await manager.start(project_id, session_id, "Continue")
    second = await manager.wait(second_key)

    assert second.status == "complete"
    assert contexts[-1].resume_strategy == "stateless"
    assert contexts[-1].resume_id is None
    assert [path.name for path in contexts[-1].staged_history] == [
        "round-01.prompt.md",
        "round-01.md",
    ]
    assert STATELESS_CONTINUATION_WARNING in second.warnings
    assert store.load_session(session_id).cli_session_id == "fake-native-session"
