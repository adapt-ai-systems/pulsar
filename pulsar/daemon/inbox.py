"""Inbox produce + reply helpers.

The daemon's inbox is a durable async comms rail between the user and the agent
pulses. This module provides:

  - ``serialize_item`` — an InboxItem → dict (single serializer the routes and
    tests share; no drift between the list and detail shapes).
  - ``produce`` — one canonical create path used by BOTH the ``POST /inbox/items``
    route and the internal producers (watcher / loopguard / scheduler), so item
    caps and channel-slug rules are enforced in ONE place.
  - ``reply`` — the async reply write path. This is a dedicated path and does
    NOT call ``messages.send``: it writes the user's reply into
    the source pulse's ``inbox/new`` (atomic tmp+rename, the same contract as
    ``messages._write_inbox``), wakes a stopped source, backfills a thread_key
    on thread-less items, saves the sent text, stamps ``replied_at`` and clears
    ``needs_response``.
    Only pulse-sourced items can be replied to; wake/write failure is an
    enveloped error and leaves the item untouched (replied_at unset,
    needs_response uncleared). A source that is archived or gone is a 409.

Rule shared by every producer: an inbox creation must NEVER break the path that
called it — callers wrap in try/except and log (standing guard, same as the
existing watcher/loopguard call sites).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional, Union

from pulsar.daemon import lifecycle, mailbox
from pulsar.daemon.timeutil import now_iso as _now_iso
from pulsar.store import (InboxItem, Store, UnknownInboxItem, UnknownPulse,
                          inbox_kind_key)

logger = logging.getLogger("pulsar.daemon.inbox")


class InboxError(Exception):
    """A produce/reply could not be made (bad input, archived source, wake
    failure). Carries the HTTP status the route should surface."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# Input caps + channel-slug rules.
TITLE_MAX = 200
BODY_MAX_BYTES = 64 * 1024            # 64KB
CHANNEL_RE = re.compile(r"^[a-z0-9-]{1,64}$")
VALID_PRIORITY = ("notify", "quiet")


def effective_channel_mode(mode: str, mute_until: Optional[str],
                           now: Optional[datetime] = None) -> str:
    """The channel's EFFECTIVE mode right now (passive mute expiry)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if mute_until:
        try:
            dt = datetime.fromisoformat(mute_until.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if now < dt:
                return "muted"
        except ValueError:
            pass
    return mode


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def normalize_channel(raw: str) -> str:
    """Lowercase + validate a channel slug.

    Raises :class:`InboxError` (422) on anything that isn't ``[a-z0-9-]{1,64}``
    after lowercasing.
    """
    ch = (raw or "").strip().lower()
    if not CHANNEL_RE.match(ch):
        raise InboxError(
            "bad_channel",
            f"channel must match [a-z0-9-]{{1,64}} (got {raw!r})", 422)
    return ch


def validate_item_input(title: str, body: str, channel: str,
                        priority: Optional[str] = None) -> tuple[str, str, str]:
    """Enforce the shared caps. Returns ``(title, body, channel)`` cleaned.

    Raises :class:`InboxError` (422) beyond the caps. Rejects control chars in
    the title, and body above the 64KB byte cap.
    """
    title = (title or "").strip()
    if not title:
        raise InboxError("bad_title", "title is required", 422)
    if len(title) > TITLE_MAX:
        raise InboxError("title_too_long",
                         f"title > {TITLE_MAX} chars", 422)
    body = (body or "").strip()
    if not body:
        raise InboxError("bad_body", "body is required", 422)
    if len(body.encode("utf-8")) > BODY_MAX_BYTES:
        raise InboxError("body_too_large", "body > 64KB", 422)
    channel = normalize_channel(channel)
    if priority is not None and priority not in VALID_PRIORITY:
        raise InboxError("bad_priority",
                         "priority must be notify|quiet", 422)
    return title, body, channel


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_item(item: InboxItem) -> dict[str, Any]:
    """One InboxItem → the API dict shape (shared by list + detail routes)."""
    return {
        "id": item.id,
        "created_at": item.created_at,
        "cursor": item.cursor,
        "channel": item.channel,
        "thread_key": item.thread_key,
        "source_kind": item.source_kind,
        "source_pulse_id": item.source_pulse_id,
        "source_label": item.source_label,
        "title": item.title,
        "body": item.body,
        "priority": item.priority,
        "needs_response": item.needs_response,
        "starred": item.starred,
        "read_at": item.read_at,
        "replied_at": item.replied_at,
        "score": item.score,
        "kind_key": item.kind_key,
    }


def serialize_rule(rule) -> dict[str, Any]:
    """InboxRule → dict."""
    return {
        "kind_key": rule.kind_key,
        "score": rule.score,
        "channel": rule.channel,
        "source_label": rule.source_label,
        "example_title": rule.example_title,
        "updated_at": rule.updated_at,
    }


def serialize_channel(ch) -> dict[str, Any]:
    """InboxChannel → dict (mode + mute_until, as PATCH returns)."""
    return {
        "channel": ch.channel,
        "mode": ch.mode,
        "mute_until": ch.mute_until,
        "created_at": ch.created_at,
    }


# --------------------------------------------------------------------------- #
# Produce — the one create path (route + internal producers share it)
# --------------------------------------------------------------------------- #
def produce(store: Store, *, title: str, body: str, channel: str,
            source_kind: str, source_label: str = "",
            source_pulse_id: Optional[str] = None,
            thread_key: Optional[str] = None,
            priority: Optional[str] = None,
            needs_response: bool = False,
            validate: bool = True) -> Optional[InboxItem]:
    """Create one inbox item. Call this from the route AND the producers.

    ``validate=True`` enforces caps + slug (the route); internal producers that
    already built a clean title/channel may pass ``validate=False`` for a tiny
    faster path (still safe — they only ship slugs they control). Returns the
    committed item, or ``None`` when dropped by a learned ``score == 1`` rule.
    """
    if validate:
        title, body, channel = validate_item_input(title, body, channel,
                                                   priority)
    # Learned-rule gate (v11 / D8): a score the user taught for this KIND of
    # message overrides the sender's self-declared priority.  1 = dropped
    # outright (not stored), 2 = quiet, 3 = notify.
    score: Optional[int] = None
    kind_key = inbox_kind_key(channel, source_label, title)
    try:
        rule = store.get_inbox_rule(kind_key)
    except Exception as exc:
        logger.error("inbox.produce rule-read FAILED err=%s (no gate)", exc)
        rule = None
    if rule is not None:
        score = rule.score
        if score == 1:
            logger.info("inbox.produce kind=%s score=1 → dropped, not stored",
                        kind_key)
            return None
        elif score == 2:
            priority = "quiet"
        else:
            priority = "notify"
    return store.create_inbox_item(
        channel=channel,
        source_kind=source_kind,
        title=title,
        body=body,
        source_label=source_label,
        thread_key=thread_key,
        source_pulse_id=source_pulse_id,
        priority=priority or "notify",
        needs_response=needs_response,
        score=score,
        read_at=None,
    )


# --------------------------------------------------------------------------- #
# Reply — the async reply write path (NOT messages.send)
# --------------------------------------------------------------------------- #
def reply(store: Store, item_id: str, text: str) -> dict[str, Any]:
    """Deliver the user's async reply to a pulse-sourced item's source pulse.

    Returns the item (now stamped replied_at + needs_response cleared). Raises
    :class:`InboxError`:
      - ``not_found`` 404 — no such item.
      - ``not_replyable`` 409 — source pulse absent/archived, OR the item was
        not pulse-sourced (only pulse-origin items can be replied to).
      - ``reply_failed`` 502 — wake/write failed; the item is left untouched
        (replied_at unset, needs_response NOT cleared).
    """
    started = time.monotonic()
    try:
        item = store.get_inbox_item(item_id)
    except UnknownInboxItem as exc:
        raise InboxError("not_found", str(exc), 404) from exc

    # Pulse-sourced only.
    if item.source_kind != "pulse" or not item.source_pulse_id:
        raise InboxError(
            "not_replyable",
            "only items produced by a pulse can be replied to", 409)

    # 409 if the source pulse is archived or gone.
    try:
        source = store.get_pulse(item.source_pulse_id)
        if source.state == "archived":
            raise InboxError(
                "source_archived",
                f"source pulse {item.source_pulse_id!r} is archived; "
                f"reply disabled", 409)
    except UnknownPulse as exc:
        raise InboxError(
            "source_gone",
            f"source pulse {item.source_pulse_id!r} no longer exists; "
            f"reply disabled", 409) from exc

    # Backfill a thread_key on thread-less items so the pulse's answer lands in
    # the same thread (re-<item_id[:8]>).
    thread_key = item.thread_key
    if not thread_key:
        thread_key = f"re-{item.id[:8]}"
        store.set_inbox_thread(item.id, thread_key)

    wrapped = (f"[The user replied via Inbox re: {thread_key or item.title}] "
               f"{text.strip()} — report your answer back via POST "
               f"/inbox/items with the same thread_key.")

    # Wake a stopped source first — a reply must land, same rule as a wake
    # schedule. Failure → enveloped error, item untouched. Wake and write share
    # the source's pulse lock (worker thread: the route awaits with to_thread).
    with lifecycle.pulse_lock(item.source_pulse_id):
        source = store.get_pulse(item.source_pulse_id)
        sent_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            mailbox.deliver(
                store, source, wrapped,
                on_archived="raise", locked=True, source="pulse",
                extra=mailbox.origin_keys("user", "inbox_reply"),
                wake_error=RuntimeError, write_error=OSError,
            )
            # The user's reply counts as user input (v17)
            store.touch_input(item.source_pulse_id)
        except mailbox.Archived as exc:
            raise InboxError(
                "source_archived",
                f"source pulse {item.source_pulse_id!r} is archived; "
                f"reply disabled", 409) from exc
        except RuntimeError as exc:
            cause = exc.__cause__ or exc
            logger.error("inbox.reply item=%s source=%s WAKE FAILED err=%s "
                         "(item left untouched)", item_id,
                         item.source_pulse_id, cause)
            raise InboxError("reply_failed",
                             f"could not wake source: {cause}", 502) from cause
        except Exception as exc:
            cause = exc.__cause__ or exc
            logger.error("inbox.reply item=%s source=%s WRITE FAILED err=%s "
                         "(item left untouched)", item_id,
                         item.source_pulse_id, cause)
            raise InboxError("reply_failed", f"could not deliver reply: {cause}",
                             502) from cause

    item = store.save_inbox_reply(item_id, text.strip(), sent_at)
    logger.info("inbox.reply item=%s source=%s thread=%s ok in %.3fs",
                item_id, item.source_pulse_id, thread_key,
                time.monotonic() - started)
    return serialize_item(item)


# --------------------------------------------------------------------------- #
# Score — the user teaches the system how to treat a KIND of message (v11)
# --------------------------------------------------------------------------- #
VALID_SCORES = (1, 2, 3)


def score(store: Store, item_id: str, value: Optional[int]) -> dict[str, Any]:
    """Score one item 1|2|3 (or ``None`` to unlearn).

    Writes the rule for the item's kind, stamps the score on every existing
    item of that kind, and for score 1 archives the unread ones (retro
    cleanup). Returns ``{"item", "rule", "affected"}``.
    """
    try:
        item = store.get_inbox_item(item_id)
    except UnknownInboxItem as exc:
        raise InboxError("not_found", str(exc), 404) from exc
    if value is not None and value not in VALID_SCORES:
        raise InboxError("bad_score", "score must be 1, 2, 3 or null", 422)
    key = item.kind_key
    rule = None
    if value is None:
        store.delete_inbox_rule(key)
    else:
        rule = store.upsert_inbox_rule(key, value, item.channel,
                                       item.source_label, item.title)
    affected = store.apply_inbox_score_to_kind(key, value,
                                               archive=(value == 1))
    item = store.get_inbox_item(item_id)
    logger.info("inbox.score item=%s kind=%s score=%s affected=%d ok",
                item_id, key, value, affected)
    return {"item": serialize_item(item),
            "rule": serialize_rule(rule) if rule else None,
            "affected": affected}
