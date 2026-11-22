"""loops.run_every — the one background-loop runner behind the lifespan tasks.

    venv/bin/python -m pytest tests/test_loops.py -q
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from pulsar.daemon import loops

pytestmark = pytest.mark.anyio


async def _until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "condition never became true"
        await asyncio.sleep(0.005)


async def test_raising_tick_keeps_loop_alive(caplog):
    calls = []

    def tick():
        calls.append(1)
        raise RuntimeError("boom")

    task = asyncio.create_task(loops.run_every("t", tick, 0.01))
    await _until(lambda: len(calls) >= 3)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "t.loop tick RAISED — continuing" in caplog.text


async def test_async_tick_is_awaited():
    calls = []

    async def tick():
        await asyncio.sleep(0)
        calls.append(1)

    task = asyncio.create_task(loops.run_every("t", tick, 0.01))
    await _until(lambda: len(calls) >= 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stop_event_exits_during_sleep():
    stop = asyncio.Event()
    calls = []
    task = asyncio.create_task(
        loops.run_every("t", lambda: calls.append(1), 30.0, stop_event=stop))
    await _until(lambda: calls)
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert len(calls) == 1


async def test_stop_event_already_set_never_ticks():
    stop = asyncio.Event()
    stop.set()
    calls = []
    await asyncio.wait_for(
        loops.run_every("t", lambda: calls.append(1), 30.0, stop_event=stop), 1.0)
    assert calls == []


async def test_cancel_exits_with_cancelled_error():
    task = asyncio.create_task(loops.run_every("t", lambda: None, 30.0))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_in_thread_runs_off_the_event_loop():
    main = threading.get_ident()
    seen = []

    def tick():
        seen.append(threading.get_ident())

    task = asyncio.create_task(loops.run_every("t", tick, 0.01, in_thread=True))
    await _until(lambda: seen)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen and all(t != main for t in seen)


async def test_in_thread_cancel_waits_for_worker():
    started = threading.Event()
    release = threading.Event()
    finished = []

    def tick():
        started.set()
        release.wait(5)
        finished.append(1)

    task = asyncio.create_task(loops.run_every("t", tick, 30.0, in_thread=True))
    await _until(started.is_set)
    task.cancel()
    await asyncio.sleep(0.1)
    assert not task.done()       # still waiting on the blocked worker
    assert finished == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    assert finished == [1]       # the worker finished before run_every returned


async def test_in_thread_raising_tick_keeps_loop_alive():
    calls = []

    def tick():
        calls.append(1)
        raise RuntimeError("boom")

    task = asyncio.create_task(loops.run_every("t", tick, 0.01, in_thread=True))
    await _until(lambda: len(calls) >= 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
