import asyncio

import pytest

from app.conversion_queue import ConversionCancelled, ConversionQueue


async def _hold(queue: ConversionQueue, job, started: asyncio.Event, release: asyncio.Event) -> str:
    async with queue.turn(job):
        started.set()
        await release.wait()
    return "done"


@pytest.mark.asyncio
async def test_jobs_wait_in_order_and_show_their_position() -> None:
    queue = ConversionQueue(asyncio.Semaphore(1))
    first, second, third = (queue.enqueue(("root", f"C{n}"), "p1") for n in (1, 2, 3))
    release = asyncio.Event()
    started = [asyncio.Event() for _ in range(3)]
    tasks = [asyncio.create_task(_hold(queue, job, event, release))
             for job, event in zip((first, second, third), started)]
    await started[0].wait()

    view = queue.snapshot()
    assert view[("root", "C1")].position is None and view[("root", "C1")].started_at is not None
    assert (view[("root", "C2")].position, view[("root", "C3")].position) == (1, 2)
    assert view[("root", "C2")].started_at is None

    release.set()
    assert await asyncio.gather(*tasks) == ["done"] * 3
    assert queue.snapshot() == {}


@pytest.mark.asyncio
async def test_cancelling_a_waiting_job_ends_it_without_the_slot() -> None:
    queue = ConversionQueue(asyncio.Semaphore(1))
    running, waiting = queue.enqueue(("root", "C1"), "p1"), queue.enqueue(("root", "C2"), "p1")
    release, started = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(_hold(queue, running, started, release))
    second = asyncio.create_task(_hold(queue, waiting, asyncio.Event(), release))
    await started.wait()
    await asyncio.sleep(0)  # the second task now waits for the slot

    assert queue.cancel(("root", "C2")) is waiting
    with pytest.raises(ConversionCancelled):
        await second
    assert ("root", "C2") not in queue.snapshot()
    release.set()
    assert await first == "done"


@pytest.mark.asyncio
async def test_a_job_cancelled_before_its_task_ran_ends_on_entry() -> None:
    queue = ConversionQueue(asyncio.Semaphore(1))
    job = queue.enqueue(("root", "C1"), "p1")
    assert queue.cancel(("root", "C1")) is job
    with pytest.raises(ConversionCancelled):
        await _hold(queue, job, asyncio.Event(), asyncio.Event())
    assert queue.snapshot() == {}


@pytest.mark.asyncio
async def test_cancelling_the_running_job_stops_its_work_and_frees_the_slot() -> None:
    slot = asyncio.Semaphore(1)
    queue = ConversionQueue(slot)
    job = queue.enqueue(("root", "D4"), "p1")
    started = asyncio.Event()
    task = asyncio.create_task(_hold(queue, job, started, asyncio.Event()))
    await started.wait()

    queue.cancel(("root", "D4"))
    with pytest.raises(ConversionCancelled):
        await task
    assert not slot.locked()
    assert queue.cancel(("root", "D4")) is None  # gone: nothing left to cancel


@pytest.mark.asyncio
async def test_a_shutdown_cancel_stays_a_cancellation() -> None:
    queue = ConversionQueue(asyncio.Semaphore(1))
    job = queue.enqueue(("root", "C1"), "p1")
    started = asyncio.Event()
    task = asyncio.create_task(_hold(queue, job, started, asyncio.Event()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.snapshot() == {}


@pytest.mark.asyncio
async def test_a_user_cancel_during_shutdown_stays_a_cancellation() -> None:
    queue = ConversionQueue(asyncio.Semaphore(1))
    job = queue.enqueue(("root", "C1"), "p1")
    started = asyncio.Event()
    task = asyncio.create_task(_hold(queue, job, started, asyncio.Event()))
    await started.wait()
    task.cancel()  # shutdown
    queue.cancel(("root", "C1"))  # and the user, in the same loop pass
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not isinstance(caught.value, ConversionCancelled)


@pytest.mark.asyncio
async def test_every_change_and_the_limit_raise_the_revision() -> None:
    changes = []
    queue = ConversionQueue(asyncio.Semaphore(1), on_change=lambda: changes.append(queue.revision))
    job = queue.enqueue(("root", "C1"), "p1")
    started = asyncio.Event()
    task = asyncio.create_task(_hold(queue, job, started, asyncio.Event()))
    await started.wait()
    queue.set_limit(job, 486.0)
    assert queue.snapshot()[("root", "C1")].limit == 486.0
    queue.cancel(("root", "C1"))
    with pytest.raises(ConversionCancelled):
        await task
    assert changes == sorted(changes) and len(changes) >= 4 and len(set(changes)) == len(changes)
