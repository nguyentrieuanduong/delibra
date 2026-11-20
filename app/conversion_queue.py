"""The server's one conversion slot as a visible queue (design 2026-11-20).

Docling can use several GB, so conversions run one at a time. Each one is a job
from the moment it is requested: waiting jobs have a position, the running one
a start time and, once the file type is known, a time limit. The user can
cancel a job while it waits or runs; a shutdown's cancellation stays a plain
CancelledError, so its records are left for the next start's recovery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import secrets
import time

JobKey = tuple[str, str]  # (the project's .delibra root, the record id: C… or D…)


class ConversionCancelled(Exception):
    """The user cancelled this conversion."""


@dataclass(eq=False)
class QueueJob:
    key: JobKey
    project_id: str
    task: asyncio.Task[object] | None = None
    cancelled: bool = False
    started_at: float | None = None  # epoch seconds once it holds the slot
    limit: float | None = None  # seconds, once the converter knows the file type
    waiting_since: int | None = None  # its place at the slot, once its task asks for it
    settled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class JobView:
    position: int | None  # 1 is next; None while running
    started_at: float | None
    limit: float | None
    cancelling: bool = False  # cancelled; its worker has not ended yet


class ConversionQueue:
    def __init__(self, slot: asyncio.Semaphore, on_change: Callable[[], None] | None = None) -> None:
        self._slot = slot
        self._jobs: dict[JobKey, QueueJob] = {}  # insertion order is the waiting order
        self._on_change = on_change
        self.revision = 0
        self._arrivals = 0
        # Revisions restart with the server: a new epoch tells the page so.
        self.epoch = secrets.token_hex(4)

    def _changed(self) -> None:
        self.revision += 1
        if self._on_change is not None:
            self._on_change()

    def enqueue(self, key: JobKey, project_id: str) -> QueueJob:
        """Register a job now, before its task runs, so a Cancel can never miss it."""

        if key in self._jobs:
            raise ValueError(f"conversion {key[1]} is already queued")
        job = QueueJob(key, project_id)
        self._jobs[key] = job
        self._changed()
        return job

    def discard(self, job: QueueJob) -> None:
        """Forget a job whose task never started (the server is shutting down)."""

        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
            self._changed()

    @asynccontextmanager
    async def turn(self, job: QueueJob) -> AsyncIterator[None]:
        """Hold the slot for `job`; raises ConversionCancelled when the user cancels it."""

        try:
            if job.cancelled:
                raise ConversionCancelled(job.key[1])
            job.task = asyncio.current_task()
            # The Semaphore serves waiters in the order they ask: positions follow it.
            self._arrivals += 1
            job.waiting_since = self._arrivals
            try:
                async with self._slot:  # FIFO: CPython's Semaphore wakes waiters in order
                    job.started_at = time.time()
                    self._changed()
                    yield
            except asyncio.CancelledError:
                task = asyncio.current_task()
                # Only the user's cancel becomes ConversionCancelled; one more
                # pending request (shutdown) keeps the task cancelled.
                if job.cancelled and task is not None and task.uncancel() == 0:
                    raise ConversionCancelled(job.key[1]) from None
                raise
        finally:
            self.discard(job)

    @asynccontextmanager
    async def outside_use(self) -> AsyncIterator[None]:
        """Hold the slot for something other than a conversion (a converter models
        action); the panel is told, so it can say why queued conversions wait."""

        async with self._slot:
            self._changed()
            try:
                yield
            finally:
                self._changed()

    def cancel(self, key: JobKey) -> QueueJob | None:
        """Cancel a waiting or running job; None when there is no such job."""

        job = self._jobs.get(key)
        if job is None or job.cancelled:
            return job
        job.cancelled = True
        if job.task is not None:
            job.task.cancel()  # the worker's process group is killed on the way out
        self._changed()
        return job

    def set_limit(self, job: QueueJob, seconds: float) -> None:
        job.limit = seconds
        self._changed()

    def snapshot(self) -> dict[JobKey, JobView]:
        views: dict[JobKey, JobView] = {}
        for key, job in self._jobs.items():
            if job.started_at is not None:
                views[key] = JobView(None, job.started_at, job.limit, cancelling=job.cancelled)
        # Waiting at the slot first, in the order they asked; jobs whose task has
        # not asked yet come after them.
        waiting = sorted(
            (job for job in self._jobs.values() if job.started_at is None and not job.cancelled),
            key=lambda job: (job.waiting_since is None, job.waiting_since or 0),
        )
        for position, job in enumerate(waiting, start=1):
            views[job.key] = JobView(position, None, None)
        return views

    def slot_busy_elsewhere(self) -> bool:
        """Something other than a conversion holds the slot (a converter models action)."""

        return self._slot.locked() and not any(job.started_at is not None for job in self._jobs.values())
