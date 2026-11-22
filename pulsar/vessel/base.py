"""Shared vessel plumbing — the pieces every CLI adapter used to re-implement.

Each vessel (claude, codex, pi, grok) wraps a
different CLI, but they all parse the same loose JSON, truncate the same tool
output, and kill the same kind of child process group. Those helpers live
here ONCE, with one set of semantics. The protocol itself (spawn / send /
subscribe / stop / cli_session_id, optional inject / interrupt) is documented
on ``BaseVessel``; a vessel subclasses it and declares its capabilities as
class attributes, which the runner bridge reads instead of matching class
names.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from pulsar.vessel.events import VesselEvent

log = logging.getLogger("pulsar.vessel")

TRUNCATED_MARK = "\n… (truncated)"


def as_int(v: Any) -> int:
    """A non-negative int from loose JSON; bools and junk count as 0."""
    if isinstance(v, bool) or not isinstance(v, int):
        return 0
    return v if v > 0 else 0


def is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


def snippet(text: Any, limit: int) -> str:
    """Tool output trimmed for events.jsonl: stripped, cut at ``limit`` with
    a visible marker. Non-strings are stringified; None/empty → ""."""
    if not text:
        return ""
    t = text if isinstance(text, str) else str(text)
    t = t.strip()
    if len(t) > limit:
        return t[:limit] + TRUNCATED_MARK
    return t


def random_session_uuid() -> str:
    """A fresh uuid4 for CLI ``--session-id`` flags (the qwen-code CLI checks
    the version nibble, so a fixed placeholder is rejected)."""
    return str(uuid.uuid4())


def safe_killpg(pid: int | None, sig: int) -> None:
    """Signal a process GROUP we own — never a recycled or foreign pid.

    Children spawn with ``start_new_session=True`` so a live child is its own
    group leader (``getpgid(pid) == pid``). Refuse to killpg anything that is
    ``pid <= 1`` or not its own group leader — on a high-PID-churn host a stale
    pid can recycle onto an unrelated live group and an unguarded killpg would
    SIGKILL it (a prior post-mortem made this guard mandatory).
    """
    if not pid or pid <= 1:
        return
    try:
        if os.getpgid(pid) != pid:
            return
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("vessel_killpg_failed pid=%s error=%s", pid, e)


async def drain_until_exited(queue: asyncio.Queue) -> AsyncIterator[VesselEvent]:
    """Yield every event from ``queue`` up to and including the terminal
    ``done`` whose reason is ``exited``, then stop."""
    while True:
        event = await queue.get()
        yield event
        if event.kind == "done" and event.payload.get("reason") == "exited":
            return


async def terminate_group(
    proc: asyncio.subprocess.Process, *, sid: str,
    term_timeout: float = 5.0, kill_timeout: float | None = 2.0,
    signal_fn: Callable[[int | None, int], None] = safe_killpg,
    log: logging.Logger | None = log,
) -> None:
    """SIGTERM the child's process group, wait ``term_timeout``, then SIGKILL.

    ``kill_timeout`` bounds the wait after SIGKILL; ``None`` waits without a
    cap. ``signal_fn`` lets a vessel keep its own pid bookkeeping (pi). Logs
    ``vessel_stop`` / ``vessel_stop_sigterm_timeout`` /
    ``vessel_stop_sigkill_timeout``; ``log=None`` signals silently. No-op when
    the process has already exited.
    """
    if proc.returncode is not None:
        return
    if log is not None:
        log.info("vessel_stop sid=%s pid=%s", sid, proc.pid)
    signal_fn(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=term_timeout)
    except (asyncio.TimeoutError, TimeoutError):
        if log is not None:
            log.warning("vessel_stop_sigterm_timeout sid=%s escalating", sid)
        signal_fn(proc.pid, signal.SIGKILL)
        if kill_timeout is None:
            await proc.wait()
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            if log is not None:
                log.error("vessel_stop_sigkill_timeout sid=%s", sid)


async def log_stderr(stream: asyncio.StreamReader | None, sid: str, *,
                     level: int = logging.WARNING) -> None:
    """Drain a child's stderr, logging each non-empty line as ``vessel_stderr``."""
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.log(level, "vessel_stderr sid=%s line=%s", sid, text)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("vessel_stderr_pump_error sid=%s error=%s", sid, e)


def cancel_task(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()


class BaseVessel:
    """The vessel contract.

    Required::

        async spawn(*, cwd, model=None, resume_cli_session_id=None, ...) -> sid
        async send(sid, text)            # start a turn
        subscribe(sid) -> AsyncIterator[VesselEvent]   # until done{exited}
        async stop(sid)
        cli_session_id(sid) -> str | None

    Optional, advertised by the class attributes below so the runner bridge
    never has to match class names::

        async inject(sid, text)     # steer the LIVE turn      (supports_inject)
        async interrupt(sid)        # abort the live turn      (supports_interrupt)

    ``spawn`` keyword acceptance is likewise declared (``takes_diet`` etc.) so
    the bridge passes only what the adapter understands.
    """

    supports_inject: bool = False
    supports_interrupt: bool = False
    takes_diet: bool = False

    # Shared process-group kill, as the staticmethod every adapter had.
    _safe_killpg = staticmethod(safe_killpg)

    def pid_of(self, sid: str) -> int | None:
        """The live child's pid for ``sid``, when the adapter keeps one in
        ``self._sessions[sid].process``. (Named ``pid_of``: several adapters
        already set a ``self.pid`` attribute, which would shadow a ``pid``
        method.)"""
        sessions = getattr(self, "_sessions", None)
        sess = sessions.get(sid) if isinstance(sessions, dict) else None
        proc = getattr(sess, "process", None)
        return getattr(proc, "pid", None) or None
