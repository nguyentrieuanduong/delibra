"""Orchestrates Delibra downloads: spool harvest, checks, approval, queue, events."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path

from watchfiles import awatch

from app.coding import DownloadPolicy
from app.config import Settings
from app.download_fetch import FetchError, Fetcher
from app.download_model_check import (
    BATCH_MAX, BATCH_WINDOW_SECONDS, CheckError, run_model_check,
)
from app.download_rules import (
    REQUEST_FILE_MAX_BYTES, TURN_MAX_REQUESTS, RequestError, parse_request, url_flags,
)
from app.download_store import (
    DownloadRecord, DownloadStore, render_index, render_status,
    write_project_file, write_turn_status,
)
from app.questions import OutboxError, read_spool_file, remove_spool_entry, scan_spool
from app.storage import StorageError

LOGGER = logging.getLogger(__name__)
PROJECT_CONCURRENCY = 4
HOST_CONCURRENCY = 2
# Turn end waits this long for the turn's model checks (one batch window
# plus one CLI timeout, with slack), then refuses what is left (B4).
SETTLE_SECONDS = 75


@dataclass(eq=False)
class DownloadTurn:
    project_id: str
    project_root: Path
    store_root: Path
    temp_dir: Path
    auto_run: int
    cycle: int | None
    round_n: int
    agent: str
    agent_name: str
    policy: DownloadPolicy
    ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unparsable: dict[str, tuple[int, int]] = field(default_factory=dict)
    checks: list[asyncio.Task[None]] = field(default_factory=list)
    watch: tuple[asyncio.Event, asyncio.Task[None]] | None = None

    @property
    def spool(self) -> Path:
        return self.temp_dir / "downloads" / "requests"


class _RunBudget:
    def __init__(self, store: DownloadStore, auto_run: int | None, rid: str, lock: asyncio.Lock) -> None:
        self.store, self.auto_run, self.rid, self.lock = store, auto_run, rid, lock

    async def reserve(self, amount: int) -> int:
        async with self.lock:
            return self.store.reserve(self.auto_run, amount, self.rid)

    async def release(self, amount: int) -> None:
        async with self.lock:
            self.store.release(self.auto_run, amount, self.rid)


class DownloadManager:
    def __init__(self, settings: Settings, provider_commands: dict[str, str], *,
                 fetcher: Fetcher | None = None,
                 model_check: Callable[..., Awaitable[dict[str, tuple[str, str]]]] | None = None) -> None:
        self.settings = settings
        self.commands = dict(provider_commands)
        self.fetcher = fetcher or Fetcher()
        self.model_check = model_check or run_model_check
        self._turns: dict[str, DownloadTurn] = {}
        self._budget_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._project_slots: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(PROJECT_CONCURRENCY))
        self._host_slots: dict[tuple[str, str], asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(HOST_CONCURRENCY))
        self._tasks: set[asyncio.Task[None]] = set()
        self._revisions: dict[str, int] = defaultdict(int)
        self._subscribers: dict[str, set[asyncio.Queue[int]]] = defaultdict(set)
        self._pending: dict[DownloadTurn, list[str]] = {}

    # ----- events
    def _publish(self, project_id: str) -> None:
        self._revisions[project_id] += 1
        for queue in list(self._subscribers[project_id]):
            try:
                queue.put_nowait(self._revisions[project_id])
            except asyncio.QueueFull:
                pass

    def revision(self, project_id: str) -> int:
        return self._revisions[project_id]

    async def subscribe(self, project_id: str) -> AsyncIterator[int]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=64)
        self._subscribers[project_id].add(queue)
        try:
            # The panel re-fetches on every event: sending the current
            # revision first closes the render-to-subscribe gap (S1).
            yield self._revisions[project_id]
            while True:
                yield await queue.get()
        finally:
            self._subscribers[project_id].discard(queue)

    # ----- rendering
    def _refresh(self, project_id: str, project_root: Path, store: DownloadStore,
                 record: DownloadRecord | None = None) -> None:
        records = store.load().requests
        turn = self._turns.get(project_id)
        if turn is not None and (record is None or record.auto_run == turn.auto_run):
            try:
                write_turn_status(turn.temp_dir, render_status([r for r in records if r.auto_run == turn.auto_run]))
            except OSError:
                turn.warnings.append("download status could not be written")
        if record is not None and record.state == "downloaded":
            try:
                write_project_file(project_root, record.folder, "index.md", render_index(
                    [r for r in records if r.folder == record.folder]))
            except (OSError, StorageError):
                LOGGER.warning("could not write the download index", exc_info=True)
        self._publish(project_id)

    # ----- turn lifecycle
    def begin_turn(self, turn: DownloadTurn) -> None:
        self._turns[turn.project_id] = turn
        self._refresh(turn.project_id, turn.project_root, DownloadStore(turn.store_root))
        stop = asyncio.Event()
        turn.watch = (stop, asyncio.create_task(self._watch(turn, stop)))

    async def _watch(self, turn: DownloadTurn, stop: asyncio.Event) -> None:
        try:
            self.harvest(turn, final=False)
            async for _changes in awatch(turn.spool, stop_event=stop, step=50, recursive=False, watch_filter=None):
                self.harvest(turn, final=False)
        except Exception:
            LOGGER.warning("download spool watch failed for %s", turn.spool, exc_info=True)
            turn.warnings.append("live downloads unavailable; requests are read at turn end")

    async def stop_watch(self, turn: DownloadTurn) -> None:
        if turn.watch is not None:
            stop, task = turn.watch
            stop.set(); task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            turn.watch = None

    def end_turn(self, turn: DownloadTurn) -> None:
        if self._turns.get(turn.project_id) is turn:
            del self._turns[turn.project_id]

    def harvest(self, turn: DownloadTurn, *, final: bool) -> None:
        try:
            entries = scan_spool(turn.spool)
        except (OutboxError, OSError):
            turn.warnings.append("download requests unreadable: the spool is not a real directory")
            return
        store = DownloadStore(turn.store_root)
        for entry in entries:
            if not entry.valid:
                if final:
                    self._remove(turn, entry.name)
                continue
            if turn.unparsable.get(entry.name) == entry.signature and not final:
                continue
            try:
                data = read_spool_file(turn.spool, entry.name, limit=REQUEST_FILE_MAX_BYTES)
            except (OutboxError, OSError):
                continue
            if data is None:
                continue
            try:
                proposal = parse_request(data)
            except RequestError as exc:
                if len(data) <= REQUEST_FILE_MAX_BYTES and not final:
                    turn.unparsable[entry.name] = entry.signature  # maybe half-written
                    continue
                turn.warnings.append(f"download request {entry.name} refused: {exc}")
                self._remove(turn, entry.name)
                continue
            self._remove(turn, entry.name)
            if len(turn.ids) >= TURN_MAX_REQUESTS:
                turn.warnings.append(f"download request {entry.name} refused: over {TURN_MAX_REQUESTS} this turn")
                continue
            record = store.add(
                proposal.url, purpose=proposal.purpose, filename=proposal.filename,
                flags=url_flags(proposal.url), auto_run=turn.auto_run, cycle=turn.cycle,
                round_n=turn.round_n, agent=turn.agent, agent_name=turn.agent_name,
                folder=turn.policy.folder,
            )
            turn.ids.append(record.id)
            if record.state == "checking":
                if turn.policy.model_check:
                    self._queue_check(turn, record.id)
                else:
                    record = self._after_checks(turn.project_id, turn.project_root, store, record.id, turn.policy)
            self._refresh(turn.project_id, turn.project_root, store, record)

    def _remove(self, turn: DownloadTurn, name: str) -> None:
        try:
            remove_spool_entry(turn.spool, name)
        except (OutboxError, OSError):
            LOGGER.warning("could not remove download request %s", name, exc_info=True)

    # ----- model check batching
    def _queue_check(self, turn: DownloadTurn, rid: str) -> None:
        batch = self._pending.setdefault(turn, [])
        batch.append(rid)
        if len(batch) == 1:
            task = asyncio.create_task(self._run_batch(turn))
            turn.checks.append(task)
            self._tasks.add(task)  # shutdown cancels checks too
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, turn: DownloadTurn) -> None:
        await asyncio.sleep(BATCH_WINDOW_SECONDS)
        ids = self._pending.pop(turn, [])
        store = DownloadStore(turn.store_root)
        # Chunks run concurrently: 64 requests are 4 calls, not 4 x 60 s (B4).
        await asyncio.gather(*(
            self._check_chunk(turn, store, ids[start:start + BATCH_MAX])
            for start in range(0, len(ids), BATCH_MAX)
        ))

    async def _check_chunk(self, turn: DownloadTurn, store: DownloadStore, chunk: list[str]) -> None:
        policy = turn.policy
        try:
            command = self.commands.get(policy.check_provider)
            if not command:
                raise CheckError("model check unavailable: the CLI is not configured")
            items = [(rid, store.get(rid).url) for rid in chunk]
            verdicts = await self.model_check(
                policy.check_provider, command, policy.check_model, items,
                self.settings.build_tmp_root,
            )
        except CheckError as exc:
            verdicts = {rid: ("unavailable", exc.reason) for rid in chunk}
        except Exception:  # B1: anything else also fails closed
            LOGGER.exception("download model check failed for %s", chunk)
            verdicts = {rid: ("unavailable", "model check unavailable: unexpected error") for rid in chunk}
        for rid in chunk:
            verdict, text = verdicts[rid]
            try:
                if store.get(rid).state != "checking":
                    continue  # settled already
                if verdict == "allow":
                    record = self._after_checks(turn.project_id, turn.project_root, store, rid, policy)
                elif verdict == "flag":
                    # The model's words stay out of every rendered surface (B3).
                    record = store.update(rid, state="refused", reason="flagged by model check",
                                          masked=True, check_note=text)
                else:
                    record = store.update(rid, state="refused", reason=text, masked=True)
            except StorageError:
                LOGGER.exception("could not record the model check of %s", rid)
                continue
            self._refresh(turn.project_id, turn.project_root, store, record)

    async def settle(self, turn: DownloadTurn) -> None:
        """Wait (bounded) for this turn's model checks; refuse what is still checking (B4)."""

        if turn.checks:
            _done, pending = await asyncio.wait(turn.checks, timeout=SETTLE_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.pop(turn, None)
        store = DownloadStore(turn.store_root)
        for record in store.load().requests:
            if record.id in turn.ids and record.state == "checking":
                record = store.update(record.id, state="refused", masked=True,
                                      reason="model check unavailable: timed out")
                self._refresh(turn.project_id, turn.project_root, store, record)

    def turn_awaiting(self, turn: DownloadTurn) -> list[str]:
        store = DownloadStore(turn.store_root)
        return [r.id for r in store.load().requests if r.id in turn.ids and r.state == "awaiting"]

    def turn_warnings(self, turn: DownloadTurn) -> list[str]:
        return list(dict.fromkeys(turn.warnings))

    # ----- approval and queue
    def _after_checks(self, project_id: str, project_root: Path, store: DownloadStore,
                      rid: str, policy: DownloadPolicy) -> DownloadRecord:
        if policy.approval == "manual":
            return store.update(rid, state="awaiting", reason="waiting for your approval")
        return self._enqueue(project_id, project_root, store, rid)

    def _enqueue(self, project_id: str, project_root: Path, store: DownloadStore, rid: str) -> DownloadRecord:
        record = store.update(rid, state="queued", reason=None)
        task = asyncio.create_task(self._download(project_id, project_root, store, rid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def _download(self, project_id: str, project_root: Path, store: DownloadStore, rid: str) -> None:
        record = store.get(rid)
        async with self._project_slots[project_id], self._host_slots[(project_id, record.host)]:
            record = store.update(rid, state="downloading")
            self._refresh(project_id, project_root, store, record)
            budget = _RunBudget(store, record.auto_run, rid, self._budget_locks[project_id])
            try:
                result = await self.fetcher.fetch(
                    record.url, check_first=not record.overridden, project_root=project_root,
                    folder=record.folder, filename=record.filename, request_id=rid, budget=budget,
                )
            except FetchError as exc:
                record = store.update(rid, state="failed", reason=exc.reason)
            except Exception:
                LOGGER.exception("download %s failed unexpectedly", rid)
                record = store.update(rid, state="failed", reason="unexpected error")
            else:
                record = store.update(rid, state="downloaded", reason=None, path=result.path,
                                      sha256=result.sha256, size=result.size,
                                      detected_type=result.detected_type, final_url=result.final_url)
            finally:
                store.finish_reservation(rid)  # S2: nothing stays held once the fetch ended
        self._refresh(project_id, project_root, store, record)

    async def idle(self) -> None:
        """Tests and shutdown: wait until no download task runs."""

        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _action(self, project_root: Path, store_root: Path, rid: str, allowed: tuple[str, ...]):
        store = DownloadStore(store_root)
        record = store.get(rid)
        if record.state not in allowed:
            raise StorageError(f"{rid} is {record.state}, not {' or '.join(allowed)}")
        return store, record

    def _project_id(self, store_root: Path) -> str:
        for project_id, turn in self._turns.items():
            if turn.store_root == store_root:
                return project_id
        return str(store_root)

    def approve(self, project_root: Path, store_root: Path, rid: str, project_id: str | None = None) -> DownloadRecord:
        store, _ = self._action(project_root, store_root, rid, ("awaiting",))
        pid = project_id or self._project_id(store_root)
        record = self._enqueue(pid, project_root, store, rid)
        self._refresh(pid, project_root, store, record)
        return record

    def approve_all(self, project_root: Path, store_root: Path, project_id: str | None = None) -> list[str]:
        ids = [r.id for r in DownloadStore(store_root).load().requests if r.state == "awaiting"]
        for rid in ids:
            self.approve(project_root, store_root, rid, project_id)
        return ids

    def refuse(self, project_root: Path, store_root: Path, rid: str, project_id: str | None = None) -> DownloadRecord:
        store, _ = self._action(project_root, store_root, rid, ("awaiting",))
        record = store.update(rid, state="refused", reason="refused by you")
        self._refresh(project_id or self._project_id(store_root), project_root, store, record)
        return record

    def allow(self, project_root: Path, store_root: Path, rid: str, project_id: str | None = None) -> DownloadRecord:
        store, record = self._action(project_root, store_root, rid, ("refused",))
        if record.reason == "refused by you":
            raise StorageError(f"{rid} was refused by you; it cannot be allowed")
        store.update(rid, overridden=True)
        pid = project_id or self._project_id(store_root)
        record = self._enqueue(pid, project_root, store, rid)
        self._refresh(pid, project_root, store, record)
        return record

    def retry(self, project_root: Path, store_root: Path, rid: str, project_id: str | None = None) -> DownloadRecord:
        store, _ = self._action(project_root, store_root, rid, ("failed",))
        pid = project_id or self._project_id(store_root)
        record = self._enqueue(pid, project_root, store, rid)
        self._refresh(pid, project_root, store, record)
        return record

    def recover(self, store_root: Path) -> None:
        try:
            DownloadStore(store_root).recover()
        except StorageError:
            LOGGER.exception("download records could not be recovered under %s", store_root)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
