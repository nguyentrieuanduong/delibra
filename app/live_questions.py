"""Questions an Auto agent writes during its turn, saved as they land.

Each turn has one LiveQuestions. Its harvests run one at a time under the
project lock: from the spool watcher mid-turn, and once more at turn end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
import logging
from pathlib import Path

from watchfiles import awatch

from app.questions import (
    SPOOL_FILE_MAX_BYTES,
    TURN_MAX_QUESTIONS,
    OutboxError,
    Question,
    QuestionStore,
    QuestionsViewWriteError,
    SpoolEntry,
    parse_outbox,
    read_spool_file,
    remove_spool_entry,
    scan_spool,
)
from app.storage import StorageError


LOGGER = logging.getLogger(__name__)
LIVE_UNAVAILABLE = "live questions unavailable; questions arrive at turn end"
STORE_UNAVAILABLE = "questions store unavailable; some questions were not saved"
VIEW_STALE = "questions saved, but the agent view could not be updated"
# Files read per mid-turn harvest; the next event or the turn end reads the rest.
SCAN_READS = 64
WATCH_STEP_MS = 50
# A file written between the first harvest and the watch's registration
# raises no event; one rescan this long after the start finds it.
REGISTRATION_RESCAN_SECONDS = 1.0


@dataclass
class LiveQuestions:
    store: QuestionStore
    spool_dir: Path
    asked_by: str
    asked_by_name: str
    auto_run: int
    cycle: int | None
    round_n: int
    asked: list[Question] = field(default_factory=list)
    _warnings: dict[str, None] = field(default_factory=dict)
    # (file name, sha256 of its bytes) already saved: a file whose removal
    # failed is never saved twice, and a reused name with new content is.
    _consumed: set[tuple[str, str]] = field(default_factory=set)
    # Unparsable files by name, with the signature they had when read.
    _unparsed: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def warn(self, text: str) -> None:
        self._warnings.setdefault(text, None)

    def harvest(self, *, final: bool) -> list[Question]:
        """Save every complete question file; never raises.

        Mid-turn, an unparsable file may still be being written, so it
        waits until it changes. At turn end every entry is settled.
        """

        try:
            entries = scan_spool(self.spool_dir)
        except (OutboxError, OSError) as exc:
            self.warn(f"questions outbox unreadable: {exc}")
            return []
        added: list[Question] = []
        reads = 0
        for entry in entries:
            if not entry.valid:
                if final:
                    self._remove(entry.name)
                continue
            if not final:
                if self._unparsed.get(entry.name) == entry.signature:
                    continue
                if reads == SCAN_READS:
                    break
            reads += 1
            saved = self._take(entry, final=final)
            if saved is None:
                break
            added.extend(saved)
        return added

    def mark_outcome(self, outcome: str) -> None:
        """Label this turn's saved questions with how the turn ended."""

        if not self.asked:
            return
        try:
            self.store.set_turn_outcome([question.id for question in self.asked], outcome)
        except StorageError:
            LOGGER.exception("could not label questions from a %s turn", outcome)

    def _take(self, entry: SpoolEntry, *, final: bool) -> list[Question] | None:
        """Save one file's questions; None when the history cannot be written."""

        try:
            data = read_spool_file(self.spool_dir, entry.name)
        except (OutboxError, OSError) as exc:
            self._reject(entry.name, str(exc))
            return []
        if data is None:
            return []
        consumed = (entry.name, sha256(data).hexdigest())
        if consumed in self._consumed:
            self._remove(entry.name)
            return []
        if len(data) > SPOOL_FILE_MAX_BYTES:
            self._reject(entry.name, f"larger than {SPOOL_FILE_MAX_BYTES} bytes")
            return []
        try:
            proposals = parse_outbox(data)
        except OutboxError as exc:
            if final:
                self._reject(entry.name, str(exc))
            else:
                self._unparsed[entry.name] = entry.signature
            return []
        if len(self.asked) + len(proposals) > TURN_MAX_QUESTIONS:
            self._reject(entry.name, f"the turn would pass {TURN_MAX_QUESTIONS} questions")
            return []
        try:
            saved = self.store.add(
                proposals,
                asked_by=self.asked_by,
                asked_by_name=self.asked_by_name,
                auto_run=self.auto_run,
                cycle=self.cycle,
                round_n=self.round_n,
            )
        except QuestionsViewWriteError as exc:
            LOGGER.warning("questions saved but their view was not", exc_info=True)
            saved = list(exc.saved)
            self.warn(VIEW_STALE)
        except StorageError:
            LOGGER.exception("could not save questions from %s", entry.name)
            self.warn(STORE_UNAVAILABLE)
            return None
        # History first, then the guard, then the file (design: Handoff order).
        self._consumed.add(consumed)
        self._unparsed.pop(entry.name, None)
        self.asked.extend(saved)
        self._remove(entry.name)
        return saved

    def _reject(self, name: str, reason: str) -> None:
        self.warn(f"question file {name} ignored: {reason}")
        self._remove(name)

    def _remove(self, name: str) -> None:
        try:
            remove_spool_entry(self.spool_dir, name)
        except (OutboxError, OSError):
            LOGGER.warning("could not remove question file %s", name, exc_info=True)


async def watch_spool(
    live: LiveQuestions,
    harvest: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    """Harvest now, once more after registration, and on every spool change.

    Events only say "look now": each harvest rescans the whole directory,
    so a coalesced or missed event loses nothing the turn end won't find.
    """

    async def rescan() -> None:
        await asyncio.sleep(REGISTRATION_RESCAN_SECONDS)
        await harvest()

    await harvest()
    late = asyncio.create_task(rescan())
    try:
        async for _changes in awatch(
            live.spool_dir,
            stop_event=stop,
            step=WATCH_STEP_MS,
            recursive=False,
            watch_filter=None,
        ):
            await harvest()
    except Exception:
        LOGGER.warning("live questions watch failed for %s", live.spool_dir, exc_info=True)
        live.warn(LIVE_UNAVAILABLE)
    finally:
        late.cancel()
        await asyncio.gather(late, return_exceptions=True)
