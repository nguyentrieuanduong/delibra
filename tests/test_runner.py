from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Callable

import pytest

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.models import (
    RoundRecord,
    SessionConfig,
    SharedContextDescriptor,
    SourceDescriptor,
)
from app.runner import RunManager, SessionBusy
from app.storage import (
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


def session(session_id: str = "a" * 32, *, mode: str = "success") -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name="runner test",
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
