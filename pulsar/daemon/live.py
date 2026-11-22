"""``/live`` — one WebSocket that pushes what the browser used to poll for.

Before: every tab polled ``GET /pulses`` every 3s and ``GET /grids`` every 5s,
and every open pane polled ``GET /pulses/{id}/events`` every 2s — each call a
fresh set of heartbeat reads and a tail read of events.jsonl, whether or not
anything had changed. With 20+ pulses and several tabs that is hundreds of
file reads a second to learn "nothing new".

Now a tab opens ``/live`` once and says which panes it has open::

    → {"type": "subscribe", "pulses": ["<id>", ...], "tail": 100}
    ← {"type": "roster", "pulses": [...GET /pulses shape...], "grids": [...]}
    ← {"type": "events", "pulse_id": "<id>", "offset": <bytes>,
       "events": [...], "delta": false}          # first snapshot / shrink
    ← {"type": "events", "pulse_id": "<id>", "offset": <bytes>,
       "events": [...new lines only...], "delta": true}
    ← {"type": "partial", "pulse_id": "<id>", "text": "<in-progress reply or null>"}
    ← {"type": "reasoning", "pulse_id": "<id>", "text": "<current reasoning trace or null>",
       "block": "<the part of the trace with no durable reasoning event yet, or null>"}
    ← {"type": "code", "pulse_id": "<id>",
       "code": {"id", "name", "text": "<tool-call argument JSON so far>", "truncated"} | null}

The roster goes out every ``ROSTER_INTERVAL_S`` and is computed ONCE per
interval no matter how many tabs are connected (module cache). Per-pane
events go out only when events.jsonl actually grew (a stat() per subscribed
pulse per ``TICK_S``), carrying the same tail window the pane rendered from
before, so the pane code needs no new rendering path. The same tick stats
each pane's ``partial.json`` and pushes ``{type: partial}`` on (mtime, size)
change so the ghost does not HTTP-poll ``GET /partial``, and the same for
``reasoning.json`` → ``{type: reasoning}`` (the reasoning panel), and
``code.json`` → ``{type: code}`` (the live code card). All file
work runs off the event loop.

The polling endpoints stay: a client that cannot hold a socket falls back to
them unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pulsar.daemon import events_io, paths, views

logger = logging.getLogger("pulsar.daemon.live")

TICK_S = 0.15            # pane check cadence; partial.json writes every 0.1s (was 0.5/0.3)
ROSTER_INTERVAL_S = 3.0  # roster/grids cadence (matches the old poll)
TAIL_DEFAULT = 100
TAIL_MAX = 2000
MAX_SUBSCRIPTIONS = 64


_METRICS_ENV = os.environ.get("PULSAR_METRICS_DATA_PATH", "").strip()
METRICS_DATA_PATH: Path | None = Path(_METRICS_ENV) if _METRICS_ENV else None


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


class _FeedChanges:
    """Cheap invalidations, not metric payloads; existing atomic writes are events.

    Each socket tracks its own cursor, including an initial/reconnect refresh.
    No JSON reads on this half-second path.
    """

    def __init__(self):
        self.sources = ([("metrics_sample", METRICS_DATA_PATH)]
                        if METRICS_DATA_PATH is not None else [])
        self.seen = {}

    def poll(self) -> list[dict[str, Any]]:
        changed = set()
        for kind, path in self.sources:
            stamp = _file_stamp(path)
            if path not in self.seen or self.seen[path] != stamp:
                if stamp is not None or path in self.seen:
                    changed.add(kind)
                self.seen[path] = stamp
        return [{"type": kind, "ts": time.time()} for kind in sorted(changed)]


def _roster_snapshot(store) -> dict[str, Any]:
    """The GET /pulses + GET /grids payloads, built with the same helpers."""
    return views.roster(store)


def _known_pulse_ids(store) -> set[str]:
    """Return every pulse id present in the store (including archived)."""
    return {p.id for p in store.list_pulses(include_archived=True)}


_parse_event_lines = views._parse_wire_lines


def _events_tail(pulse_id: str, tail: int) -> dict[str, Any]:
    """The GET /pulses/{id}/events payload plus the file size as ``offset``."""
    return views.events_live(pulse_id, tail, None)


def _events_since(pulse_id: str, after: int, tail: int) -> dict[str, Any]:
    """New jsonl lines after ``after``, or a full tail if the cursor is stale.

    ``delta: True`` means the client must append; ``False`` replaces the window
    (first subscribe, file shrank, or the gap exceeded the bounded read).
    """
    return views.events_live(pulse_id, tail, after)


_events_size = events_io.events_size


def _partial_stat(pulse_id: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of the runner's partial.json, or None if missing."""
    try:
        st = paths.partial_path(pulse_id).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _partial_payload(pulse_id: str, st: tuple[int, int] | None) -> dict[str, Any]:
    return views.partial_live(pulse_id, st)


def _reasoning_stat(pulse_id: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of the runner's reasoning.json, or None if missing."""
    try:
        st = paths.reasoning_path(pulse_id).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _reasoning_payload(pulse_id: str,
                       st: tuple[int, int] | None) -> dict[str, Any]:
    return views.reasoning_live(pulse_id, st)


def _code_stat(pulse_id: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of the runner's code.json, or None if missing."""
    try:
        st = paths.code_path(pulse_id).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class _RosterCache:
    """One roster computation per interval shared by every connection."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.at = 0.0
        self.lock = asyncio.Lock()
        self._gen = 0  # bumped by invalidate(); discards in-flight stale computes

    def invalidate(self) -> None:
        """A mutation just changed the roster — never serve the old snapshot.

        Without this, a frame sent up to ROSTER_INTERVAL_S after a pulse was
        created still carries a snapshot computed before the create committed.
        The web client adds the new pulse locally from the create response, so
        such a frame briefly DELETES it again — and on the mobile deck the pane
        then adopts a neighbour and never comes back (the "new pulse sometimes
        doesn't open on mobile" bug, 2026-09-06).
        """
        self._gen += 1
        self.payload = None

    async def get(self, store) -> dict[str, Any]:
        while True:
            now = time.monotonic()
            if self.payload is not None and now - self.at < ROSTER_INTERVAL_S:
                return self.payload
            gen = self._gen
            async with self.lock:
                now = time.monotonic()
                if self.payload is not None and now - self.at < ROSTER_INTERVAL_S:
                    return self.payload
                snap = await asyncio.to_thread(_roster_snapshot, store)
                if gen != self._gen:
                    # Invalidated while the snapshot was being computed off
                    # loop — it predates the mutation, so recompute instead of
                    # caching a roster that already lies.
                    continue
                self.payload = snap
                self.at = time.monotonic()
                return self.payload


def build_router(app=None) -> APIRouter:
    router = APIRouter()
    roster_cache = _RosterCache()
    if app is not None:
        # Mutating endpoints (create/fork) invalidate the cache so a /live
        # frame is never served without a pulse the API already acked.
        app.state.roster_cache = roster_cache

    @router.websocket("/live")
    async def live(ws: WebSocket):
        await ws.accept()
        store = ws.app.state.store
        subs: dict[str, int] = {}        # pulse id -> tail size
        seen: dict[str, int] = {}        # pulse id -> last pushed file size
        # pulse id -> last (mtime_ns, size) of partial.json; None = missing
        partial_seen: dict[str, tuple[int, int] | None] = {}
        reasoning_seen: dict[str, tuple[int, int] | None] = {}
        code_seen: dict[str, tuple[int, int] | None] = {}
        last_roster_at = 0.0
        t_open = time.monotonic()
        pushes = 0
        feeds = _FeedChanges()

        async def reader() -> None:
            nonlocal subs
            while True:
                msg = await ws.receive_json()
                if msg.get("type") != "subscribe":
                    continue
                raw_ids = msg.get("pulses")
                ids = raw_ids if isinstance(raw_ids, list) else []
                tail = int(msg.get("tail") or TAIL_DEFAULT)
                tail = max(1, min(tail, TAIL_MAX))
                candidates = [
                    pid for pid in ids[:MAX_SUBSCRIPTIONS]
                    if isinstance(pid, str) and pid
                    and "/" not in pid and "\\" not in pid and ".." not in pid
                ]
                known = (await asyncio.to_thread(_known_pulse_ids, store)
                         if candidates else set())
                new: dict[str, int] = {
                    pid: tail for pid in candidates if pid in known
                }
                # Tail-window changes (scrollback) do not resend: the pane
                # HTTP-fetches GET /events for older history. Only drop cursors
                # for pulses that left the subscription.
                for pid in list(seen):
                    if pid not in new:
                        seen.pop(pid, None)
                        partial_seen.pop(pid, None)
                        reasoning_seen.pop(pid, None)
                        code_seen.pop(pid, None)
                subs = new

        read_task = asyncio.create_task(reader())
        try:
            while True:
                if read_task.done():
                    read_task.result()  # raises WebSocketDisconnect
                now = time.monotonic()
                if now - last_roster_at >= ROSTER_INTERVAL_S:
                    await ws.send_json(await roster_cache.get(store))
                    last_roster_at = now
                    pushes += 1
                for payload in await asyncio.to_thread(feeds.poll):
                    await ws.send_json(payload)
                    pushes += 1
                for pid, tail in list(subs.items()):
                    size = await asyncio.to_thread(_events_size, pid)
                    prev = seen.get(pid)
                    if prev == size:
                        pass
                    else:
                        if prev is None:
                            payload = await asyncio.to_thread(_events_tail, pid, tail)
                        else:
                            payload = await asyncio.to_thread(
                                _events_since, pid, prev, tail)
                        if not payload.get("unchanged"):
                            seen[pid] = payload["offset"]
                            await ws.send_json(payload)
                            pushes += 1
                        else:
                            seen[pid] = payload["offset"]
                    st = _partial_stat(pid)
                    if pid in partial_seen and partial_seen.get(pid) == st:
                        pass
                    elif pid not in partial_seen and st is None:
                        # First look, nothing streaming — remember absence so
                        # a later create is a change, but don't push null.
                        partial_seen[pid] = None
                    else:
                        partial_seen[pid] = st
                        await ws.send_json(await asyncio.to_thread(
                            _partial_payload, pid, st))
                        pushes += 1
                    rst = _reasoning_stat(pid)
                    if pid in reasoning_seen and reasoning_seen.get(pid) == rst:
                        pass
                    elif pid not in reasoning_seen and rst is None:
                        # Same contract as the partial: silence while absent.
                        reasoning_seen[pid] = None
                    else:
                        reasoning_seen[pid] = rst
                        await ws.send_json(await asyncio.to_thread(
                            _reasoning_payload, pid, rst))
                        pushes += 1
                    cst = _code_stat(pid)
                    if pid in code_seen and code_seen.get(pid) == cst:
                        pass
                    elif pid not in code_seen and cst is None:
                        code_seen[pid] = None
                    else:
                        code_seen[pid] = cst
                        await ws.send_json(await asyncio.to_thread(
                            views.code_live, pid, cst))
                        pushes += 1
                await asyncio.sleep(TICK_S)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("live: connection loop RAISED")
        finally:
            read_task.cancel()
            logger.info("live: closed after %.0fs pushes=%d subs=%d",
                        time.monotonic() - t_open, pushes, len(subs))

    return router
