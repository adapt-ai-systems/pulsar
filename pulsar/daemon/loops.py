"""One background-loop runner for the daemon's lifespan tasks.

``run_every`` is the shape the scheduler, handoff and loop-guard
monitors each used to copy: run ``tick``, sleep the interval (waking early when
``stop_event`` is set), and never let a raised tick kill the loop.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("pulsar.daemon.loops")


async def run_every(name: str, tick: Callable[[], Any], seconds: float, *,
                    stop_event: Optional[asyncio.Event] = None,
                    in_thread: bool = False) -> None:
    """Run ``tick()`` every ``seconds`` until cancelled or ``stop_event`` is set.

    ``tick`` takes no arguments (bind the store with a lambda or
    ``functools.partial``) and may be sync or async. A raised tick never kills
    the loop — it is logged and the next tick runs (standing rule: no silent
    death). Cancellation exits cleanly with a log line.

    ``in_thread=True`` runs a sync ``tick`` off the event loop, so a slow tick
    (blocking subprocess calls, sleeps) cannot freeze /healthz or the other
    loops. The worker is shielded: on cancel the loop waits for it to finish
    before re-raising, so the shared DB is never closed under a running thread.
    """
    logger.info("%s.loop starting tick_seconds=%.1f", name, seconds)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("%s.loop stop_event set — exiting", name)
                return
            try:
                if in_thread:
                    job = asyncio.ensure_future(asyncio.to_thread(tick))
                    try:
                        await asyncio.shield(job)
                    except asyncio.CancelledError:
                        try:
                            await job  # do not return while the worker still runs
                        except Exception:
                            logger.exception("%s.loop tick RAISED during cancel", name)
                        raise
                else:
                    result = tick()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.exception("%s.loop tick RAISED — continuing", name)
            try:
                if stop_event is not None:
                    # Wake early if asked to stop; otherwise sleep the interval.
                    await asyncio.wait_for(stop_event.wait(), timeout=seconds)
                    logger.info("%s.loop stop_event set — exiting", name)
                    return
                await asyncio.sleep(seconds)
            except asyncio.TimeoutError:
                continue  # normal interval elapsed
    except asyncio.CancelledError:
        logger.info("%s.loop cancelled — exiting", name)
        raise
