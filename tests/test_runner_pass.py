from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

from app.agents.base import AgentEvent, Command, RunContext
from app.config import Settings
from app.models import RoundRecord, SessionConfig, SourceDescriptor
from app.pass_prompts import BUILT_IN_PASS_PROMPT_TEMPLATE
from app.runner import RunManager, SessionBusy
from app.storage import LockCoordinator, ProjectStore, RegistryStore, StorageError


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class Adapter:
    def __init__(self, contexts: list[RunContext]) -> None:
        self.contexts = contexts
        self._final = ""

    def build_command(self, config: SessionConfig, context: RunContext) -> Command:
        self.contexts.append(context)
        return Command(
            [sys.executable, str(FAKE_CLI), "--mode", config.model],
            context.user_prompt,
        )

    def parse_line(self, line: str) -> list[AgentEvent]:
        payload = json.loads(line)
        if payload.get("kind") == "init":
            return [AgentEvent("init", cli_session_id=payload.get("session_id"))]
        if payload.get("kind") == "delta":
            return [AgentEvent("text_delta", payload.get("text", ""))]
        if payload.get("kind") == "result":
            self._final = payload.get("text", "")
            return [AgentEvent("result", self._final)]
        return []

    def final_text(self) -> str:
        return self._final


def session(session_id: str, name: str, model: str = "success") -> SessionConfig:
    return SessionConfig(
        id=session_id,
        name=name,
        agent="fake",
        model=model,
        effort="low",
        role_instructions="Test",
        cli_session_id=None,
        status="idle",
        created_at="2026-07-17T00:00:00Z",
        rounds=[],
    )


def manager_setup(tmp_path: Path, *, target_model: str = "success"):
    registry = RegistryStore(tmp_path / "home")
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = registry.register("Runner pass", project_path)
    store = ProjectStore(project)
    source = session("a" * 32, "Source")
    source.rounds.append(
        RoundRecord(
            n=1,
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
    target = session("b" * 32, "Target", target_model)
    store.create_session(source)
    store.create_session(target)
    rounds = store.rounds_dir(source.id)
    (rounds / "round-01.prompt.md").write_text("Source prompt", encoding="utf-8")
    (rounds / "round-01.md").write_text("Trusted source", encoding="utf-8")
    contexts: list[RunContext] = []
    manager = RunManager(
        registry=registry,
        locks=LockCoordinator(),
        settings=Settings(home=tmp_path / "home", run_timeout=2),
        adapter_factory=lambda config: Adapter(contexts),
        # Never the real CLI: the account quota read spawns `codex
        # app-server`, and a test must not reach the operator's account.
        codex_executable="/missing/codex",
    )
    descriptor = SourceDescriptor(
        type="pass", from_session=source.id, from_round=1
    )
    return manager, project.id, store, source, target, descriptor, contexts


@pytest.mark.asyncio
async def test_pass_renders_and_persists_submitted_template(tmp_path: Path) -> None:
    manager, project_id, store, _, target, descriptor, contexts = manager_setup(tmp_path)
    template = (
        "Session {source_session}; round {source_round}; unknown {keep}; "
        "read {source_path}"
    )
    key = await manager.start(project_id, target.id, template, source=descriptor)
    expected = "Session Source; round 1; unknown {keep}; read inputs/round-01/source.md"
    assert key.round_n == 1
    assert len(contexts) == 1
    assert contexts[0].user_prompt == expected
    assert contexts[0].user_prompt.count("inputs/round-01/source.md") == 1
    assert (store.rounds_dir(target.id) / "round-01.prompt.md").read_text() == expected
    result = await manager.wait(key)
    assert result.status == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    ["missing path", "{source_path}\n{source_path}"],
)
async def test_invalid_pass_template_fails_before_any_content_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    manager, project_id, store, _, target, descriptor, _ = manager_setup(tmp_path)

    def unexpected_shared_read(*args, **kwargs):
        pytest.fail("shared Markdown must not be read before Pass-template preflight")

    monkeypatch.setattr(
        ProjectStore,
        "read_selected_shared_markdown",
        unexpected_shared_read,
    )
    monkeypatch.setattr(
        "app.runner.safe_copy_file",
        lambda *args, **kwargs: pytest.fail(
            "source bytes must not be copied before Pass-template preflight"
        ),
    )
    with pytest.raises(StorageError, match="Pass prompt"):
        await manager.start(project_id, target.id, template, source=descriptor)

    assert store.load_session(target.id).rounds == []
    assert not (store.workspace_dir(target.id) / "inputs" / "round-01").exists()
    assert not (store.rounds_dir(target.id) / "round-01.prompt.md").exists()
    assert not (store.rounds_dir(target.id) / "round-01.partial.md").exists()


@pytest.mark.asyncio
async def test_pass_rejects_path_duplicated_by_template_literal_before_staging(
    tmp_path: Path,
) -> None:
    manager, project_id, store, _, target, descriptor, _ = manager_setup(tmp_path)
    with pytest.raises(StorageError, match="exactly once"):
        await manager.start(
            project_id,
            target.id,
            "inputs/round-01/source.md\n{source_path}",
            source=descriptor,
        )
    assert store.load_session(target.id).rounds == []
    assert not (store.workspace_dir(target.id) / "inputs" / "round-01").exists()


@pytest.mark.asyncio
async def test_concurrent_pass_and_direct_run_have_one_winner_and_no_orphan_input(
    tmp_path: Path,
) -> None:
    manager, project_id, store, _, target, descriptor, contexts = manager_setup(
        tmp_path, target_model="sleep"
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            manager.start(project_id, target.id, "Direct"),
            manager.start(project_id, target.id, BUILT_IN_PASS_PROMPT_TEMPLATE, source=descriptor),
            return_exceptions=True,
        ),
        timeout=3,
    )
    keys = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(keys) == 1
    assert len(failures) == 1 and isinstance(failures[0], SessionBusy)
    assert len(store.load_session(target.id).rounds) == 1
    input_root = store.workspace_dir(target.id) / "inputs" / "round-01"
    if contexts[0].staged_source is None:
        assert not input_root.exists()
    else:
        assert (input_root / "source.md").is_file()
    await manager.cancel(keys[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("sabotage", ["symlink", "delete"])
async def test_source_swap_or_delete_during_staging_fails_without_half_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sabotage: str,
) -> None:
    manager, project_id, store, source, target, descriptor, _ = manager_setup(tmp_path)
    source_path = store.rounds_dir(source.id) / "round-01.md"
    original_copy = __import__("app.runner", fromlist=["safe_copy_file"]).safe_copy_file
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    def sabotaging_copy(source_file, source_root, destination, destination_root):
        if destination.name == "source.md":
            source_file.unlink()
            if sabotage == "symlink":
                source_file.symlink_to(outside)
        return original_copy(source_file, source_root, destination, destination_root)

    monkeypatch.setattr("app.runner.safe_copy_file", sabotaging_copy)
    with pytest.raises(StorageError):
        await manager.start(project_id, target.id, BUILT_IN_PASS_PROMPT_TEMPLATE, source=descriptor)
    persisted = store.load_session(target.id)
    assert persisted.status == "idle"
    assert persisted.rounds == []
    assert not (store.workspace_dir(target.id) / "inputs" / "round-01").exists()
    assert not (store.rounds_dir(target.id) / "round-01.prompt.md").exists()
    assert source_path.exists() is (sabotage == "symlink")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["directory", "symlink"])
async def test_precreated_target_input_is_rejected_and_not_removed(
    tmp_path: Path,
    kind: str,
) -> None:
    manager, project_id, store, _, target, descriptor, _ = manager_setup(tmp_path)
    input_root = store.workspace_dir(target.id) / "inputs" / "round-01"
    if kind == "directory":
        input_root.mkdir()
        (input_root / "attacker.txt").write_text("keep", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        input_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises((StorageError, FileExistsError)):
        await manager.start(project_id, target.id, BUILT_IN_PASS_PROMPT_TEMPLATE, source=descriptor)
    assert input_root.exists()
    assert store.load_session(target.id).rounds == []


@pytest.mark.asyncio
async def test_symlinked_inputs_parent_cannot_escape_staging(tmp_path: Path) -> None:
    manager, project_id, store, _, target, descriptor, _ = manager_setup(tmp_path)
    inputs = store.workspace_dir(target.id) / "inputs"
    inputs.rmdir()
    outside = tmp_path / "outside-inputs"
    outside.mkdir()
    inputs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError):
        await manager.start(project_id, target.id, BUILT_IN_PASS_PROMPT_TEMPLATE, source=descriptor)
    assert not list(outside.iterdir())
    assert store.load_session(target.id).rounds == []
