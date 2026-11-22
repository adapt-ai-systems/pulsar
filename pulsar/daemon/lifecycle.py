"""Pulse lifecycle: start a pulse's runner, stop it, wake it if stopped.

One home for what every spawn site used to repeat: "start this pulse's runner
with its stored settings" (diet, title, effort, fast, memory_max), "stop a
runner" (socket first, systemd unit fallback), "wake a stopped pulse" and the
rule that drops an effort or fast setting the target vessel cannot speak.

The failure cleanup after a failed start is NOT here on purpose. Each caller
(create, restore, reset, fork, switch, handoff) keeps its own write order and
caught exception class; they differ and the differences are observable.

Tests patch ``units_mod.spawn_runner`` and ``sockclient.stop`` on the shared
modules, so this file reaches both through the module attribute. Never
``from ... import spawn_runner``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from pulsar import models as models_mod
from pulsar.daemon import paths, sockclient
from pulsar.launch import units as units_mod
from pulsar.store import Pulse, Store
from pulsar.vessel import FAST_VESSELS, efforts_for

logger = logging.getLogger("pulsar.daemon.lifecycle")

_KEEP: Any = object()    # "use the pulse's stored value"


class StopFailed(Exception):
    """Both the socket stop and the systemd fallback failed."""


# --------------------------------------------------------------------------- #
# Per-pulse lock
# --------------------------------------------------------------------------- #
# Stop, start and wake blocked the event loop, so two of them never overlapped
# on one pulse. Once they run in worker threads they can: two sends could both
# see a stopped target before either marks it running. One non-reentrant lock
# per pulse id serializes them. Worker threads only: taking it on the loop
# thread would block the loop, so that raises. Taking it twice on one thread
# raises too (fail fast, never deadlock). Code holds at most one pulse lock.
_LOCKS: dict[str, threading.Lock] = {}
_OWNERS: dict[str, int] = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def pulse_lock(pulse_id: str) -> Iterator[None]:
    """Hold the pulse's lock. Worker threads only; never re-entered."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            f"pulse_lock({pulse_id}) on a thread with a running event loop")
    me = threading.get_ident()
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(pulse_id, threading.Lock())
        if _OWNERS.get(pulse_id) == me:
            raise RuntimeError(f"re-entry: pulse_lock({pulse_id}) already "
                               "held by this thread")
    lock.acquire()
    with _LOCKS_GUARD:
        _OWNERS[pulse_id] = me
    try:
        yield
    finally:
        with _LOCKS_GUARD:
            _OWNERS.pop(pulse_id, None)
        lock.release()


def spawn_kwargs(store: Store, pulse: Pulse, *,
                 settings: Optional[dict[str, Any]] = None,
                 effort: Any = _KEEP, fast: Any = _KEEP) -> dict[str, Any]:
    """The keyword arguments ``units_mod.spawn_runner`` takes beyond the identity.

    ``settings`` defaults to the pulse's own; ``effort`` and ``fast`` default
    to the stored values and can be overridden (switch, handoff). ``memory_max``
    is present only when set.
    """
    s = pulse.settings if settings is None else settings
    kwargs: dict[str, Any] = {
        "diet": store.diet_with_defaults(s.get("diet")),
        "title": pulse.title,
        "effort": s.get("effort") if effort is _KEEP else effort,
        "fast": bool(s.get("fast")) if fast is _KEEP else fast,
    }
    mem = s.get("memory_max")
    if mem:
        kwargs["memory_max"] = mem
    return kwargs


def start_locked(store: Store, pulse: Pulse, *, resume: Optional[str],
          model: Optional[str] = None, vessel: Optional[str] = None,
          settings: Optional[dict[str, Any]] = None,
          effort: Any = _KEEP, fast: Any = _KEEP,
          memory_max: Optional[str] = None) -> None:
    """Spawn the pulse's runner (blocking: waits on systemd). Caller holds
    the pulse lock.

    ``model`` and ``vessel`` default to the pulse's; ``memory_max`` overrides
    the stored one. Raises whatever ``units_mod.spawn_runner`` raises.
    """
    home = paths.pulse_home(pulse.id)
    home.mkdir(parents=True, exist_ok=True)
    kwargs = spawn_kwargs(store, pulse, settings=settings, effort=effort,
                          fast=fast)
    if memory_max:
        kwargs["memory_max"] = memory_max
    units_mod.spawn_runner(pulse.id, home, model or pulse.model,
                       vessel or pulse.vessel, resume=resume, **kwargs)


def start(store: Store, pulse: Pulse, **kwargs: Any) -> None:
    """:func:`start_locked` under the pulse lock (for callers not holding it)."""
    with pulse_lock(pulse.id):
        start_locked(store, pulse, **kwargs)


async def astart(store: Store, pulse: Pulse, **kwargs: Any) -> None:
    """:func:`start` off the event loop (spawn_runner blocks on systemd)."""
    await asyncio.to_thread(start, store, pulse, **kwargs)


def stop_locked(pulse_id: str) -> str:
    """Stop a running pulse's runner: socket op, systemd fallback on failure.
    Caller holds the pulse lock.

    Returns the path taken (``"socket"`` or ``"unit"``). Raises
    :class:`StopFailed` only if BOTH fail. Blocking.
    """
    started = time.monotonic()
    try:
        sockclient.stop(paths.sock_path(pulse_id), pulse_id)
        logger.info("lifecycle.stop pulse=%s via socket in %.3fs",
                    pulse_id, time.monotonic() - started)
        return "socket"
    except sockclient.SocketError as exc:
        logger.error("lifecycle.stop pulse=%s socket stop failed (%s) — "
                     "systemd fallback", pulse_id, exc)
    if not units_mod.stop_unit(pulse_id):
        logger.error("lifecycle.stop pulse=%s systemd fallback FAILED too in "
                     "%.3fs", pulse_id, time.monotonic() - started)
        raise StopFailed(f"could not stop runner for {pulse_id}")
    logger.info("lifecycle.stop pulse=%s via systemd fallback in %.3fs",
                pulse_id, time.monotonic() - started)
    return "unit"


def stop(pulse_id: str) -> str:
    """:func:`stop_locked` under the pulse lock (for callers not holding it)."""
    with pulse_lock(pulse_id):
        return stop_locked(pulse_id)


async def astop(pulse_id: str) -> str:
    """:func:`stop` off the event loop."""
    return await asyncio.to_thread(stop, pulse_id)


def retire_stopped_in_flight(pulse_id: str, home: Path) -> None:
    """Retire the messages a user stop cut off, so restore doesn't replay
    them. Only the names pause recorded; anything else in feeding/
    is a crash leftover and keeps its replay."""
    marker = home / "stopped_in_flight.json"
    if not marker.exists():
        return
    try:
        names = json.loads(marker.read_text())
    except (OSError, ValueError):
        names = []
    acked = home / "inbox" / "acked"
    acked.mkdir(parents=True, exist_ok=True)
    for name in names if isinstance(names, list) else []:
        src = home / "inbox" / "feeding" / str(name)
        if src.name != name or not src.exists():
            continue
        try:
            src.replace(acked / src.name)
            logger.info("restore pulse=%s retired stopped message %s",
                        pulse_id, src.name)
        except OSError as exc:
            logger.warning("restore pulse=%s could not retire %s: %s",
                           pulse_id, src.name, exc)
    marker.unlink(missing_ok=True)


def wake_locked(store: Store, pulse_id: str) -> bool:
    """Respawn a stopped pulse's runner exactly like the restore route.

    Caller holds the pulse lock. Re-reads the pulse, so a wake that lost the
    race to another wake does nothing. Resume id from the last heartbeat,
    memory_max from settings. Order: spawn, ``stop_reason`` cleared, state
    ``running``. Raises on spawn failure (the caller logs and does not retry
    until next due). Returns True when it spawned, False when the pulse was
    not ``stopped``.
    """
    pulse = store.get_pulse(pulse_id)
    if pulse.state != "stopped":
        return False
    beat = paths.read_heartbeat(pulse.id)
    resume = beat.get("cli_session_id") if beat else None
    # A turn the user stopped must not replay on a schedule wake.
    retire_stopped_in_flight(pulse.id, paths.pulse_home(pulse.id))
    logger.info("lifecycle.wake pulse=%s resume=%s — respawning",
                pulse.id, resume or "-")
    start_locked(store, pulse, resume=resume)
    # Clear any stale stop_reason now it's live again (mirrors restore route).
    store.update_settings(pulse.id, {"stop_reason": None})
    store.set_state(pulse.id, "running")
    return True


def wake_if_stopped(store: Store, pulse: Pulse) -> None:
    """:func:`wake_locked` under the pulse lock. A pulse not ``stopped`` is
    left untouched. For callers that hold no pulse lock (scheduler, watcher,
    condwatch)."""
    with pulse_lock(pulse.id):
        wake_locked(store, pulse.id)


def settings_for_target(store: Store, pulse: Pulse, model: str,
                        vessel: str) -> tuple[Optional[str], bool]:
    """The ``(effort, fast)`` to spawn ``pulse`` with on ``model``/``vessel``.

    Reasoning effort and fast mode follow the pulse across a handoff, but a
    cross-vessel switch may land on a CLI that does not speak the stored value
    (claude "max" -> codex; fast on a CLI with no service-tier switch). An
    invalid value is dropped loudly AND cleared in the store.
    """
    pid = pulse.id
    effort = pulse.settings.get("effort")
    # A slot pulse's effort dial follows the model CURRENTLY served behind
    # its endpoint (table by served-id prefix), not the pi vessel (no dial).
    slot_entry = models_mod.model_by_id(model)
    if slot_entry is not None and slot_entry.slot:
        served = ((pulse.settings.get("diet") or {}).get("slot") or {}
                  ).get("served_id")
        levels = models_mod.slot_efforts(served)
        canon = models_mod.canonical_effort(model, effort, served or "")
    else:
        levels = efforts_for(model, vessel)
        canon = models_mod.canonical_effort(model, effort)
    # A retired name (DS41 "low" -> "50") is migrated, not dropped.
    if canon != effort and canon in levels:
        logger.info("handoff.effort pulse=%s %r -> %r (retired name)",
                    pid, effort, canon)
        store.update_settings(pid, {"effort": canon})
        effort = canon
    if effort and effort not in levels:
        logger.warning("handoff.effort pulse=%s %r not valid for %s/%s "
                       "— dropped", pid, effort, model, vessel)
        store.update_settings(pid, {"effort": None})
        effort = None
    fast = bool(pulse.settings.get("fast"))
    if fast and vessel not in FAST_VESSELS:
        logger.warning("handoff.fast pulse=%s not valid for vessel=%s "
                       "— dropped", pid, vessel)
        store.update_settings(pid, {"fast": None})
        fast = False
    return effort, fast
