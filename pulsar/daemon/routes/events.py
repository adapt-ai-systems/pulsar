"""Per-pulse event reads: durable events, task events, live partial text and
reasoning."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request

from pulsar.daemon import bookmark as bookmark_mod, events_io, \
    mailbox, paths, views
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    invalidate_roster as _invalidate_roster,
    ok as _ok,
    store as _store,
)


# /pulses/{id}/events tail bounds: the pane's first page, and a hard cap so a
# scrollback request can never turn into an unbounded read of events.jsonl.
EVENTS_TAIL_DEFAULT = 100
EVENTS_TAIL_MAX = 2000


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/pulses/{pulse_id}/partial")
    async def get_partial(request: Request, pulse_id: str):
        """The in-progress reply text, or null when nothing is streaming.

        Reads the runner's atomic-replaced partial.json; a torn/missing file is
        indistinguishable from "not streaming" on purpose — this surface is
        display-only and refreshes within half a second.
        """
        pulse = _get_pulse(request, pulse_id)
        return _ok(views.partial_http(pulse.id))

    @router.get("/pulses/{pulse_id}/reasoning")
    async def get_reasoning(request: Request, pulse_id: str):
        """The current turn's reasoning trace, or null when there is none.

        Reads the runner's atomic-replaced reasoning.json (the model's
        thinking, written while it thinks). Unlike the partial it survives the
        turn boundary so the last trace stays readable until the next turn
        starts. Display-only: a torn/missing file reads as "nothing", never a
        500 — same contract as GET /pulses/{id}/partial.
        """
        pulse = _get_pulse(request, pulse_id)
        return _ok(views.reasoning_http(pulse.id))

    @router.get("/pulses/{pulse_id}/code")
    async def get_code(request: Request, pulse_id: str):
        """The tool call being generated right now (its argument JSON so
        far), or null. Reads the runner's atomic-replaced code.json; same
        display-only contract as GET /pulses/{id}/partial."""
        pulse = _get_pulse(request, pulse_id)
        return _ok(views.code_http(pulse.id))

    # -- events ------------------------------------------------------------ #
    @router.get("/pulses/{pulse_id}/events")
    async def get_events(request: Request, pulse_id: str,
                         tail: int = EVENTS_TAIL_DEFAULT,
                         after: Optional[int] = None,
                         event_id: Optional[str] = None,
                         full: bool = False):
        """The last ``tail`` events, read as a bounded seek from EOF.

        events.jsonl is append-only and grows for the pulse's whole life, while
        every visible pane polls this every two seconds — so the tail is a
        backward window read (``events_io.last_lines``), never a whole-file
        decode, and ``tail`` is capped.

        ``after`` (optional) is the byte ``offset`` the previous response
        reported. Passing it back turns a poll where nothing was appended into
        a single stat() — no read, no parse — and a poll with new events into
        a read of just the appended delta (``events_io.read_events_after``);
        the response always carries the new ``offset`` to store.

        Long ``tool_result`` bodies are clipped on the wire (``truncated: true``)
        so the collapsed tool line still renders; pass ``event_id`` (or
        ``full=1``) to fetch the untrimmed event for tap-to-expand. The durable
        jsonl is never rewritten.
        """
        pulse = _get_pulse(request, pulse_id)
        if tail < 1:
            raise ApiError("bad_request", "tail must be >= 1", 400)
        tail = min(tail, EVENTS_TAIL_MAX)
        return _ok(views.events_http(
            pulse.id, tail, after, event_id=event_id, full=full
        ))

    @router.get("/pulses/{pulse_id}/task-events")
    async def get_task_events(request: Request, pulse_id: str):
        """The pulse's task-tool events (todo_write / update_plan /
        TaskCreate / TaskUpdate + TaskCreate results) from the WHOLE log,
        oldest first, capped (``events_io.TASK_EVENTS_MAX``). The pane folds
        these with its tail so the TODO card's timestamps reflect when each
        item really appeared, started, and finished — not the start of the
        tail window."""
        pulse = _get_pulse(request, pulse_id)
        path = paths.events_path(pulse.id)
        if not path.exists():
            return _ok({"events": [], "count": 0})

        def scan():
            # Off the event loop: a cold scan of a big log reads it whole, and
            # the reply can be a megabyte of JSON (2026-11-02: 5 s freezes).
            events = events_io.read_task_events(path)
            return _ok({"events": events, "count": len(events)})

        return await asyncio.to_thread(scan)

    # -- bookmark ("what happened since I left") --------------------------- #
    @router.post("/pulses/{pulse_id}/bookmark")
    async def press_bookmark(request: Request, pulse_id: str):
        """The pane's bookmark button. First press sets a bookmark (time +
        events.jsonl offset in settings.bookmark). Each later press writes a
        digest of the events since it, asks the pulse to summarize only that
        period (waking it if stopped), and sets a new bookmark at now."""
        pulse = _get_pulse(request, pulse_id)
        if pulse.state == "archived":
            raise ApiError("archived", "pulse is archived", 409)
        try:
            out = await asyncio.to_thread(bookmark_mod.press, _store(request), pulse)
        except mailbox.Archived as exc:
            raise ApiError("archived", str(exc), 409)
        except OSError as exc:
            raise ApiError("bookmark_failed", str(exc), 500)
        _invalidate_roster(request)
        return _ok({"bookmark": out["bookmark"], "summary": out["summary"],
                    "pulse": views.pulse_view(out["pulse"])})

    @router.delete("/pulses/{pulse_id}/bookmark")
    async def clear_bookmark(request: Request, pulse_id: str):
        """Double-tap on the bookmark button: clear it, ask for no summary."""
        pulse = _get_pulse(request, pulse_id)
        updated = await asyncio.to_thread(bookmark_mod.clear, _store(request), pulse)
        _invalidate_roster(request)
        return _ok({"bookmark": None, "pulse": views.pulse_view(updated)})

    return router
