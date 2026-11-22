"""Inbox routes: items, read state, stars, rules and channels."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Request

from pulsar.daemon import inbox as inbox_mod
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.schemas import (
    CreateInboxItemBody,
    InboxReadAllBody,
    InboxReplyBody,
    InboxScoreBody,
    PatchChannelBody,
)
from pulsar.store import UnknownChannel, UnknownInboxItem

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- inbox: produce / consume ------------------------------------------ #
    @router.post("/inbox/items")
    async def create_inbox_item(request: Request, body: CreateInboxItemBody):
        st = _store(request)
        try:
            title, body_txt, channel = inbox_mod.validate_item_input(
                body.title, body.body, body.channel, body.priority)
        except inbox_mod.InboxError as exc:
            raise ApiError(exc.code, exc.args[0], exc.status)

        needs_response = bool(body.needs_response)
        source_kind = "script"
        source_pulse_id = None
        source_label = body.source_label or "script"

        # Pulse-sourced items validate like pulse messages (from_pulse_id names
        # the sender; the label freezes the sender's title at creation).
        if body.from_pulse_id:
            sender = _get_pulse(request, body.from_pulse_id)
            source_kind = "pulse"
            source_pulse_id = sender.id
            source_label = sender.title
            needs_response = bool(body.needs_response)

        try:
            item = inbox_mod.produce(
                st, title=title, body=body_txt, channel=channel,
                source_kind=source_kind, source_label=source_label,
                source_pulse_id=source_pulse_id,
                thread_key=body.thread_key, priority=body.priority,
                needs_response=needs_response, validate=False)
        except Exception as exc:
            logger.error("inbox.item create FAILED err=%s", exc)
            raise ApiError("create_failed", str(exc), 400)

        if item is None:
            kind_key = inbox_mod.inbox_kind_key(channel, source_label, title)
            logger.info("inbox.item channel=%s kind_key=%s dropped (score=1)",
                        channel, kind_key)
            return _ok({"dropped": True, "score": 1, "kind_key": kind_key},
                       status=200)
        logger.info("inbox.item id=%s channel=%s kind=%s priority=%s ok",
                    item.id, item.channel, item.source_kind, item.priority)
        return _ok(inbox_mod.serialize_item(item), status=201)

    @router.get("/inbox")
    async def list_inbox(request: Request, unread_only: bool = True,
                         channel: Optional[str] = None,
                         thread_key: Optional[str] = None,
                         starred: bool = False, needs_response: bool = False,
                         q: Optional[str] = None, limit: int = 200,
                         before: Optional[str] = None):
        st = _store(request)
        items = st.list_inbox_items(
            unread_only=unread_only, channel=channel, thread_key=thread_key,
            starred=starred, needs_response=needs_response, q=q, limit=limit,
            before=before)
        return _ok([inbox_mod.serialize_item(i) for i in items])

    @router.get("/inbox/search")
    async def inbox_search(request: Request, q: str = "", limit: int = 100):
        """Whole-archive search across BOTH states (finds every match, not just
        the ones inside the 200-row view page). Empty q → empty list."""
        q = (q or "").strip()
        if not q:
            return _ok([])
        items = _store(request).search_inbox_items(q, limit=limit)
        return _ok([inbox_mod.serialize_item(i) for i in items])

    @router.get("/inbox/unread-count")
    async def inbox_unread_count(request: Request):
        st = _store(request)
        per_channel, per_thread, total = st.inbox_unread_maps()
        # Map the NULL thread key to the "" label the client expects.
        per_thread = {("" if k is None else k): v
                      for k, v in per_thread.items()}
        return _ok({"total": total, "channels": per_channel,
                    "threads": per_thread})

    @router.post("/inbox/read-all")
    async def inbox_read_all(request: Request, body: InboxReadAllBody):
        st = _store(request)
        # Requires a ``before`` cutoff — enforced in the schema.
        try:
            n = st.read_all_inbox(
                before=body.before, unread_only=body.unread_only,
                channel=body.channel, thread_key=body.thread_key,
                starred=body.starred, needs_response=body.needs_response,
                q=body.q)
        except Exception as exc:
            logger.error("inbox.read_all FAILED err=%s", exc)
            raise ApiError("read_all_failed", str(exc), 400)
        return _ok({"archived": n})

    @router.post("/inbox/items/{item_id}/read")
    async def inbox_set_read(request: Request, item_id: str):
        st = _store(request)
        try:
            item = st.set_inbox_read(item_id, True)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(inbox_mod.serialize_item(item))

    @router.post("/inbox/items/{item_id}/unread")
    async def inbox_set_unread(request: Request, item_id: str):
        st = _store(request)
        try:
            item = st.set_inbox_read(item_id, False)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(inbox_mod.serialize_item(item))

    @router.post("/inbox/items/{item_id}/star")
    async def inbox_star(request: Request, item_id: str):
        st = _store(request)
        try:
            item = st.set_inbox_starred(item_id, True)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(inbox_mod.serialize_item(item))

    @router.post("/inbox/items/{item_id}/unstar")
    async def inbox_unstar(request: Request, item_id: str):
        st = _store(request)
        try:
            item = st.set_inbox_starred(item_id, False)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(inbox_mod.serialize_item(item))

    @router.post("/inbox/items/{item_id}/dismiss-response")
    async def inbox_dismiss_response(request: Request, item_id: str):
        st = _store(request)
        try:
            item = st.clear_inbox_response(item_id)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(inbox_mod.serialize_item(item))

    @router.get("/inbox/items/{item_id}/conversation")
    async def inbox_conversation(request: Request, item_id: str):
        try:
            members, replies = _store(request).inbox_conversation(item_id)
        except UnknownInboxItem as exc:
            raise ApiError("not_found", str(exc), 404)
        messages = [inbox_mod.serialize_item(item) for item in members]
        messages.extend({**reply, "source_kind": "user", "source_label": "You",
                         "read_at": reply["created_at"], "needs_response": False}
                        for reply in replies)
        messages.sort(key=lambda message: message["created_at"])
        return _ok(messages)

    @router.post("/inbox/items/{item_id}/reply")
    async def inbox_reply(request: Request, item_id: str,
                          body: InboxReplyBody):
        st = _store(request)
        try:
            item = await asyncio.to_thread(
                inbox_mod.reply, st, item_id, body.text)
        except inbox_mod.InboxError as exc:
            raise ApiError(exc.code, exc.args[0], exc.status)
        return _ok(item)

    @router.post("/inbox/items/{item_id}/score")
    async def inbox_score(request: Request, item_id: str,
                          body: InboxScoreBody):
        st = _store(request)
        try:
            return _ok(inbox_mod.score(st, item_id, body.score))
        except inbox_mod.InboxError as exc:
            raise ApiError(exc.code, exc.args[0], exc.status)
        except Exception as exc:
            logger.error("inbox.score id=%s FAILED err=%s", item_id, exc)
            raise ApiError("score_failed", str(exc), 400)

    @router.get("/inbox/rules")
    async def inbox_rules(request: Request):
        st = _store(request)
        return _ok([inbox_mod.serialize_rule(r) for r in st.list_inbox_rules()])

    @router.delete("/inbox/rules/{kind_key:path}")
    async def inbox_delete_rule(request: Request, kind_key: str):
        st = _store(request)
        if not st.delete_inbox_rule(kind_key):
            raise ApiError("not_found", f"no rule for {kind_key!r}", 404)
        return _ok({"deleted": kind_key})

    @router.get("/inbox/channels")
    async def inbox_channels(request: Request):
        st = _store(request)
        return _ok([inbox_mod.serialize_channel(c) for c in st.list_channels()])

    @router.patch("/inbox/channels/{channel_name}")
    async def inbox_patch_channel(request: Request, channel_name: str,
                                  body: PatchChannelBody):
        st = _store(request)
        if body.mode is not None and body.mode not in ("notify", "quiet",
                                                       "muted"):
            raise ApiError("bad_mode", "mode must be notify|quiet|muted", 422)
        changes = {"mode": body.mode}
        if "mute_until" in body.model_fields_set:
            changes["mute_until"] = body.mute_until
        try:
            ch = st.update_channel(channel_name, **changes)
        except UnknownChannel as exc:
            raise ApiError("not_found", str(exc), 404)
        except Exception as exc:
            logger.error("inbox.channel patch=%s FAILED err=%s",
                         channel_name, exc)
            raise ApiError("patch_failed", str(exc), 400)
        return _ok(inbox_mod.serialize_channel(ch))

    return router
