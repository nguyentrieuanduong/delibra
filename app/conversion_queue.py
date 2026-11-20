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
    settled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class JobView:
    position: int | None  # 1 is next; None while running
    started_at: float | None
    limit: float | None


class ConversionQueue:
    def __init__(self, slot: asyncio.Semaphore, on_change: Callable[[], None] | None = None) -> None:
        self._slot = slot
        self._jobs: dict[JobKey, QueueJob] = {}  # insertion order is the waiting order
        self._on_change = on_change
        self.revision = 0
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
        position = 0
        for key, job in self._jobs.items():
            if job.cancelled:
                continue
            if job.started_at is not None:
                views[key] = JobView(None, job.started_at, job.limit)
            else:
                position += 1
                views[key] = JobView(position, None, None)
        return views
