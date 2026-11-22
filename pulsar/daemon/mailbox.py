"""One mailbox writer for ``inbox/new/*.json`` with explicit delivery policies.

Every daemon path that queues a message for a pulse writes through this module:
pane messages, pulse-to-pulse sends, question answers/dismissals, Inbox replies,
scheduled prompts, delegation watches, condition watches, handoff requests/seeds/
warnings, and loopguard steers.

Contract (must stay byte-compatible with the runner):
  - Filenames: ``<ts>-<8hex>.json`` or ``<prefix>-<ts>-<8hex>.json`` where
    ``<ts>`` is ``%Y%m%dT%H%M%S%fZ``.
  - Atomic write: write ``<name>.tmp`` in ``inbox/new/`` then rename to
    ``<name>``.
  - JSON payload: ``{"text": ..., "queued_at": <ISO seconds>}``, plus
    ``"source": ...`` when ``source`` is not ``None``, plus any non-empty/non-None
    entries in ``extra`` (``display_text``, ``attachments``, ``reply_to`` and the
    origin keys below).
  - Origin keys (all optional, additive; build them with :func:`origin_keys`):
      ``origin``       ``"user"`` (the user typed it), ``"peer"`` (another pulse sent
                       it) or ``"daemon"`` (schedule, watch, condwatch, handoff,
                       loopguard).
      ``via``          the channel: ``pane``, ``inbox_reply``, ``question``,
                       ``pulse``, ``first_message``, ``schedule``, ``watch``,
                       ``condwatch``, ``handoff``, ``loopguard``.
      ``from_pulse_id``, ``from_title``   only when ``origin == "peer"``: the
                       sender's id and title at send time.
    Readers must fall back to today's text/filename rules when a key is missing
    (old files and old events do not have them). The ``[message from ...]`` text
    prefix stays.
  - Supersede: when ``supersede=True``, unlink every ``f"{prefix}-*.json"`` in
    ``inbox/new/`` before writing. Because the glob is ``f"{prefix}-*.json"``,
    a ``"handoff"`` write also unlinks queued ``handoff-seed-*`` and
    ``handoff-warn-*`` files.
  - Delivery order inside :func:`deliver`:
      1. archived check (``"skip"`` returns ``None``, ``"raise"`` raises
         :class:`Archived`)
      2. ``new_name(prefix)``
      3. ``watch_sleep.remember(store, pulse, name)`` (when ``remember=True``)
      4. ``store.unarchive_pulse(pulse.id)`` + ``store.update_settings(pulse.id,
         {"mode": "watching"})`` (when ``on_archived="unarchive"`` and the pulse
         is archived) — MUST run after ``remember`` so the saved resting state
         is ``"archived"``, not ``"stopped"``.
      5. wake then write (default), or write then wake (``write_first=True``).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pulsar.daemon import lifecycle, paths, watch_sleep
from pulsar.daemon.timeutil import now_iso
from pulsar.fsutil import atomic_write_text
from pulsar.store import Pulse, Store

logger = logging.getLogger("pulsar.daemon.mailbox")

USER = ""
SCHED = "sched"
WATCH = "watch"
CONDWATCH = "condwatch"
HANDOFF = "handoff"
HANDOFF_SEED = "handoff-seed"
HANDOFF_WARN = "handoff-warn"
LOOPGUARD = "loopguard"

OnArchived = Literal["raise", "unarchive", "skip"]


class Archived(Exception):
    """Target pulse is archived and caller requested ``on_archived='raise'``."""


def new_name(prefix: str = USER) -> str:
    """Mint a chronological ``inbox/new/`` filename for ``prefix``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{ts}-{uuid.uuid4().hex[:8]}.json"
    return f"{prefix}-{stem}" if prefix else stem


def origin_keys(origin: str, via: str, *, from_pulse_id: Optional[str] = None,
                from_title: Optional[str] = None) -> dict[str, Any]:
    """The origin keys for ``extra=`` (see the module docstring)."""
    keys: dict[str, Any] = {"origin": origin, "via": via}
    if origin == "peer":
        keys["from_pulse_id"] = from_pulse_id
        keys["from_title"] = from_title
    return keys


def write(pulse_id: str, text: str, *,
          prefix: str = USER,
          source: Optional[str] = None,
          extra: Optional[dict[str, Any]] = None,
          supersede: bool = False,
          name: Optional[str] = None) -> str:
    """Atomically write one message JSON into ``inbox/new/`` and return its filename."""
    new_dir = paths.inbox_new(pulse_id)
    new_dir.mkdir(parents=True, exist_ok=True)
    if supersede and prefix:
        for old in new_dir.glob(f"{prefix}-*.json"):
            try:
                old.unlink()
                logger.info("mailbox.write pulse=%s superseded %s",
                            pulse_id, old.name)
            except OSError:
                pass
    fname = name or new_name(prefix)
    final = new_dir / fname
    body: dict[str, Any] = {
        "text": text,
        "queued_at": now_iso(),
    }
    if source is not None:
        body["source"] = source
    if extra:
        for k, v in extra.items():
            if k == "display_text":
                if v is not None:
                    body[k] = v
            elif v:
                body[k] = v
    payload = json.dumps(body)
    atomic_write_text(final, payload)
    logger.info("mailbox.write pulse=%s file=%s bytes=%d ok",
                pulse_id, fname, len(payload))
    return fname


def deliver(store: Store, pulse: Pulse, text: str, *,
            on_archived: OnArchived,
            wake: bool = True,
            locked: bool = False,
            remember: bool = False,
            write_first: bool = False,
            prefix: str = USER,
            source: Optional[str] = None,
            extra: Optional[dict[str, Any]] = None,
            supersede: bool = False,
            name: Optional[str] = None,
            wake_error: Optional[type[Exception]] = None,
            write_error: Optional[type[Exception]] = None) -> Optional[str]:
    """Deliver ``text`` to ``pulse`` with explicit archived-pulse and wake policy.

    When ``locked=True`` the caller already holds ``lifecycle.pulse_lock(pulse.id)``
    and :func:`lifecycle.wake_locked` is used; otherwise :func:`lifecycle.wake_if_stopped`
    is called. If ``wake_error`` or ``write_error`` is set, exceptions during the
    wake or write step are wrapped in that exception type (preserving ``__cause__``).
    """
    if pulse.state == "archived":
        if on_archived == "skip":
            return None
        if on_archived == "raise":
            raise Archived(f"pulse {pulse.id!r} is archived")

    fname = name or new_name(prefix)

    def _do_write() -> str:
        try:
            return write(pulse.id, text, prefix=prefix, source=source, extra=extra,
                         supersede=supersede, name=fname)
        except Exception as exc:
            if write_error is not None:
                raise write_error(str(exc)) from exc
            raise

    def _do_wake(target: Pulse) -> None:
        if not wake:
            return
        try:
            if locked:
                lifecycle.wake_locked(store, target.id)
            else:
                lifecycle.wake_if_stopped(store, target)
        except Exception as exc:
            if wake_error is not None:
                raise wake_error(str(exc)) from exc
            raise

    if write_first:
        _do_write()
        if remember:
            watch_sleep.remember(store, pulse, fname)
        if pulse.state == "archived" and on_archived == "unarchive":
            pulse = store.unarchive_pulse(pulse.id)
            pulse = store.update_settings(pulse.id, {"mode": "watching"})
        _do_wake(pulse)
        return fname

    if remember:
        watch_sleep.remember(store, pulse, fname)
    if pulse.state == "archived" and on_archived == "unarchive":
        pulse = store.unarchive_pulse(pulse.id)
        pulse = store.update_settings(pulse.id, {"mode": "watching"})
    _do_wake(pulse)
    return _do_write()
