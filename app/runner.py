"""Async subprocess lifecycle, durable round capture, and replayable run events."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
from typing import Callable, Protocol

from app.agents.base import AgentAdapter, AgentEvent, RunContext
from app.agents.errors import CODEX_AUTH_MARKERS, classify_text, fold_categories
from app.auto import parse_auto_verdict
from app.config import Settings
from app.pass_prompts import PassPromptTemplateError, render_pass_prompt
from app.models import (
    AutoRoundDescriptor,
    AutoRunRecord,
    ContextReading,
    Project,
    RoundRecord,
    RunKey,
    SessionConfig,
    SharedContextDescriptor,
    SourceDescriptor,
    TimeoutExtensionRecord,
    TimeoutRecord,
    TurnUsage,
)
from app.storage import (
    ConflictError,
    LockCoordinator,
    NotFoundError,
    OwnershipError,
    ProjectStore,
    RegistryStore,
    StorageError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    ensure_owned_directory,
    safe_copy_file,
    utc_now,
    validate_id,
)


LOGGER = logging.getLogger(__name__)
STATELESS_CONTINUATION_WARNING = (
    "Native context was reset or unavailable; bounded staged history supplied "
    "a stateless continuation."
)
class SessionBusy(ConflictError):
    """A second run was requested for a session that is already running."""


class AdapterFactory(Protocol):
    def __call__(self, config: SessionConfig) -> AgentAdapter: ...


@dataclass(frozen=True)
class StreamEvent:
    event_id: int
    kind: str
    data: str = ""


@dataclass
class ActiveRun:
    key: RunKey
    project: Project
    store: ProjectStore
    config: SessionConfig
    record: RoundRecord
    adapter: AgentAdapter
    process: asyncio.subprocess.Process | None
    partial_path: Path
    output_path: Path
    completion: asyncio.Future[RoundRecord]
    started_monotonic: float
    deadline_monotonic: float
    hard_deadline_monotonic: float
    deadline_changed: asyncio.Event
    auto_request: AutoRunRequest | None = None
    task: asyncio.Task[None] | None = None
    captured: bytearray = field(default_factory=bytearray)
    stderr_tail: bytearray = field(default_factory=bytearray)
    errors: list[str] = field(default_factory=list)
    error_categories: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cli_session_id: str | None = None
    usage: TurnUsage | None = None
    context_reading: ContextReading | None = None
    cancel_requested: bool = False
    timeout_claimed: bool = False
    finalized: bool = False
    next_event_id: int = 1
    replay: deque[StreamEvent] = field(default_factory=deque)
    replay_bytes: int = 0
    subscribers: set[asyncio.Queue[StreamEvent | None]] = field(default_factory=set)


@dataclass(frozen=True)
class CompletedRun:
    record: RoundRecord
    events: tuple[StreamEvent, ...]
    snapshot: str


@dataclass(frozen=True)
class TimeoutExtensionResult:
    timeout: TimeoutRecord
    max_addition_seconds: int
    auto_id: str | None = None


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    source: SourceDescriptor | None = None
    retry_of: int | None = None
    force_stateless: bool = False
    expected_source_sha256: str | None = None
    auto: AutoRunRequest | None = None


@dataclass(frozen=True)
class AutoRunRequest:
    auto_id: str
    phase: str
    cycle: int | None
    position: int
    context_source: Path
    context_root: Path
    context_sha256: str
    shared_source: Path | None
    shared_root: Path | None
    shared_path: str | None
    shared_sha256: str | None
    execution_prompt: str
    initial_timeout_seconds: int
    preserve_native_session: bool
    ignore_returned_session: bool


class RunManager:
    def __init__(
        self,
        *,
        registry: RegistryStore,
        locks: LockCoordinator,
        settings: Settings,
        adapter_factory: AdapterFactory,
        final_writer: Callable[[Path, str], None] = atomic_write_text,
    ) -> None:
        self.registry = registry
        self.locks = locks
        self.settings = settings
        self.adapter_factory = adapter_factory
        self.final_writer = final_writer
        self._active: dict[RunKey, ActiveRun] = {}
        self._completed: dict[RunKey, CompletedRun] = {}

    def has_active_project(self, project_id: str) -> bool:
        validate_id(project_id, "project id")
        return any(key.project_id == project_id for key in self._active)

    @staticmethod
    def _display_deadline_after(seconds: int) -> str:
        return (
            datetime.now(UTC) + timedelta(seconds=seconds)
        ).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _extend_display_deadline(value: str, seconds: int) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed + timedelta(seconds=seconds)).isoformat().replace(
            "+00:00",
            "Z",
        )

    async def start(
        self,
        project_id: str,
        session_id: str,
        prompt: str,
        *,
        source: SourceDescriptor | None = None,
    ) -> RunKey:
        session_ids = [session_id]
        if source is not None:
            if (
                source.type != "pass"
                or not isinstance(source.from_session, str)
                or not isinstance(source.from_round, int)
                or source.from_round < 1
            ):
                raise StorageError("pass source descriptor is invalid")
            session_ids.append(source.from_session)
        async with self.locks.registry_project_sessions(project_id, session_ids):
            return await self._start_locked(
                project_id,
                session_id,
                RunRequest(prompt=prompt, source=source),
            )

    async def start_auto_locked(
        self,
        project_id: str,
        session_id: str,
        request: AutoRunRequest,
    ) -> RunKey:
        """Start one reservation-owned Auto turn while the caller holds its locks."""

        return await self._start_locked(
            project_id,
            session_id,
            RunRequest(prompt=request.execution_prompt, auto=request),
        )

    def _validate_auto_request(
        self,
        store: ProjectStore,
        config: SessionConfig,
        request: AutoRunRequest,
    ) -> AutoRunRecord:
        validate_id(request.auto_id, "Auto run id")
        record = store.require_auto_owner(request.auto_id)
        if request.phase not in {"preparation", "discussion"}:
            raise StorageError("Auto phase is invalid")
        expected_status = (
            "preparing" if request.phase == "preparation" else "discussing"
        )
        if record.status != expected_status or record.stop_requested:
            raise ConflictError("Auto run is not ready for this phase")
        if (
            record.active_key is not None
            or record.active_timeout is not None
        ):
            raise ConflictError("Auto run already has an active turn")
        if (
            type(request.position) is not int
            or request.position != record.next_participant
            or not 0 <= request.position < len(record.participants)
        ):
            raise ConflictError("Auto next participant changed")
        participant = record.participants[request.position]
        if (
            participant.session_id,
            participant.name,
            participant.agent,
            participant.model,
            participant.effort,
        ) != (
            config.id,
            config.name,
            config.agent,
            config.model,
            config.effort,
        ):
            raise ConflictError("Auto participant configuration changed")
        if (
            type(request.preserve_native_session) is not bool
            or type(request.ignore_returned_session) is not bool
        ):
            raise StorageError("Auto native-session policy is invalid")
        if request.phase == "preparation":
            if request.cycle is not None:
                raise StorageError("Auto preparation request is invalid")
        elif (
            type(request.cycle) is not int
            or request.cycle < 1
            or request.cycle != record.current_cycle
        ):
            raise StorageError("Auto discussion request is invalid")
        if (
            type(request.initial_timeout_seconds) is not int
            or not 1 <= request.initial_timeout_seconds <= self.settings.max_run_timeout
        ):
            raise StorageError("Auto timeout budget is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", request.context_sha256) is None:
            raise StorageError("Auto context digest is invalid")
        run_dir = store.auto_run_dir(request.auto_id).resolve(strict=True)
        if request.context_root.resolve(strict=True) != run_dir:
            raise OwnershipError("Auto context root is invalid")
        try:
            request.context_source.resolve(strict=True).relative_to(run_dir)
        except (OSError, ValueError) as exc:
            raise OwnershipError("Auto context source is invalid") from exc
        shared_values = (
            request.shared_source,
            request.shared_root,
            request.shared_path,
            request.shared_sha256,
        )
        if any(value is not None for value in shared_values):
            if any(value is None for value in shared_values):
                raise StorageError("Auto shared context is incomplete")
            assert request.shared_source is not None
            assert request.shared_root is not None
            assert request.shared_sha256 is not None
            if record.shared_context is None or (
                request.shared_path != record.shared_context_source
                or request.shared_sha256 != record.shared_context.sha256
            ):
                raise ConflictError("Auto shared context changed")
            if request.shared_root.resolve(strict=True) != run_dir:
                raise OwnershipError("Auto shared context root is invalid")
            try:
                request.shared_source.resolve(strict=True).relative_to(run_dir)
            except (OSError, ValueError) as exc:
                raise OwnershipError("Auto shared context source is invalid") from exc
        elif record.shared_context is not None:
            raise StorageError("Auto shared context is incomplete")
        return record

    async def retry(
        self,
        project_id: str,
        session_id: str,
        round_n: int,
    ) -> RunKey:
        async with self.locks.registry_project_sessions(project_id, [session_id]):
            project = self.registry.get(project_id)
            store = ProjectStore(project)
            record = self._retry_record(
                store.load_session(session_id),
                round_n,
            )
            discovered_source = (
                record.source.type,
                record.source.from_session,
            )
            source_session_id = (
                record.source.from_session
                if record.source.type == "pass"
                else None
            )

        session_ids = [session_id]
        if source_session_id is not None:
            session_ids.append(source_session_id)
        async with self.locks.registry_project_sessions(project_id, session_ids):
            project = self.registry.get(project_id)
            store = ProjectStore(project)
            record = self._retry_record(
                store.load_session(session_id),
                round_n,
            )
            current_source = (record.source.type, record.source.from_session)
            if current_source != discovered_source:
                raise ConflictError("retry source changed during validation")
            if record.source.type == "pass":
                retry_source = record.source
                expected_source_sha256 = record.source.source_sha256
            else:
                retry_source = None
                expected_source_sha256 = None
            prompt = self._retry_prompt(store, session_id, round_n)
            return await self._start_locked(
                project_id,
                session_id,
                RunRequest(
                    prompt=prompt,
                    source=retry_source,
                    retry_of=round_n,
                    force_stateless=True,
                    expected_source_sha256=expected_source_sha256,
                ),
            )

    @staticmethod
    def _retry_record(config: SessionConfig, round_n: int) -> RoundRecord:
        record = next((item for item in config.rounds if item.n == round_n), None)
        if record is None:
            raise StorageError("retry round does not exist")
        if record.status != "error":
            raise ConflictError("only an error round can be retried")
        if record.source.type not in {"user", "pass"}:
            raise ConflictError("retry source type is unsupported")
        if record.source.type == "pass" and (
            not isinstance(record.source.from_session, str)
            or not isinstance(record.source.from_round, int)
            or record.source.from_round < 1
            or not isinstance(record.source.staged_file, str)
            or not record.source.staged_file
            or not isinstance(record.source.source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", record.source.source_sha256)
        ):
            raise StorageError("retry source provenance is incomplete")
        return record

    @staticmethod
    def _retry_prompt(
        store: ProjectStore,
        session_id: str,
        round_n: int,
    ) -> str:
        prompt_path = store.rounds_dir(session_id) / f"round-{round_n:02d}.prompt.md"
        try:
            return prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StorageError("retry prompt is unavailable") from exc

    async def _start_locked(
        self,
        project_id: str,
        session_id: str,
        request: RunRequest,
    ) -> RunKey:
        input_root: Path | None = None
        project = self.registry.get(project_id)
        store = ProjectStore(project)
        config = store.load_session(session_id)
        auto_request = request.auto
        auto_record = (
            self._validate_auto_request(store, config, auto_request)
            if auto_request is not None
            else None
        )
        if auto_request is None:
            store.require_auto_inactive()
        if config.status == "running" or any(
            key.project_id == project_id and key.session_id == session_id
            for key in self._active
        ):
            raise SessionBusy("session already has a running agent")

        round_n = store.allocate_round(session_id)
        key = RunKey(project_id, session_id, round_n)
        workspace = store.workspace_dir(session_id)
        if auto_request is not None:
            strategy = "stateless"
            resume_id = None
            if not auto_request.preserve_native_session:
                config.cli_session_id = None
        elif request.force_stateless:
            strategy = "stateless"
            resume_id = None
            config.cli_session_id = None
        else:
            strategy = (
                "native" if round_n == 1 or config.cli_session_id else "stateless"
            )
            resume_id = config.cli_session_id
        staged_history: list[Path] = []
        staged_source: Path | None = None
        staged_shared_context: Path | None = None
        shared_context: SharedContextDescriptor | None = None
        execution_prompt = request.prompt
        record_source = SourceDescriptor(type="user")
        pass_source: tuple[SessionConfig, Path] | None = None
        if (
            auto_request is None
            and request.source is not None
            and request.expected_source_sha256 is None
        ):
            source_config, source_path = self._pass_source(store, request.source)
            staged_source = Path("inputs") / f"round-{round_n:02d}" / "source.md"
            try:
                execution_prompt = render_pass_prompt(
                    request.prompt,
                    source_path=staged_source.as_posix(),
                    source_session=source_config.name,
                    source_round=request.source.from_round,
                )
            except PassPromptTemplateError as exc:
                raise StorageError(str(exc)) from exc
            pass_source = (source_config, source_path)
        try:
            if auto_request is not None:
                input_root = self._create_input_root(store, session_id, round_n)
                staged_source = (
                    Path("inputs") / f"round-{round_n:02d}" / "auto-context.md"
                )
                digest = safe_copy_file(
                    auto_request.context_source,
                    auto_request.context_root,
                    workspace / staged_source,
                    input_root,
                )
                if digest != auto_request.context_sha256:
                    raise ConflictError("Auto context digest changed")
                record_source = SourceDescriptor(
                    type="auto",
                    staged_file=staged_source.as_posix(),
                    source_sha256=digest,
                )
                execution_prompt = auto_request.execution_prompt
                if auto_request.shared_source is not None:
                    assert auto_request.shared_root is not None
                    assert auto_request.shared_path is not None
                    assert auto_request.shared_sha256 is not None
                    staged_shared_context = (
                        Path("inputs")
                        / f"round-{round_n:02d}"
                        / "shared-context.md"
                    )
                    shared_digest = safe_copy_file(
                        auto_request.shared_source,
                        auto_request.shared_root,
                        workspace / staged_shared_context,
                        input_root,
                    )
                    if shared_digest != auto_request.shared_sha256:
                        raise ConflictError("Auto shared context digest changed")
                    shared_context = SharedContextDescriptor(
                        path=auto_request.shared_path,
                        staged_file=staged_shared_context.as_posix(),
                        sha256=shared_digest,
                    )
            elif strategy == "stateless":
                input_root, staged_history = self._stage_history(
                    store,
                    config,
                    round_n,
                )
            shared_document = (
                None
                if auto_request is not None
                else store.read_selected_shared_markdown(
                    self.settings.file_view_limit
                )
            )
            if auto_request is None and shared_document is not None:
                if input_root is None:
                    input_root = self._create_input_root(
                        store,
                        session_id,
                        round_n,
                    )
                staged_shared_context = (
                    Path("inputs") / f"round-{round_n:02d}" / "shared-context.md"
                )
                atomic_write_bytes(
                    workspace / staged_shared_context,
                    shared_document.text.encode("utf-8"),
                )
                shared_context = SharedContextDescriptor(
                    path=shared_document.relative_path,
                    staged_file=staged_shared_context.as_posix(),
                    sha256=shared_document.sha256,
                )
            if auto_request is None and request.expected_source_sha256 is not None:
                retry_source = request.source
                if (
                    retry_source is None
                    or retry_source.type != "pass"
                    or retry_source.source_sha256
                    != request.expected_source_sha256
                ):
                    raise StorageError("retry source provenance is incomplete")
                old_staged = retry_source.staged_file
                if not old_staged:
                    raise StorageError("retry source provenance is incomplete")
                try:
                    source_config, source_path = self._pass_source(
                        store,
                        retry_source,
                    )
                except NotFoundError:
                    source_path = workspace / old_staged
                    source_root = workspace
                else:
                    source_root = store.rounds_dir(source_config.id)
                if execution_prompt.count(old_staged) != 1:
                    raise ConflictError(
                        "retry source prompt has ambiguous staging provenance"
                    )
                if input_root is None:
                    input_root = self._create_input_root(
                        store,
                        session_id,
                        round_n,
                    )
                staged_source = (
                    Path("inputs") / f"round-{round_n:02d}" / "source.md"
                )
                try:
                    digest = safe_copy_file(
                        source_path,
                        source_root,
                        workspace / staged_source,
                        input_root,
                    )
                except OwnershipError as exc:
                    raise StorageError("retry source is unavailable") from exc
                if digest != request.expected_source_sha256:
                    raise ConflictError(
                        "retry source no longer matches its recorded digest"
                    )
                execution_prompt = execution_prompt.replace(
                    old_staged,
                    staged_source.as_posix(),
                    1,
                )
                record_source = SourceDescriptor(
                    type="pass",
                    from_session=retry_source.from_session,
                    from_round=retry_source.from_round,
                    staged_file=staged_source.as_posix(),
                    source_sha256=digest,
                )
            elif auto_request is None and request.source is not None:
                assert pass_source is not None
                assert staged_source is not None
                source_config, source_path = pass_source
                if input_root is None:
                    input_root = self._create_input_root(store, session_id, round_n)
                digest = safe_copy_file(
                    source_path,
                    store.rounds_dir(source_config.id),
                    workspace / staged_source,
                    input_root,
                )
                record_source = SourceDescriptor(
                    type="pass",
                    from_session=source_config.id,
                    from_round=request.source.from_round,
                    staged_file=staged_source.as_posix(),
                    source_sha256=digest,
                )
        except Exception:
            self._cleanup_input_root(input_root)
            raise
        context = RunContext(
            user_prompt=execution_prompt,
            resume_id=resume_id,
            resume_strategy=strategy,
            staged_history=staged_history,
            staged_source=staged_source,
            workspace=workspace,
            staged_shared_context=staged_shared_context,
        )
        try:
            adapter = self.adapter_factory(config)
            command = adapter.build_command(config, context)
        except Exception:
            self._cleanup_input_root(input_root)
            raise
        rounds = store.rounds_dir(session_id)
        prompt_path = rounds / f"round-{round_n:02d}.prompt.md"
        partial_path = rounds / f"round-{round_n:02d}.partial.md"
        output_path = rounds / f"round-{round_n:02d}.md"
        initial_timeout = (
            auto_request.initial_timeout_seconds
            if auto_request is not None
            else self.settings.run_timeout
        )
        timeout = TimeoutRecord(
            initial_seconds=initial_timeout,
            effective_seconds=initial_timeout,
            hard_cap_seconds=self.settings.max_run_timeout,
            deadline_at=self._display_deadline_after(initial_timeout),
        )
        if auto_request is not None:
            assert staged_source is not None
        record = RoundRecord(
            n=round_n,
            status="running",
            error=None,
            warnings=(
                [STATELESS_CONTINUATION_WARNING]
                if (
                    auto_request is None
                    and round_n > 1
                    and strategy == "stateless"
                )
                else []
            ),
            agent=config.agent,
            model=config.model,
            effort=config.effort,
            started_at=utc_now(),
            finished_at=None,
            source=record_source,
            shared_context=shared_context,
            retry_of=request.retry_of,
            auto=(
                AutoRoundDescriptor(
                    auto_id=auto_request.auto_id,
                    phase=auto_request.phase,
                    cycle=auto_request.cycle,
                    position=auto_request.position,
                    context_file=staged_source.as_posix(),
                    context_sha256=auto_request.context_sha256,
                )
                if auto_request is not None
                else None
            ),
            timeout=timeout,
        )

        auto_state_persisted = False
        try:
            atomic_write_text(prompt_path, execution_prompt)
            atomic_write_text(partial_path, "")
            if auto_record is not None and auto_request is not None:
                auto_record.active_key = key
                auto_record.active_timeout = deepcopy(timeout)
                store.save_auto_run(auto_record)
                auto_state_persisted = True
            config.rounds.append(record)
            config.status = "running"
            store.save_session(config)
        except Exception:
            if auto_state_persisted and auto_record is not None:
                auto_record.active_key = None
                auto_record.active_timeout = None
                auto_record.status = "error"
                auto_record.terminal_reason = "failed to persist Auto round"
                try:
                    store.save_auto_run(auto_record)
                except Exception:
                    LOGGER.exception("Failed to roll back Auto state for %s", key)
            prompt_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
            self._cleanup_input_root(input_root)
            raise

        loop = asyncio.get_running_loop()
        completion = loop.create_future()
        started_monotonic = loop.time()
        active = ActiveRun(
            key=key,
            project=project,
            store=store,
            config=config,
            record=record,
            adapter=adapter,
            process=None,
            partial_path=partial_path,
            output_path=output_path,
            completion=completion,
            started_monotonic=started_monotonic,
            deadline_monotonic=started_monotonic + initial_timeout,
            hard_deadline_monotonic=(
                started_monotonic + self.settings.max_run_timeout
            ),
            deadline_changed=asyncio.Event(),
            auto_request=auto_request,
            warnings=list(record.warnings),
        )
        self._active[key] = active
        try:
            environment = self._subprocess_environment(config, workspace)
            active.process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=self.settings.stdout_line_limit + 1,
            )
        except (OSError, ValueError, StorageError) as exc:
            message = self._normalize_error(
                config.agent,
                f"failed to spawn agent: {exc}",
            )
            active.errors.append(message)
            await self._finalize_locked(active, None)
            return key

        active.task = asyncio.create_task(
            self._run_active(active, command.stdin),
            name=f"delibra-run-{session_id}-{round_n}",
        )
        return key

    @staticmethod
    def _pass_source(
        store: ProjectStore,
        source: SourceDescriptor,
    ) -> tuple[SessionConfig, Path]:
        if source.from_session is None or source.from_round is None:
            raise StorageError("pass source descriptor is invalid")
        source_config = store.load_session(source.from_session)
        record = next(
            (item for item in source_config.rounds if item.n == source.from_round),
            None,
        )
        if record is None:
            raise StorageError("source round does not exist")
        if record.status != "complete":
            raise ConflictError("only a complete round can be passed")
        source_path = (
            store.rounds_dir(source_config.id) / f"round-{source.from_round:02d}.md"
        )
        return source_config, source_path

    @staticmethod
    def _create_input_root(
        store: ProjectStore,
        session_id: str,
        round_n: int,
    ) -> Path:
        input_root = (
            store.workspace_dir(session_id) / "inputs" / f"round-{round_n:02d}"
        )
        ensure_owned_directory(input_root.parent, store.workspace_dir(session_id))
        try:
            input_root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ConflictError("round input staging path already exists") from exc
        except OSError as exc:
            raise StorageError("round input staging path is unavailable") from exc
        return input_root

    @staticmethod
    def _cleanup_input_root(input_root: Path | None) -> None:
        if input_root is not None:
            shutil.rmtree(input_root, ignore_errors=True)

    def _stage_history(
        self,
        store: ProjectStore,
        config: SessionConfig,
        round_n: int,
    ) -> tuple[Path, list[Path]]:
        completed = sorted(
            (
                record
                for record in config.rounds
                if record.status == "complete"
                and not (
                    record.auto is not None
                    and record.auto.phase == "preparation"
                )
            ),
            key=lambda record: record.n,
            reverse=True,
        )
        selected: list[tuple[int, Path, Path, int]] = []
        omitted: list[int] = []
        total = 0
        rounds_dir = store.rounds_dir(config.id)
        for index, record in enumerate(completed):
            prompt = rounds_dir / f"round-{record.n:02d}.prompt.md"
            output = rounds_dir / f"round-{record.n:02d}.md"
            try:
                pair_size = prompt.stat().st_size + output.stat().st_size
            except OSError as exc:
                raise StorageError(f"history files unavailable for round {record.n}") from exc
            if index == 0 and pair_size > self.settings.stateless_history_limit:
                raise StorageError("newest history round exceeds the stateless byte limit")
            if (
                len(selected) >= self.settings.stateless_round_limit
                or total + pair_size > self.settings.stateless_history_limit
            ):
                omitted.append(record.n)
                continue
            selected.append((record.n, prompt, output, pair_size))
            total += pair_size

        input_root: Path | None = None
        try:
            input_root = self._create_input_root(store, config.id, round_n)
            history_root = input_root / "history"
            history_root.mkdir(mode=0o700)
            staged: list[Path] = []
            for number, prompt, output, _ in sorted(selected, key=lambda item: item[0]):
                for source in (prompt, output):
                    destination = history_root / source.name
                    safe_copy_file(source, rounds_dir, destination, history_root)
                    staged.append(destination.relative_to(store.workspace_dir(config.id)))
            manifest = {
                "format": "delibra-history/1",
                "round_limit": self.settings.stateless_round_limit,
                "byte_limit": self.settings.stateless_history_limit,
                "included_rounds": [item[0] for item in sorted(selected)],
                "omitted_rounds": sorted(omitted),
                "omission_reason": "round or byte budget" if omitted else None,
                "staged_bytes": total,
            }
            atomic_write_json(history_root / "manifest.json", manifest)
            return input_root, staged
        except Exception:
            self._cleanup_input_root(input_root)
            raise

    def _subprocess_environment(
        self,
        config: SessionConfig,
        workspace: Path,
    ) -> dict[str, str]:
        environment = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM")
            if name in os.environ
        }
        tmpdir = workspace / ".tmp"
        ensure_owned_directory(tmpdir, workspace)
        environment["TMPDIR"] = str(tmpdir)
        if config.agent == "codex":
            self._prepare_codex_home()
            environment["CODEX_HOME"] = str(self.settings.codex_home)
        return environment

    def _prepare_codex_home(self) -> None:
        codex_home = self.settings.codex_home
        ensure_owned_directory(codex_home, self.settings.home)
        os.chmod(codex_home, 0o700)
        target = codex_home / "auth.json"
        if target.is_symlink():
            raise StorageError("Codex auth path must not be a symlink")
        if target.is_file():
            os.chmod(target, 0o600)
            return
        if target.exists():
            raise StorageError("Codex auth path is not a regular file")
        raise StorageError("Codex isolated login is missing")

    @staticmethod
    def _fold_error_category(
        active: ActiveRun,
        error: str | None,
        raw_stderr: str,
    ) -> str:
        """Fold every piece of failure evidence into one category.

        Adapters only see stdout JSONL, so stderr, runner-created failures, and
        the normalized Codex auth message would otherwise never acquire a
        category and a quota or auth marker present only there would go terminal
        instead of pausing. Unclassified text folds to ``unknown`` and is
        dropped, so incidental noise cannot outrank a proven retryable event.
        """

        evidence = [*active.error_categories]
        evidence.append(classify_text(raw_stderr))
        evidence.append(classify_text(error or ""))
        return fold_categories(evidence)

    def _normalize_error(self, agent: str, value: str) -> str:
        lowered = value.casefold()
        if agent != "codex" or not any(
            marker in lowered for marker in CODEX_AUTH_MARKERS
        ):
            return value
        home = shlex.quote(str(self.settings.codex_home))
        return (
            "Codex authentication for Delibra needs renewal. Run: "
            f"env CODEX_HOME={home} codex login --device-auth, then Retry."
        )

    async def _run_active(self, active: ActiveRun, stdin_payload: str) -> None:
        process = active.process
        assert process is not None
        try:
            if process.stdin is not None:
                process.stdin.write(stdin_payload.encode("utf-8"))
                try:
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    active.warnings.append("agent closed stdin before reading the prompt")
                process.stdin.close()

            stdout_task = asyncio.create_task(self._consume_stdout(active))
            stderr_task = asyncio.create_task(self._consume_stderr(active))
            wait_task = asyncio.create_task(process.wait())
            await self._wait_for_process_or_deadline(active, wait_task)
            results = await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    exception = result
                else:
                    exception = None
                if exception is not None:
                    active.errors.append(f"agent stream failed: {exception}")
                    await self._terminate(active)
            returncode = process.returncode
        except asyncio.CancelledError:
            active.cancel_requested = True
            await self._terminate(active)
            returncode = process.returncode
        except Exception as exc:
            active.errors.append(f"agent runner failed: {exc}")
            await self._terminate(active)
            returncode = process.returncode
        await self._finalize(active, returncode)

    async def _wait_for_process_or_deadline(
        self,
        active: ActiveRun,
        wait_task: asyncio.Task[int],
    ) -> None:
        loop = asyncio.get_running_loop()
        while not wait_task.done():
            remaining = max(0.0, active.deadline_monotonic - loop.time())
            changed = asyncio.create_task(active.deadline_changed.wait())
            done, _ = await asyncio.wait(
                {wait_task, changed},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in done:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                return
            if changed in done:
                active.deadline_changed.clear()
                continue
            changed.cancel()
            await asyncio.gather(changed, return_exceptions=True)
            async with self.locks.sessions(
                active.key.project_id,
                [active.key.session_id],
            ):
                if wait_task.done() or active.process is None:
                    return
                if active.process.returncode is not None:
                    return
                if loop.time() < active.deadline_monotonic:
                    continue
                if active.deadline_changed.is_set():
                    active.deadline_changed.clear()
                    continue
                if active.cancel_requested or active.finalized:
                    return
                active.timeout_claimed = True
                timeout = active.record.timeout
                effective = timeout.effective_seconds if timeout is not None else 0
                active.errors.append(f"agent timed out after {effective} seconds")
            await self._terminate(active)
            await asyncio.shield(wait_task)
            return

    async def _consume_stdout(self, active: ActiveRun) -> None:
        process = active.process
        assert process is not None and process.stdout is not None
        while True:
            try:
                line = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                active.errors.append("agent stdout line limit exceeded")
                self._signal_process(active, signal.SIGTERM)
                await self._drain_stdout(process.stdout)
                return
            if not line:
                return
            if len(line) > self.settings.stdout_line_limit:
                active.errors.append("agent stdout line limit exceeded")
                self._signal_process(active, signal.SIGTERM)
                await self._drain_stdout(process.stdout)
                return
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                active.errors.append("agent stdout was not valid UTF-8")
                self._signal_process(active, signal.SIGTERM)
                await self._drain_stdout(process.stdout)
                return
            for event in active.adapter.parse_line(decoded.rstrip("\r\n")):
                if not await self._handle_agent_event(active, event):
                    await self._drain_stdout(process.stdout)
                    return

    @staticmethod
    async def _drain_stdout(stream: asyncio.StreamReader) -> None:
        while True:
            try:
                block = await stream.read(4_096)
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not block:
                return

    async def _handle_agent_event(self, active: ActiveRun, event: AgentEvent) -> bool:
        if event.kind == "init":
            if event.cli_session_id and not (
                active.auto_request is not None
                and active.auto_request.ignore_returned_session
            ):
                active.cli_session_id = event.cli_session_id
            return True
        if event.kind == "text_delta":
            encoded = event.text.encode("utf-8")
            remaining = self.settings.captured_output_limit - len(active.captured)
            if remaining <= 0:
                active.errors.append("captured output limit exceeded")
                self._signal_process(active, signal.SIGTERM)
                return False
            accepted = encoded[:remaining]
            while accepted:
                try:
                    text = accepted.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    accepted = accepted[:-1]
            else:
                text = ""
            if accepted:
                active.captured.extend(accepted)
                with active.partial_path.open("ab") as partial:
                    partial.write(accepted)
                    partial.flush()
                self._publish(active, "text_delta", text)
            if len(encoded) > remaining:
                active.errors.append("captured output limit exceeded")
                self._signal_process(active, signal.SIGTERM)
                return False
            return True
        if event.kind == "turn_usage":
            # Latest wins: an adapter reports once per turn, and a second
            # report would describe the same round more completely.
            active.usage = event.usage
            return True
        if event.kind == "context_usage":
            active.context_reading = event.context
            return True
        if event.kind == "progress":
            self._publish(active, "progress", event.text)
        elif event.kind == "warning":
            active.warnings.append(event.text)
            self._publish(active, "warning", event.text)
        elif event.kind == "error":
            message = self._normalize_error(active.config.agent, event.text)
            active.errors.append(message)
            # Classify the raw text, never the normalized one: normalization
            # rewrites the Codex auth message into user-facing instructions that
            # no longer carry the markers that identify it.
            active.error_categories.append(
                event.error_info.category
                if event.error_info is not None
                else classify_text(event.text)
            )
            self._publish(active, "error", message)
        return True

    async def _consume_stderr(self, active: ActiveRun) -> None:
        process = active.process
        assert process is not None and process.stderr is not None
        while True:
            block = await process.stderr.read(4_096)
            if not block:
                return
            active.stderr_tail.extend(block)
            excess = len(active.stderr_tail) - self.settings.stderr_tail_limit
            if excess > 0:
                del active.stderr_tail[:excess]

    async def _terminate(self, active: ActiveRun) -> None:
        process = active.process
        if process is None or process.returncode is not None:
            return
        self._signal_process(active, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    @staticmethod
    def _signal_process(active: ActiveRun, requested_signal: signal.Signals) -> None:
        process = active.process
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, requested_signal)
        except ProcessLookupError:
            pass

    async def _finalize(self, active: ActiveRun, returncode: int | None) -> None:
        async with self.locks.sessions(
            active.key.project_id,
            [active.key.session_id],
        ):
            await self._finalize_locked(active, returncode)

    async def _finalize_locked(
        self,
        active: ActiveRun,
        returncode: int | None,
    ) -> None:
        if active.finalized:
            return
        active.finalized = True
        record = active.record
        raw_stderr = active.stderr_tail.decode("utf-8", errors="replace").strip()
        stderr = (
            self._normalize_error(active.config.agent, raw_stderr)
            if raw_stderr
            else ""
        )
        if active.cancel_requested:
            status = "cancelled"
            error = "cancelled by user"
        elif active.errors:
            status = "error"
            error = "; ".join(dict.fromkeys(active.errors))
        elif returncode not in (0, None):
            status = "error"
            error = f"agent exited with code {returncode}"
        elif not active.adapter.final_text():
            status = "error"
            error = "agent returned empty final result"
        else:
            status = "complete"
            error = None
        if (
            status == "error"
            and stderr
            and stderr not in active.errors
            and stderr not in (error or "")
        ):
            error = f"{error}; stderr: {stderr}"
        suppress_native_warning = (
            active.auto_request is not None
            and active.auto_request.ignore_returned_session
        )
        if not active.cli_session_id and not suppress_native_warning:
            active.warnings.append("provider did not return a native session id")

        auto_discussion = (
            active.auto_request is not None
            and active.auto_request.phase == "discussion"
        )
        parsed_verdict: str | None = None
        if status == "complete":
            output = active.adapter.final_text()
            if auto_discussion:
                parsed_verdict = parse_auto_verdict(output)
        else:
            output = active.captured.decode("utf-8", errors="replace")
        output_persisted = False
        metadata_persisted = False
        try:
            self.final_writer(active.output_path, output)
            output_persisted = True
        except Exception as exc:
            status = "error"
            error = f"failed to persist final output: {exc}"
            LOGGER.exception("Failed to persist final output for %s", active.key)

        if status == "complete" and parsed_verdict is not None:
            assert record.auto is not None
            record.auto = replace(record.auto, verdict=parsed_verdict)
        record.status = status
        record.error = error
        record.error_category = (
            self._fold_error_category(active, error, raw_stderr)
            if status == "error"
            else None
        )
        record.warnings = list(dict.fromkeys(active.warnings))
        record.finished_at = utc_now()
        record.usage = active.usage
        if active.context_reading is not None:
            # Only the runner knows which round the reading belongs to. A turn
            # that reported nothing leaves the last known observation alone: a
            # silent turn is not evidence that the window emptied.
            active.config.context_observation = active.context_reading.observed(
                round_n=record.n,
                observed_at=record.finished_at,
            )
        active.config.status = "idle" if status in {"complete", "cancelled"} else "error"
        if active.cli_session_id and not suppress_native_warning:
            active.config.cli_session_id = active.cli_session_id
        try:
            active.store.save_session(active.config)
            metadata_persisted = True
        except Exception as exc:
            status = "error"
            record.status = "error"
            record.error = f"failed to persist terminal metadata: {exc}"
            active.config.status = "error"
            if record.auto is not None:
                record.auto = replace(record.auto, verdict=None)
            LOGGER.exception("Failed to persist terminal metadata for %s", active.key)

        if output_persisted and metadata_persisted:
            active.partial_path.unlink(missing_ok=True)
        if record.status == "error":
            self._publish(active, "error", record.error or "agent run failed")
        self._publish(active, "done", record.status)

        completed = CompletedRun(
            record=record,
            events=tuple(active.replay),
            snapshot=active.captured.decode("utf-8", errors="replace"),
        )
        self._completed[active.key] = completed
        self._active.pop(active.key, None)
        if not active.completion.done():
            active.completion.set_result(record)

    def _publish(self, active: ActiveRun, kind: str, data: str = "") -> StreamEvent:
        event = StreamEvent(active.next_event_id, kind, data)
        active.next_event_id += 1
        size = len(data.encode("utf-8")) + len(kind) + 16
        active.replay.append(event)
        active.replay_bytes += size
        while len(active.replay) > 1 and active.replay_bytes > self.settings.replay_limit:
            removed = active.replay.popleft()
            active.replay_bytes -= (
                len(removed.data.encode("utf-8")) + len(removed.kind) + 16
            )
        dropped: list[asyncio.Queue[StreamEvent | None]] = []
        for queue in active.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dropped.append(queue)
        for queue in dropped:
            active.subscribers.discard(queue)
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(None)
        return event

    async def subscribe(
        self,
        key: RunKey,
        last_event_id: int | str | None = None,
    ):
        active = self._active.get(key)
        completed = self._completed.get(key)
        if active is None and completed is None:
            completed = self._completed_from_disk(key)

        if active is not None:
            events = tuple(active.replay)
            snapshot = active.captured.decode("utf-8", errors="replace")
        else:
            assert completed is not None
            events = completed.events
            snapshot = completed.snapshot

        latest = events[-1].event_id if events else 0
        parsed_last, invalid = self._parse_last_event_id(last_event_id, latest)
        initial: list[StreamEvent] = []
        if active is None and last_event_id is None:
            done = next((event for event in reversed(events) if event.kind == "done"), None)
            initial = [done or StreamEvent(1, "done", completed.record.status)]
        elif invalid or (
            parsed_last is not None
            and events
            and parsed_last < events[0].event_id - 1
        ):
            done_event = (
                next((event for event in reversed(events) if event.kind == "done"), None)
                if active is None
                else None
            )
            snapshot_id = max(0, done_event.event_id - 1) if done_event else latest
            initial = [
                StreamEvent(max(0, snapshot_id - 1), "reset", ""),
                StreamEvent(snapshot_id, "snapshot", snapshot),
            ]
            initial.extend(event for event in events if event.event_id > snapshot_id)
        else:
            threshold = parsed_last or 0
            initial = [event for event in events if event.event_id > threshold]

        queue: asyncio.Queue[StreamEvent | None] | None = None
        if active is not None and not any(event.kind == "done" for event in initial):
            queue = asyncio.Queue(maxsize=1_000)
            active.subscribers.add(queue)
        try:
            for event in initial:
                yield event
                if event.kind == "done":
                    return
            while queue is not None:
                event = await queue.get()
                if event is None:
                    return
                yield event
                if event.kind == "done":
                    return
        finally:
            if active is not None and queue is not None:
                active.subscribers.discard(queue)

    @staticmethod
    def _parse_last_event_id(
        value: int | str | None,
        latest: int,
    ) -> tuple[int | None, bool]:
        if value is None:
            return None, False
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None, True
        if parsed < 0 or parsed > latest:
            return parsed, True
        return parsed, False

    def _completed_from_disk(self, key: RunKey) -> CompletedRun:
        project = self.registry.get(key.project_id)
        store = ProjectStore(project)
        config = store.load_session(key.session_id)
        record = next((item for item in config.rounds if item.n == key.round_n), None)
        if record is None or record.status == "running":
            raise StorageError("run is not available")
        output = store.rounds_dir(key.session_id) / f"round-{key.round_n:02d}.md"
        snapshot = output.read_text(encoding="utf-8") if output.exists() else ""
        completed = CompletedRun(record, (StreamEvent(1, "done", record.status),), snapshot)
        self._completed[key] = completed
        return completed

    async def wait(self, key: RunKey) -> RoundRecord:
        active = self._active.get(key)
        if active is not None:
            return await asyncio.shield(active.completion)
        completed = self._completed.get(key)
        if completed is not None:
            return completed.record
        return self._completed_from_disk(key).record

    def timeout_snapshot(self, key: RunKey) -> TimeoutRecord:
        active = self._active.get(key)
        if active is not None and active.record.timeout is not None:
            return deepcopy(active.record.timeout)
        completed = self._completed.get(key)
        record = completed.record if completed is not None else self._completed_from_disk(key).record
        if record.timeout is None:
            raise ConflictError("round has no timeout metadata")
        return deepcopy(record.timeout)

    async def extend_timeout(
        self,
        key: RunKey,
        minutes: int,
        scope: str,
        expected_version: int,
    ) -> TimeoutExtensionResult:
        if type(minutes) is not int or not 1 <= minutes <= 240:
            raise StorageError("timeout extension minutes must be from 1 through 240")
        if scope not in {"current", "current_and_future_auto"}:
            raise StorageError("timeout extension scope is invalid")
        if type(expected_version) is not int or expected_version < 0:
            raise StorageError("timeout extension version is invalid")
        async with self.locks.project_sessions(key.project_id, [key.session_id]):
            active = self._active.get(key)
            if active is None or active.record.timeout is None:
                raise ConflictError("round is not active")
            process = active.process
            if (
                active.finalized
                or active.cancel_requested
                or active.timeout_claimed
                or process is None
                or process.returncode is not None
            ):
                raise ConflictError("round can no longer be extended")
            current = active.record.timeout
            descriptor = active.record.auto
            auto_id = descriptor.auto_id if descriptor is not None else None
            if scope == "current_and_future_auto" and auto_id is None:
                raise ConflictError("timeout extension scope is not available")
            if current.version != expected_version:
                raise ConflictError("timeout version changed")
            added_seconds = minutes * 60
            effective = current.effective_seconds + added_seconds
            if effective > current.hard_cap_seconds:
                raise ConflictError("timeout extension exceeds the maximum")
            updated = TimeoutRecord(
                initial_seconds=current.initial_seconds,
                effective_seconds=effective,
                hard_cap_seconds=current.hard_cap_seconds,
                deadline_at=self._extend_display_deadline(
                    current.deadline_at,
                    added_seconds,
                ),
                version=current.version + 1,
                extensions=[
                    *current.extensions,
                    TimeoutExtensionRecord(
                        added_seconds=added_seconds,
                        scope=scope,
                        extended_at=utc_now(),
                    ),
                ],
            )
            if auto_id is not None:
                auto_record = active.store.require_auto_owner(auto_id)
                if (
                    auto_record.stop_requested
                    or auto_record.active_key != key
                    or auto_record.active_timeout != current
                ):
                    raise ConflictError("Auto active timeout changed")
                future_timeout = auto_record.future_turn_timeout_seconds
                if scope == "current_and_future_auto":
                    future_timeout += added_seconds
                    if future_timeout > current.hard_cap_seconds:
                        raise ConflictError("future Auto timeout exceeds the maximum")
                auto_record.active_timeout = deepcopy(updated)
                auto_record.future_turn_timeout_seconds = future_timeout
                active.store.save_auto_run(auto_record)
                active.record.timeout = deepcopy(updated)
                persisted_record = next(
                    item
                    for item in active.config.rounds
                    if item.n == key.round_n
                )
                persisted_record.timeout = deepcopy(updated)
            else:
                persisted_config = active.store.load_session(key.session_id)
                persisted_record = next(
                    item for item in persisted_config.rounds if item.n == key.round_n
                )
                persisted_record.timeout = deepcopy(updated)
                active.store.save_session(persisted_config)
                active.config = persisted_config
                active.record = persisted_record
            active.deadline_monotonic += added_seconds
            active.deadline_changed.set()
            self._publish(active, "timeout_extended", str(updated.version))
            return TimeoutExtensionResult(
                timeout=deepcopy(updated),
                max_addition_seconds=updated.hard_cap_seconds - updated.effective_seconds,
                auto_id=auto_id,
            )

    def active_key(self, project_id: str, session_id: str) -> RunKey | None:
        """Return the single active run for a session, if one exists."""
        return next(
            (
                key
                for key in self._active
                if key.project_id == project_id and key.session_id == session_id
            ),
            None,
        )

    async def cancel(self, key: RunKey) -> None:
        active = self._active.get(key)
        if active is None:
            return
        async with self.locks.project_sessions(key.project_id, [key.session_id]):
            project = self.registry.get(key.project_id)
            ProjectStore(project).require_auto_inactive()
            active.cancel_requested = True
        await self._terminate(active)
        await asyncio.shield(active.completion)

    def claim_auto_cancel_locked(self, key: RunKey, auto_id: str) -> bool:
        """Claim cancellation for the exact Auto-owned active run under its locks."""

        active = self._active.get(key)
        if active is None:
            return False
        descriptor = active.record.auto
        if descriptor is None or descriptor.auto_id != auto_id:
            raise ConflictError("active run is not owned by this Auto run")
        process = active.process
        if (
            active.finalized
            or active.timeout_claimed
            or process is None
            or process.returncode is not None
        ):
            return False
        active.cancel_requested = True
        return True

    async def finish_auto_cancel(self, key: RunKey) -> None:
        active = self._active.get(key)
        if active is None:
            await self.wait(key)
            return
        await self._terminate(active)
        await asyncio.shield(active.completion)

    async def shutdown(self) -> None:
        active = list(self._active.values())
        for run in active:
            run.cancel_requested = True
        await asyncio.gather(*(self._terminate(run) for run in active))
        if active:
            await asyncio.gather(
                *(asyncio.shield(run.completion) for run in active),
                return_exceptions=True,
            )
