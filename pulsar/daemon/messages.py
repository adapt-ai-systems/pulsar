"""Pulse-to-pulse messages.

One pulse sends another an ordinary inbox message — same contract as a human
typing in the pane. There is no reply tracking: a "reply" is just the target
messaging the sender back the same way (the spawn-time context file teaches
every pulse the recipe). This replaced the summon watcher; with replies
explicit there is nothing to poll, no timeout, and no duration limit.

The daemon does exactly three things the plain inbox write wouldn't:
  - refuses self-sends and archived targets (loudly)
  - wakes a stopped target (a pulse-to-pulse ask must land, same rule as a
    wake schedule)
  - prefixes the text with the sender's title AND id — the id is the reply
    address, so the target never needs a roster lookup to answer
"""

from __future__ import annotations

import logging
import time

from pulsar.daemon import lifecycle, mailbox
from pulsar.store import Store

logger = logging.getLogger("pulsar.daemon.messages")


class MessageError(Exception):
    """A pulse-to-pulse send could not be made (self, archived, wake failure)."""


def send(store: Store, sender_id: str, target_id: str, text: str,
         source: str = "pulse") -> str:
    """Deliver one pulse's message to another's inbox. Returns the filename.

    Wakes a stopped target, under the target's pulse lock (wake, then write).
    Sync, worker threads only: the route awaits it with ``to_thread``.
    Raises :class:`MessageError` on self-send, archived target, or wake
    failure — the caller surfaces it.
    """
    started = time.monotonic()
    if sender_id == target_id:
        logger.error("message REFUSED pulse=%s messaging itself", sender_id)
        raise MessageError("a pulse cannot message itself")
    sender = store.get_pulse(sender_id)
    with lifecycle.pulse_lock(target_id):
        target = store.get_pulse(target_id)
        try:
            name = mailbox.deliver(
                store, target,
                f"[message from {sender.title!r} (pulse {sender.id})] {text}",
                on_archived="raise", locked=True, source="pulse",
                extra=mailbox.origin_keys(
                    "peer", "schedule" if source.startswith("schedule") else "pulse",
                    from_pulse_id=sender.id, from_title=sender.title),
                wake_error=RuntimeError,
            )
            assert name is not None
        except mailbox.Archived as exc:
            logger.error("message REFUSED sender=%s target=%s (archived)",
                         sender_id, target_id)
            raise MessageError(f"target pulse {target.title!r} is archived") from exc
        except RuntimeError as exc:
            logger.error("message sender=%s target=%s WAKE FAILED err=%s",
                         sender_id, target_id, exc.__cause__ or exc)
            raise MessageError(
                f"could not wake target: {exc.__cause__ or exc}") from (exc.__cause__ or exc)
    logger.info("message.sent %s -> %s file=%s source=%s in %.3fs",
                sender_id, target_id, name, source,
                time.monotonic() - started)
    return name
