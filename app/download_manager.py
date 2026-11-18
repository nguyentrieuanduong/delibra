"""Orchestrates Delibra downloads: spool harvest, checks, approval, queue, events."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
import logging
from pathlib import Path
import shutil
import tempfile
from typing import Any, TypeVar

from watchfiles import awatch

from app.coding import DownloadPolicy
from app.config import Settings
from app.converter_models import ready_models_dir
from app.doc_convert import ConvertResult, convert_file, stage_source
from app.download_fetch import FetchError, Fetcher
from app.download_model_check import (
    BATCH_MAX, BATCH_WINDOW_SECONDS, CheckError, run_model_check,
)
from app.convert_store import ConversionRecord, ConversionStore, render_convert_status
from app.download_rules import (
    CONVERT_DIR, CONVERT_REQUEST_MAX_BYTES, CONVERT_SPOOL, DOWNLOAD_SPOOL, DOWNLOADS_DIR, REQUEST_FILE_MAX_BYTES,
    STATUS_NAME, TURN_MAX_CONVERSIONS, TURN_MAX_REQUESTS, RequestError, convert_prompt, converted_output,
    downloads_prompt, parse_convert_request, parse_request, url_flags,
)
from app.download_store import (
    DownloadRecord, DownloadStore, render_index, render_status,
    write_project_file, write_turn_status,
)
from app.questions import OutboxError, prepare_spool, read_spool_file, remove_spool_entry, scan_spool
from app.storage import StorageError

LOGGER = logging.getLogger(__name__)
PROJECT_CONCURRENCY = 4
HOST_CONCURRENCY = 2
# Turn end waits this long for the turn's model checks (one batch window
# plus one CLI timeout, with slack), then refuses what is left (B4).
SETTLE_SECONDS = 75
# A broken store must not fail the build turn: it becomes the turn's warning
# (and Auto turns commits off on its own, plan review B5).
UNREADABLE_WARNING = "download records are unreadable; download requests were not read"
NOT_CONVERTIBLE = "not a convertible project file"
T = TypeVar("T")


def has_shell(agent: str, *, build: bool) -> bool:
    """Build turns and Codex turns run in a shell; Claude chat and discussion turns
    do not (`--tools` without Bash), so they get no wait loop (design Part A)."""

    return build or agent == "codex"


def turn_spool_notes(temp_root: Path, policy: DownloadPolicy, *, agent: str, build: bool) -> str:
    """The turn's download and convert notes. Discussion and chat turns get fresh
    spools in `temp_root` here; build turns get theirs when the runner recreates
    the temp dir."""

    shell = has_shell(agent, build=build)
    notes = ""
    if policy.enabled:
        directory = temp_root / DOWNLOADS_DIR
        if not build:
            prepare_spool(temp_root, DOWNLOADS_DIR, DOWNLOAD_SPOOL, extra_names=(STATUS_NAME,))
        notes += downloads_prompt(directory / STATUS_NAME, directory / DOWNLOAD_SPOOL,
                                  manual=policy.approval == "manual", shell=shell)
    if policy.convert:
        directory = temp_root / CONVERT_DIR
        if not build:
            prepare_spool(temp_root, CONVERT_DIR, CONVERT_SPOOL, extra_names=(STATUS_NAME,))
        notes += convert_prompt(directory / STATUS_NAME, directory / CONVERT_SPOOL,
                                f"{policy.folder}/converted", shell=shell)
    return notes


@dataclass(frozen=True)
class _Conversion:
    """What staging and converting one file gave: a refusal, a reuse, or a result."""

    refusal: str | None = None
    sha256: str | None = None
    reused: bool = False
    result: ConvertResult = field(default_factory=lambda: ConvertResult("failed", "not converted"))


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
    session_id: str
    policy: DownloadPolicy
    ids: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # id -> this turn's request file
    warnings: list[str] = field(default_factory=list)
    unparsable: dict[str, tuple[int, int]] = field(default_factory=dict)
    checks: list[asyncio.Task[None]] = field(default_factory=list)
    watch: tuple[asyncio.Event, asyncio.Task[None]] | None = None
    convert: bool = False  # the turn has a convert spool (design Part B)
    convert_files: dict[str, str] = field(default_factory=dict)  # id -> this turn's request file
    convert_unparsable: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def spool(self) -> Path:
        return self.temp_dir / DOWNLOADS_DIR / DOWNLOAD_SPOOL

    @property
    def convert_spool(self) -> Path:
        return self.temp_dir / CONVERT_DIR / CONVERT_SPOOL


class _RunBudget:
    def __init__(self, store: DownloadStore, budget_key: str, rid: str, lock: asyncio.Lock) -> None:
        self.store, self.budget_key, self.rid, self.lock = store, budget_key, rid, lock

    async def reserve(self, amount: int) -> int:
        async with self.lock:
            return self.store.reserve(self.budget_key, amount, self.rid)

    async def release(self, amount: int) -> None:
        async with self.lock:
            self.store.release(self.budget_key, amount, self.rid)


class DownloadManager:
    def __init__(self, settings: Settings, provider_commands: dict[str, str], *,
                 fetcher: Fetcher | None = None,
                 model_check: Callable[..., Awaitable[dict[str, tuple[str, str]]]] | None = None,
                 converter: Callable[..., Awaitable[ConvertResult]] | None = None) -> None:
        self.settings = settings
        self.commands = dict(provider_commands)
        self.fetcher = fetcher or Fetcher()
        self.model_check = model_check or run_model_check
        self.converter = converter or convert_file
        # One conversion at a time per server: Docling can use several GB (design).
        self._convert_slot = asyncio.Semaphore(1)
        self._turns: dict[tuple[str, str], DownloadTurn] = {}
        self._budget_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._project_slots: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(PROJECT_CONCURRENCY))
        self._host_slots: dict[tuple[str, str], asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(HOST_CONCURRENCY))
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
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
        turns = [turn for (turn_project, _session), turn in self._turns.items() if turn_project == project_id]
        try:
            records = store.load().requests
        except StorageError:
            LOGGER.warning("download records under %s are unreadable", store.root, exc_info=True)
            for turn in turns:
                turn.warnings.append(UNREADABLE_WARNING)
            self._publish(project_id)
            return
        for turn in turns:
            if not turn.policy.enabled:
                continue  # a convert-only turn has no download status file
            if record is not None and not self._turn_owns(turn, record):
                continue
            try:
                write_turn_status(turn.temp_dir, render_status(
                    [r for r in records if self._turn_owns(turn, r)], turn.files))
            except OSError:
                turn.warnings.append("download status could not be written")
        if record is not None and record.state == "downloaded":
            try:
                write_project_file(project_root, record.folder, "index.md", render_index(
                    [r for r in records if r.folder == record.folder]))
            except (OSError, StorageError):
                LOGGER.warning("could not write the download index", exc_info=True)
        self._publish(project_id)

    @staticmethod
    def _turn_owns(turn: DownloadTurn, record: DownloadRecord | ConversionRecord) -> bool:
        """Auto turns list their run's records; a chat turn lists its agent's chat records."""

        if turn.auto_run:
            return record.auto_run == turn.auto_run
        return not record.auto_run and record.agent == turn.session_id

    # ----- turn lifecycle
    def begin_turn(self, turn: DownloadTurn) -> None:
        self._turns[(turn.project_id, turn.session_id)] = turn
        self._refresh(turn.project_id, turn.project_root, DownloadStore(turn.store_root))
        stop = asyncio.Event()
        turn.watch = (stop, asyncio.create_task(self._watch(turn, stop)))

    async def _watch(self, turn: DownloadTurn, stop: asyncio.Event) -> None:
        try:
            self.harvest(turn, final=False)
            paths = [
                *([turn.spool] if turn.policy.enabled else []),
                *([turn.convert_spool] if turn.convert else []),
            ]
            async for _changes in awatch(*paths, stop_event=stop, step=50, recursive=False, watch_filter=None):
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
        key = (turn.project_id, turn.session_id)
        if self._turns.get(key) is turn:
            del self._turns[key]

    def harvest(self, turn: DownloadTurn, *, final: bool) -> None:
        if turn.policy.enabled:
            self._harvest_downloads(turn, final=final)
        if turn.convert:
            self._harvest_conversions(turn, final=final)

    def _spool_requests(
        self, turn: DownloadTurn, spool: Path, *, kind: str, limit: int,
        parse: Callable[[bytes], T], unparsable: dict[str, tuple[int, int]], final: bool,
    ) -> Iterator[tuple[str, T]]:
        """Each complete request file in `spool`, parsed and removed. Half-written files
        wait for a later pass; refused ones become turn warnings."""

        try:
            entries = scan_spool(spool)
        except (OutboxError, OSError):
            turn.warnings.append(f"{kind} requests unreadable: the spool is not a real directory")
            return
        for entry in entries:
            if not entry.valid:
                if final:
                    self._remove(spool, entry.name)
                continue
            if unparsable.get(entry.name) == entry.signature and not final:
                continue
            try:
                data = read_spool_file(spool, entry.name, limit=limit)
            except (OutboxError, OSError):
                continue
            if data is None:
                continue
            try:
                value = parse(data)
            except RequestError as exc:
                if len(data) <= limit and not final:
                    unparsable[entry.name] = entry.signature  # maybe half-written
                    continue
                turn.warnings.append(f"{kind} request {entry.name} refused: {exc}")
                self._remove(spool, entry.name)
                continue
            self._remove(spool, entry.name)
            yield entry.name, value

    def _harvest_downloads(self, turn: DownloadTurn, *, final: bool) -> None:
        store = DownloadStore(turn.store_root)
        try:
            store.load()
        except StorageError:
            turn.warnings.append(UNREADABLE_WARNING)  # requests stay in the spool
            return
        for name, proposal in self._spool_requests(
            turn, turn.spool, kind="download", limit=REQUEST_FILE_MAX_BYTES, parse=parse_request,
            unparsable=turn.unparsable, final=final,
        ):
            if len(turn.ids) >= TURN_MAX_REQUESTS:
                turn.warnings.append(f"download request {name} refused: over {TURN_MAX_REQUESTS} this turn")
                continue
            try:
                record = store.add(
                    proposal.url, purpose=proposal.purpose, filename=proposal.filename,
                    flags=url_flags(proposal.url), auto_run=turn.auto_run, cycle=turn.cycle,
                    round_n=turn.round_n, agent=turn.agent, agent_name=turn.agent_name,
                    folder=turn.policy.folder, convert=turn.policy.convert,
                )
            except StorageError:
                LOGGER.warning("download request %s could not be recorded", name, exc_info=True)
                turn.warnings.append(f"download request {name} was not recorded: {UNREADABLE_WARNING}")
                return
            turn.ids.append(record.id)
            turn.files[record.id] = name
            if record.state == "checking":
                if turn.policy.model_check:
                    self._queue_check(turn, record.id)
                else:
                    record = self._after_checks(turn.project_id, turn.project_root, store, record.id, turn.policy)
            self._refresh(turn.project_id, turn.project_root, store, record)

    def _remove(self, spool: Path, name: str) -> None:
        try:
            remove_spool_entry(spool, name)
        except (OutboxError, OSError):
            LOGGER.warning("could not remove request file %s", name, exc_info=True)

    def _start(self, coroutine: Coroutine[Any, Any, None]) -> bool:
        """Track a background task; none starts once shutdown began (review round 1:
        a chat reply finalised after the last shutdown step). What was recorded is
        failed by the next start's recovery."""

        if self._closing:
            coroutine.close()
            return False
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    # ----- conversion requests (design Part B)
    def _harvest_conversions(self, turn: DownloadTurn, *, final: bool) -> None:
        conversions = ConversionStore(turn.store_root)
        added = False
        for name, path in self._spool_requests(
            turn, turn.convert_spool, kind="convert", limit=CONVERT_REQUEST_MAX_BYTES,
            parse=parse_convert_request, unparsable=turn.convert_unparsable, final=final,
        ):
            if len(turn.convert_files) >= TURN_MAX_CONVERSIONS:
                turn.warnings.append(f"convert request {name} refused: over {TURN_MAX_CONVERSIONS} this turn")
                continue
            try:
                record = conversions.add(path, request=name, auto_run=turn.auto_run,
                                         round_n=turn.round_n, agent=turn.session_id)
            except StorageError:
                LOGGER.warning("convert request %s could not be recorded", name, exc_info=True)
                turn.warnings.append(f"convert request {name} was not recorded: records unreadable")
                return
            turn.convert_files[record.id] = name
            self._start(self._convert_request(turn, record))
            added = True
        self._write_convert_status(turn)
        if added:
            self._publish(turn.project_id)

    def _write_convert_status(self, turn: DownloadTurn) -> None:
        try:
            records = [r for r in ConversionStore(turn.store_root).load() if self._turn_owns(turn, r)]
            write_turn_status(turn.temp_dir, render_convert_status(records, turn.convert_files),
                              directory=CONVERT_DIR)
        except (OSError, StorageError):
            turn.warnings.append("convert status could not be written")

    async def _run_conversion(
        self, project_root: Path, store_root: Path, path: str,
        reusable: Callable[[str], bool] = lambda _sha256: False,
    ) -> _Conversion:
        """Stage `path` and convert it, all inside the server's one conversion slot
        (review round 1: staging 100 MiB per request outside it multiplied the cost)."""

        scratch = store_root / "downloads" / ".converting"
        async with self._convert_slot:
            scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
            staged = Path(tempfile.mkdtemp(dir=scratch)) / "source"
            try:
                try:
                    source = await asyncio.to_thread(stage_source, project_root, path, staged)
                except ValueError as exc:
                    return _Conversion(refusal=str(exc))
                except (StorageError, OSError):  # StorageError covers every app.storage refusal
                    return _Conversion(refusal=NOT_CONVERTIBLE)
                if reusable(source.sha256):
                    return _Conversion(sha256=source.sha256, reused=True)
                try:
                    result = await self.converter(
                        staged, display_path=path, source_sha256=source.sha256, scratch_root=scratch,
                        models_dir=ready_models_dir(self.settings.home),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("conversion of %s failed unexpectedly", path)
                    result = ConvertResult("failed", "unexpected error")
                return _Conversion(sha256=source.sha256, result=result)
            finally:
                shutil.rmtree(staged.parent, ignore_errors=True)

    async def _convert_request(self, turn: DownloadTurn, record: ConversionRecord) -> None:
        conversions = ConversionStore(turn.store_root)
        output = converted_output(turn.policy.folder, record.path)

        def reusable(sha256: str) -> bool:
            reused = conversions.latest_converted(record.path, sha256)
            return reused is not None and reused.output == output and (turn.project_root / output).is_file()

        try:
            done = await self._run_conversion(turn.project_root, turn.store_root, record.path, reusable)
            if done.refusal is not None:
                conversions.update(record.id, state="refused", reason=done.refusal)
            elif done.reused:
                conversions.update(record.id, state="converted", reason="reused",
                                   source_sha256=done.sha256, output=output)
            elif done.result.state == "unsupported":
                conversions.update(record.id, state="refused", reason=done.result.reason)
            elif done.result.state != "converted" or done.result.markdown is None:
                conversions.update(record.id, state="failed", reason=done.result.reason)
            else:
                folder, _, leaf = output.rpartition("/")
                try:
                    write_project_file(turn.project_root, folder, leaf, done.result.markdown)
                except (OSError, StorageError) as exc:
                    conversions.update(record.id, state="failed", reason=f"could not save the Markdown: {exc}")
                else:
                    conversions.update(record.id, state="converted", reason=done.result.reason,
                                       source_sha256=done.sha256, output=output)
        except StorageError:
            LOGGER.exception("conversion %s could not be recorded", record.id)
        finally:
            # The live turn that owns it, which may be a later one: an ended turn's
            # request names must not overwrite the next turn's status file.
            for live in list(self._turns.values()):
                if live.convert and live.store_root == turn.store_root and self._turn_owns(live, record):
                    self._write_convert_status(live)
            self._publish(turn.project_id)

    # ----- model check batching
    def _queue_check(self, turn: DownloadTurn, rid: str) -> None:
        batch = self._pending.setdefault(turn, [])
        batch.append(rid)
        if len(batch) == 1 and not self._closing:
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
            # Closing: Auto's shutdown waits on this turn, so wait for nothing.
            timeout = 0 if self._closing else SETTLE_SECONDS
            _done, pending = await asyncio.wait(turn.checks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.pop(turn, None)
        store = DownloadStore(turn.store_root)
        try:
            for record in store.load().requests:
                if record.id in turn.ids and record.state == "checking":
                    record = store.update(record.id, state="refused", masked=True,
                                          reason="model check unavailable: timed out")
                    self._refresh(turn.project_id, turn.project_root, store, record)
        except StorageError:
            turn.warnings.append(UNREADABLE_WARNING)

    def turn_awaiting(self, turn: DownloadTurn) -> list[str]:
        try:
            records = DownloadStore(turn.store_root).load().requests
        except StorageError:
            turn.warnings.append(UNREADABLE_WARNING)
            return []
        return [r.id for r in records if r.id in turn.ids and r.state == "awaiting"]

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
        self._start(self._download(project_id, project_root, store, rid))
        return record

    async def _download(self, project_id: str, project_root: Path, store: DownloadStore, rid: str) -> None:
        record = store.get(rid)
        async with self._project_slots[project_id], self._host_slots[(project_id, record.host)]:
            record = store.update(rid, state="downloading")
            self._refresh(project_id, project_root, store, record)
            budget = _RunBudget(store, record.budget_key, rid, self._budget_locks[project_id])
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
        if record.state == "downloaded" and record.convert:
            # Outside the download slots: a long conversion never holds up other downloads.
            record = store.update(rid, convert_state="converting")
            self._refresh(project_id, project_root, store, record)
            record = await self._convert_download(project_root, store, record)
            self._refresh(project_id, project_root, store, record)

    async def _convert_download(
        self, project_root: Path, store: DownloadStore, record: DownloadRecord
    ) -> DownloadRecord:
        """Every finished download goes through the converter: Magika, not the saved
        name, decides whether it is a document (a PDF saved as raw/view still converts)."""

        assert record.path is not None
        done = await self._run_conversion(project_root, store.root, record.path)
        if done.refusal is not None:
            return store.update(record.id, convert_state="failed", convert_reason=done.refusal)
        result = done.result
        if result.state == "unsupported":
            return store.update(record.id, convert_state="skipped", convert_reason=result.reason)
        if result.state != "converted" or result.markdown is None:
            return store.update(record.id, convert_state="failed", convert_reason=result.reason)
        folder, _, leaf = record.path.rpartition("/")
        try:
            write_project_file(project_root, folder, f"{leaf}.md", result.markdown)
        except (OSError, StorageError) as exc:
            return store.update(record.id, convert_state="failed",
                                convert_reason=f"could not save the Markdown: {exc}")
        return store.update(
            record.id, convert_state="converted", convert_reason=result.reason,
            converted_path=f"{record.path}.md",
        )

    def converting(self) -> bool:
        """A conversion holds the server's one slot right now."""

        return self._convert_slot.locked()

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
        for turn in self._turns.values():
            if turn.store_root == store_root:
                return turn.project_id
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
        try:
            ConversionStore(store_root).recover()
        except StorageError:
            LOGGER.exception("conversion records could not be recovered under %s", store_root)

    def close(self) -> None:
        """First half of shutdown: cancel checks and downloads, end settle waits.

        Runs before Auto's shutdown, which waits on turns that may be settling;
        shutdown() afterwards cancels what Auto's last harvest started.
        """

        self._closing = True
        for task in list(self._tasks):
            task.cancel()

    async def shutdown(self) -> None:
        self.close()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
