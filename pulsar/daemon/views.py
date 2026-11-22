"""Read models shared by the HTTP routes and the ``/live`` WebSocket.

Pure serializers and bounded file readers for pulses, grids, schedules, the
roster snapshot, partial/reasoning streams, and ``events.jsonl`` windows.

HTTP and ``/live`` intentionally keep their distinct wire contracts:
- Events: :func:`events_http` returns the full tail window after an append
  (plus ``event_id`` and ``full`` modes), whereas :func:`events_live` returns
  only the newly appended delta (``delta=True``) or a full replacement tail
  (``delta=False``) on first subscribe, shrink, or an over-cap gap.
- Partial / reasoning: :func:`partial_http` and :func:`reasoning_http` include
  ``ts`` inside a nested dict; :func:`partial_live` and :func:`reasoning_live`
  return the flat ``/live`` frame shape without ``ts``.
"""

from __future__ import annotations

from pulsar.daemon import loop_triggers

import json
from typing import Any, Optional

from pulsar.daemon import events_io, paths, sockclient
from pulsar.store import Grid, Pulse, Schedule, Store

# /pulses/{id}/events and /live tail bounds.
EVENTS_TAIL_DEFAULT = 100
EVENTS_TAIL_MAX = 2000


def default_stream_partials(vessel: Optional[str], model: Optional[str]) -> bool:
    """Live streaming is on by default for every agent tool.

    The runner writes partial.json unconditionally — this is only the UI gate.
    A pulse turns it off with PATCH /pulses/{id}/stream.
    """
    return True


def pulse_dict(pulse: Pulse) -> dict[str, Any]:
    settings = dict(pulse.settings or {})
    # A pulse that never stored the key gets the default (on).
    # Fill it in the view so the pane gate sees ON without a DB backfill.
    if "stream_partials" not in settings and default_stream_partials(
        pulse.vessel, pulse.model
    ):
        settings["stream_partials"] = True
    if "stream_reasoning" not in settings:
        settings["stream_reasoning"] = True
    if "stream_code" not in settings:
        settings["stream_code"] = True
    return {
        "id": pulse.id,
        "title": pulse.title,
        "model": pulse.model,
        "vessel": pulse.vessel,
        "grid_id": pulse.grid_id,
        "state": pulse.state,
        "handoff_tokens": pulse.handoff_tokens,
        "created_at": pulse.created_at,
        "updated_at": pulse.updated_at,
        "archived_at": pulse.archived_at,
        "settings": settings,
        "position": pulse.position,
        "last_input_at": pulse.last_input_at,
    }


def grid_dict(grid: Grid) -> dict[str, Any]:
    return {
        "id": grid.id,
        "name": grid.name,
        "position": grid.position,
        "parent_id": grid.parent_id,
    }


def schedule_dict(sched: Schedule) -> dict[str, Any]:
    return {
        "id": sched.id,
        "pulse_id": sched.pulse_id,
        "kind": sched.kind,
        "prompt": sched.prompt,
        "interval_minutes": sched.interval_minutes,
        "at_iso": sched.at_iso,
        "cron_hhmm": sched.cron_hhmm,
        "days": sched.days,
        "enabled": sched.enabled,
        "last_fired_at": sched.last_fired_at,
        "created_at": sched.created_at,
        "target_pulse_id": sched.target_pulse_id,
        "triggers": loop_triggers.as_list(sched.triggers),
        "trigger_mode": loop_triggers.mode(sched.trigger_mode),
    }


def live_state(pulse: Pulse, probe: bool = False) -> Optional[dict[str, Any]]:
    """Live decoration for a running pulse, read off its heartbeat file.

    The heartbeat carries exactly what the socket's status op returns (state,
    cli_pid, cli_session_id, busy_since) and the runner rewrites it every
    second, so the roster derives liveness from the file it already reads
    instead of a socket round trip per pulse: ``sockclient.TIMEOUT_S`` is 5s
    and ``/pulses`` maps this serially inside an async handler, so a handful of
    dead sockets could stall the whole daemon for a poll.

    ``probe=True`` (the single-pulse GET) adds the authoritative socket check
    on top for callers that want the runner asked directly.

    Only meaningful while the store says ``running``. Never raises — a dead
    runner yields a ``{"reachable": false, ...}`` block, not a 500.
    """
    if pulse.state != "running":
        return None
    beat = paths.read_heartbeat(pulse.id)
    fresh = paths.heartbeat_fresh(beat)
    b = beat or {}
    hb_state = b.get("state")
    dead_beat = hb_state == "cli_dead"
    live: dict[str, Any] = {
        "heartbeat": beat,
        "heartbeat_age_s": paths.heartbeat_age(beat),
        "heartbeat_fresh": fresh,
        # No probe here: a heartbeat written seconds ago IS the evidence that
        # the runner is alive — unless that beat already reports death.
        "reachable": fresh and not dead_beat,
        "socket_state": hb_state,
        "cli_pid": b.get("cli_pid"),
        "cli_session_id": b.get("cli_session_id"),
        # "thinking · Ns" source (status honesty): set iff busy.
        "busy_since": b.get("busy_since"),
    }
    if probe:
        status = sockclient.probe(paths.sock_path(pulse.id), pulse.id)
        live["reachable"] = status is not None
        if status is not None:
            live["socket_state"] = status.get("state")
            live["cli_pid"] = status.get("cli_pid")
            live["cli_session_id"] = status.get("cli_session_id")
            live["busy_since"] = status.get("busy_since")
    return live


def pulse_view(
    pulse: Pulse, include_live: bool = True, probe: bool = False
) -> dict[str, Any]:
    d = pulse_dict(pulse)
    if include_live:
        d["live"] = live_state(pulse, probe=probe)
    return d


def roster(store: Store) -> dict[str, Any]:
    """The GET /pulses + GET /grids payloads, built with the shared helpers."""
    pulses = store.list_pulses(include_archived=False)
    views = [pulse_view(p) for p in pulses]
    grids = [grid_dict(g) for g in store.list_grids()]
    return {"type": "roster", "pulses": views, "grids": grids}


# --------------------------------------------------------------------------- #
# Partial & reasoning readers
# --------------------------------------------------------------------------- #
def read_partial(pulse_id: str) -> tuple[Optional[str], Optional[Any]]:
    """Read ``(text, ts)`` from ``partial.json``, or ``(None, None)`` if absent/invalid."""
    try:
        data = json.loads(paths.partial_path(pulse_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None, None
    return data["text"], data.get("ts")


def partial_http(pulse_id: str) -> dict[str, Any]:
    """HTTP ``GET /pulses/{id}/partial`` value payload (keeps ``ts``)."""
    text, ts = read_partial(pulse_id)
    if text is None:
        return {"partial": None}
    return {"partial": {"ts": ts, "text": text}}


def partial_live(
    pulse_id: str, st: tuple[int, int] | None = ...
) -> dict[str, Any]:
    """``/live`` partial frame payload (omits ``ts``)."""
    if st is None:
        text = None
    else:
        text, _ = read_partial(pulse_id)
    return {"type": "partial", "pulse_id": pulse_id, "text": text}


def read_reasoning(
    pulse_id: str,
) -> tuple[Optional[str], Optional[str], Optional[Any]]:
    """Read ``(text, block, ts)`` from ``reasoning.json``, or ``(None, None, None)``."""
    try:
        data = json.loads(paths.reasoning_path(pulse_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None, None, None
    block = data.get("block")
    return (
        data["text"],
        block if isinstance(block, str) else None,
        data.get("ts"),
    )


def reasoning_http(pulse_id: str) -> dict[str, Any]:
    """HTTP ``GET /pulses/{id}/reasoning`` value payload (keeps ``ts``)."""
    text, block, ts = read_reasoning(pulse_id)
    if text is None:
        return {"reasoning": None}
    return {"reasoning": {"ts": ts, "text": text, "block": block}}


def reasoning_live(
    pulse_id: str, st: tuple[int, int] | None = ...
) -> dict[str, Any]:
    """``/live`` reasoning frame payload (omits ``ts``)."""
    if st is None:
        text, block = None, None
    else:
        text, block, _ = read_reasoning(pulse_id)
    return {
        "type": "reasoning",
        "pulse_id": pulse_id,
        "text": text,
        "block": block,
    }


def read_code(pulse_id: str) -> Optional[dict[str, Any]]:
    """Read ``{ts, id, name, text, truncated}`` from ``code.json``, or None."""
    try:
        data = json.loads(paths.code_path(pulse_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    name, cid = data.get("name"), data.get("id")
    return {
        "ts": data.get("ts"),
        "id": cid if isinstance(cid, str) else None,
        "name": name if isinstance(name, str) else None,
        "text": data["text"],
        "truncated": data.get("truncated") is True,
    }


def code_http(pulse_id: str) -> dict[str, Any]:
    """HTTP ``GET /pulses/{id}/code`` value payload (keeps ``ts``)."""
    return {"code": read_code(pulse_id)}


def code_live(
    pulse_id: str, st: tuple[int, int] | None = ...
) -> dict[str, Any]:
    """``/live`` code frame payload (omits ``ts``); ``code`` null = none."""
    code = None if st is None else read_code(pulse_id)
    if code is not None:
        code.pop("ts", None)
    return {"type": "code", "pulse_id": pulse_id, "code": code}


# --------------------------------------------------------------------------- #
# Events readers (HTTP full-window vs /live delta)
# --------------------------------------------------------------------------- #
def _parse_wire_lines(lines: list[str]) -> list[dict[str, Any]]:
    return events_io.trim_events_for_wire(
        events_io.parse(lines, keep_raw=True)
    )


def events_http(
    pulse_id: str,
    tail: int,
    after: Optional[int] = None,
    *,
    event_id: Optional[str] = None,
    full: bool = False,
) -> dict[str, Any]:
    """HTTP ``GET /pulses/{id}/events`` value payload (full tail window on append)."""
    path = paths.events_path(pulse_id)
    if not path.exists():
        return {"events": [], "count": 0, "offset": 0}
    if event_id:
        read = events_io.read_events_after(
            path, max(tail, EVENTS_TAIL_MAX), None
        )
    else:
        read = events_io.read_events_after(path, tail, after)
    events = events_io.parse(read.pop("events"), keep_raw=True)
    if event_id:
        events = [e for e in events if e.get("id") == event_id]
    elif not full:
        events = events_io.trim_events_for_wire(events)
    read["events"] = events
    read["count"] = len(events)
    return read


def _events_live_tail(pulse_id: str, tail: int) -> dict[str, Any]:
    path = paths.events_path(pulse_id)
    if not path.exists():
        return {
            "type": "events",
            "pulse_id": pulse_id,
            "offset": 0,
            "events": [],
            "count": 0,
            "delta": False,
        }
    try:
        size = path.stat().st_size
    except OSError:
        return {
            "type": "events",
            "pulse_id": pulse_id,
            "offset": 0,
            "events": [],
            "count": 0,
            "delta": False,
        }
    events = _parse_wire_lines(events_io.last_lines(path, tail))
    return {
        "type": "events",
        "pulse_id": pulse_id,
        "offset": size,
        "events": events,
        "count": len(events),
        "delta": False,
    }


def events_live(
    pulse_id: str, tail: int, after: Optional[int] = None
) -> dict[str, Any]:
    """``/live`` events frame payload (delta on growth, replacement on first/shrink/gap)."""
    if after is None:
        return _events_live_tail(pulse_id, tail)
    path = paths.events_path(pulse_id)
    if not path.exists():
        return {
            "type": "events",
            "pulse_id": pulse_id,
            "offset": 0,
            "events": [],
            "count": 0,
            "delta": False,
        }
    try:
        size = path.stat().st_size
    except OSError:
        return {
            "type": "events",
            "pulse_id": pulse_id,
            "offset": 0,
            "events": [],
            "count": 0,
            "delta": False,
        }
    if after < 0 or after > size:
        # Shrink/lost cursor — full replacement window. Never trust the
        # caller's tail here: scrollback sets it to 400/800/1200…, and this
        # snapshot REPLACES the client's window. A fallback capped at
        # EVENTS_TAIL_MAX (2000) once the client is past that would silently
        # delete events it had already loaded.
        return _events_live_tail(pulse_id, max(int(tail), EVENTS_TAIL_MAX))
    if after == size:
        return {
            "type": "events",
            "pulse_id": pulse_id,
            "offset": size,
            "events": [],
            "count": 0,
            "delta": True,
            "unchanged": True,
        }
    lines = events_io.lines_after(path, after)
    if not lines and size > after:
        # Gap too large (or a torn read) — full replacement tail; same
        # untrusted-tail rule as the shrink fallback above.
        return _events_live_tail(pulse_id, max(int(tail), EVENTS_TAIL_MAX))
    events = _parse_wire_lines(lines)
    return {
        "type": "events",
        "pulse_id": pulse_id,
        "offset": size,
        "events": events,
        "count": len(events),
        "delta": True,
    }
