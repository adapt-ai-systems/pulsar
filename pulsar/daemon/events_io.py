"""Bounded ``events.jsonl`` tail readers, line parser, byte cursor, and appender.

One module for reading and appending to per-pulse ``events.jsonl`` logs across
the daemon (activity, handoff, failover, loopguard, live, app).

Callers keep their historical line-splitting and bad-JSON rules via explicit
parameters:
- ``split="newline"`` splits on ``"\\n"`` and drops blank/whitespace-only lines
  (activity / live / HTTP events).
- ``split="lines"`` uses ``str.splitlines()`` (handoff / loopguard).
- ``parse(lines, keep_raw=False)`` skips bad JSON lines, or wraps them as
  ``{"_raw": line}`` when ``keep_raw=True``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from pulsar.daemon import paths

logger = logging.getLogger("pulsar.daemon.events_io")

# How far back from EOF we seek per file by default (64 KiB).
TAIL_WINDOW_BYTES = 64 * 1024

# Ceiling for line-counted and delta tail reads (8 MiB).
TAIL_MAX_BYTES = 8 * 1024 * 1024

# Reading forward from a byte offset: cap one poll's read (4 MiB).
READ_CHUNK_BYTES = 4 * 1024 * 1024


def tail_lines(
    path: Path,
    nbytes: int = TAIL_WINDOW_BYTES,
    *,
    split: str = "newline",
) -> list[str]:
    """Return the complete lines in the last ``nbytes`` bytes of ``path``.

    Seeks from the end and reads at most ``nbytes`` bytes so bytes touched stay
    bounded regardless of file size. If the window starts mid-file, the first
    (partial) line is discarded so callers only ever see complete lines.
    Missing/unreadable files yield ``[]`` (logged).
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("activity tail path=%s stat FAILED err=%s", path, exc)
        return []
    if size == 0:
        return []
    start = max(0, size - nbytes)
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read(nbytes)
    except OSError as exc:
        logger.error("activity tail path=%s read FAILED err=%s", path, exc)
        return []
    text = chunk.decode("utf-8", errors="replace")
    if split == "lines":
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        return lines
    if split == "newline":
        lines = text.split("\n")
        # If we started past byte 0 the first line is almost certainly a fragment of
        # a line whose head we skipped — drop it rather than feed torn JSON.
        if start > 0 and lines:
            lines = lines[1:]
        # Trailing empty from a final newline (or an in-progress last write).
        return [ln for ln in lines if ln.strip()]
    raise ValueError(f"unknown split mode: {split!r}")


def read_tail_lines(path: Path, window: int = TAIL_WINDOW_BYTES) -> list[str]:
    """Backward-compatible alias for ``tail_lines(..., split='newline')``."""
    return tail_lines(path, window, split="newline")


def last_lines(
    path: Path,
    n: int,
    max_bytes: int = TAIL_MAX_BYTES,
) -> list[str]:
    """Return the last ``n`` complete lines of ``path`` (fewer if shorter).

    Same seek-from-EOF trick as :func:`tail_lines`, but sized by line
    count instead of one fixed window: start at ``TAIL_WINDOW_BYTES`` and widen
    only while the window holds fewer lines than asked for, stopping at the
    start of the file or ``max_bytes``. Bytes read stay bounded no matter how
    large the file grows. Missing/unreadable files yield ``[]`` (logged).
    """
    if n < 1:
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("tail path=%s stat FAILED err=%s", path, exc)
        return []
    if size == 0:
        return []
    window = min(TAIL_WINDOW_BYTES, max_bytes)
    while True:
        start = max(0, size - window)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(size - start)
        except OSError as exc:
            logger.error("tail path=%s read FAILED err=%s", path, exc)
            return []
        lines = chunk.decode("utf-8", errors="replace").split("\n")
        # Started past byte 0 → the first line is a fragment; drop it rather
        # than hand back torn JSON.
        if start > 0 and lines:
            lines = lines[1:]
        lines = [ln for ln in lines if ln.strip()]
        if len(lines) >= n or start == 0 or window >= max_bytes:
            return lines[-n:]
        window = min(window * 4, max_bytes)


read_last_lines = last_lines


def lines_after(
    path: Path,
    offset: int,
    max_bytes: int = TAIL_MAX_BYTES,
) -> list[str]:
    """Complete lines whose first byte is at or after ``offset``.

    ``offset`` is a previous file size (the /live byte cursor). Used to push
    only the new jsonl lines instead of re-sending the whole tail window.
    Missing/unreadable files yield ``[]`` (logged). If the gap is larger than
    ``max_bytes`` the caller should fall back to a tail read — this function
    returns ``[]`` in that case so we never pull an unbounded delta.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("delta path=%s stat FAILED err=%s", path, exc)
        return []
    if offset < 0 or offset >= size:
        return []
    gap = size - offset
    if gap > max_bytes:
        logger.info(
            "delta path=%s gap=%d exceeds cap=%d — empty (tail fallback)",
            path, gap, max_bytes,
        )
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(gap)
    except OSError as exc:
        logger.error("delta path=%s read FAILED err=%s", path, exc)
        return []
    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    return [ln for ln in lines if ln.strip()]


read_lines_after = lines_after


def read_events_after(
    path: Path,
    tail: int,
    after: Optional[int],
    max_bytes: int = TAIL_MAX_BYTES,
) -> dict[str, Any]:
    """Cursor read for the pane poll: only the lines appended since ``after``."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("events cursor path=%s stat FAILED err=%s", path, exc)
        return {"events": [], "count": 0, "offset": 0}
    if after is None:
        lines = last_lines(path, tail, max_bytes)
        return {"events": lines, "count": len(lines), "offset": size}
    if after < 0:
        lines = last_lines(path, tail, max_bytes)
        return {"events": lines, "count": len(lines), "offset": size}
    if after == size:
        return {"events": [], "count": 0, "offset": size, "unchanged": True}
    if after > size:
        logger.info(
            "events cursor path=%s SHRANK size=%d after=%d — tail fallback",
            path, size, after,
        )
        lines = last_lines(path, tail, max_bytes)
        return {"events": lines, "count": len(lines), "offset": size}
    lines = last_lines(path, tail, max_bytes)
    return {"events": lines, "count": len(lines), "offset": size}


def parse(
    lines: Iterable[str],
    *,
    keep_raw: bool = False,
) -> list[dict[str, Any]]:
    """Parse JSONL lines into event dicts.

    When ``keep_raw=False`` (default), unparseable lines are skipped.
    When ``keep_raw=True``, unparseable lines become ``{"_raw": line}``.
    """
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if keep_raw:
                out.append({"_raw": line})
    return out


def tail_events(
    pulse_id: str,
    nbytes: int = TAIL_WINDOW_BYTES,
    *,
    split: str = "lines",
) -> list[dict[str, Any]]:
    """Read and parse the last ``nbytes`` of a pulse's ``events.jsonl``."""
    f = paths.events_path(pulse_id)
    if not f.exists():
        return []
    return parse(tail_lines(f, nbytes, split=split), keep_raw=False)


def events_size(pulse_id: str) -> int:
    """Current byte size of the pulse's ``events.jsonl``, or 0 if missing."""
    try:
        return paths.events_path(pulse_id).stat().st_size
    except OSError:
        return 0


class EventCursor:
    """Read ``events.jsonl`` FORWARD from a byte offset taken when a request
    was written."""

    def __init__(self, pulse_id: str, offset: int):
        self.pulse_id = pulse_id
        self.offset = offset
        self.events: list[dict[str, Any]] = []
        self.ends: list[int] = []   # byte offset just past each event's line
        self._partial = b""

    def poll(self) -> list[dict[str, Any]]:
        f = paths.events_path(self.pulse_id)
        try:
            with f.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() < self.offset:     # truncated/rotated: restart
                    self.offset, self._partial = 0, b""
                fh.seek(self.offset)
                raw = fh.read(READ_CHUNK_BYTES)
        except OSError:
            return self.events
        pos = self.offset - len(self._partial)  # where buf starts in the file
        self.offset += len(raw)
        buf = self._partial + raw
        lines = buf.split(b"\n")
        self._partial = lines.pop()             # incomplete last line
        for line in lines:
            pos += len(line) + 1
            try:
                self.events.append(json.loads(line.decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue
            self.ends.append(pos)
        return self.events


def _format_ts(ts_style: str) -> str:
    if ts_style == "iso":
        return datetime.now(timezone.utc).isoformat()
    if ts_style == "strftime_z":
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")
    raise ValueError(f"unknown ts_style: {ts_style!r}")


def append(
    pulse_id_or_path: str | Path,
    event: dict[str, Any],
    *,
    ts_style: str = "iso",
) -> dict[str, Any]:
    """Append one JSON event line to ``events.jsonl`` and return ``event``.

    ``pulse_id_or_path`` may be a pulse id or a filesystem path (``Path`` or
    path string containing ``/`` or ending with ``.jsonl``) so callers that
    receive an ``events_file`` path and callers that pass ``pulse_id`` both
    work. Uses a single ``O_APPEND`` write so concurrent runner appends never
    tear lines.
    """
    event.setdefault("ts", _format_ts(ts_style))
    if isinstance(pulse_id_or_path, Path):
        path = pulse_id_or_path
    else:
        s = str(pulse_id_or_path)
        if "/" in s or s.endswith(".jsonl"):
            path = Path(s)
        else:
            path = paths.events_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(event) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return event


def emit(pulse_id: str, event: dict[str, Any]) -> None:
    """Append one daemon marker event (strftime ``%z`` timestamp), logging
    instead of raising on a write failure — a pane marker must never fail
    the route or loop that writes it."""
    try:
        append(pulse_id, event, ts_style="strftime_z")
    except OSError as exc:
        logger.error("events.emit pulse=%s type=%s FAILED err=%s", pulse_id,
                     event.get("type"), exc)


# --------------------------------------------------------------------------- #
# Wire-level trimming & resumable task-event scan (per-pulse events)
# --------------------------------------------------------------------------- #
# Wire-level cap for tool_result bodies in /live and GET /events. The collapsed
# tool line only needs ~160 chars; tap-to-expand fetches the full event via
# GET /events?event_id=. Durable events.jsonl is never rewritten.
TOOL_RESULT_BODY_MAX = 200

# Task-tool calls the pane folds into the TODO card (web toolcards.foldTodos).
# Lower-case; matched case-insensitively (Claude sends TaskCreate, pi/codex
# send todo_write).
TASK_TOOL_NAMES = frozenset({"todo_write", "todowrite", "update_plan",
                             "taskcreate", "taskupdate"})
TASK_EVENTS_MAX = 5000  # the pane's record reaches the first session
_TASK_RESULT_SNIPPET = 200

# Prefilter byte patterns. Every events.jsonl writer is json.dumps with the
# default separators (runner.emit, events_io.append), and json.dumps
# escapes every quote inside a string, so '"id": ' or '"name": ' only ever
# matches a real key — never text inside a snippet.
_SESSION_LINE = b'"type": "session"'
_TOOL_CALL_LINE = b'"type": "tool_call"'
_TOOL_RESULT_LINE = b'"type": "tool_result"'
_NAME_KEY = b'"name": '
_ID_KEY = b'"id": '
_TASK_TOOL_NAMES_B = frozenset(n.encode() for n in TASK_TOOL_NAMES)
_TASK_SCANS_MAX = 64  # logs whose scan state is kept, least recently used dropped
_TASK_SCAN_SIG = 64   # bytes before the resume offset that must be unchanged


def _key_values(raw: bytes, key: bytes) -> Iterator[Optional[bytes]]:
    """The raw value of every ``key`` on one json.dumps line: the text inside
    the quotes for a string, the bare token otherwise. A string holding an
    escape comes back as None — it can't be compared as bytes, so the caller
    treats the line as a match and lets json.loads decide."""
    i = raw.find(key)
    while i != -1:
        j = i + len(key)
        if raw.startswith(b'"', j):
            k = raw.find(b'"', j + 1)
            if k == -1:
                return
            tok = raw[j + 1:k]
            yield None if b"\\" in tok else tok
        else:
            k = j
            while k < len(raw) and raw[k] not in b",}] \r\n":
                k += 1
            yield raw[j:k]
        i = raw.find(key, k)


class _TaskScan:
    """One log's task-event scan, resumable. events.jsonl is append-only, so
    a repeat call reads only the bytes appended since the last one."""

    __slots__ = ("lock", "ident", "offset", "sig", "cap", "out",
                 "create_ids", "create_tokens")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset(None, TASK_EVENTS_MAX)

    def reset(self, ident: Optional[tuple[int, int]], cap: int) -> None:
        self.ident = ident
        self.offset = 0
        self.sig = b""
        self.cap = cap
        self.out: list[dict[str, Any]] = []
        self.create_ids: set[str] = set()
        self.create_tokens: set[bytes] = set()

    def _wanted(self, raw: bytes) -> bool:
        """Might this line be a task event? A false positive costs one
        json.loads; a false negative would drop an event."""
        if _SESSION_LINE in raw:
            return True
        if _TOOL_CALL_LINE in raw and any(
                v is None or v.lower() in _TASK_TOOL_NAMES_B
                for v in _key_values(raw, _NAME_KEY)):
            return True
        # A TaskCreate result names no tool; it is kept by id.
        return bool(self.create_tokens) and _TOOL_RESULT_LINE in raw and any(
            v is None or v in self.create_tokens for v in _key_values(raw, _ID_KEY))

    def feed(self, raw: bytes) -> None:
        if not self._wanted(raw):
            return
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "session":
            self.out.append({"type": "session", "session_id": ev.get("session_id"),
                             "ts": ev.get("ts")})
        elif t == "tool_call":
            name = str(ev.get("name") or "").lower()
            if name not in TASK_TOOL_NAMES:
                return
            if name == "taskcreate" and ev.get("id") is not None:
                self.create_ids.add(str(ev["id"]))
                self.create_tokens.add(str(ev["id"]).encode())
            self.out.append(ev)
        elif t == "tool_result" and str(ev.get("id")) in self.create_ids:
            ev = dict(ev)
            snip = ev.get("snippet")
            if isinstance(snip, str) and len(snip) > _TASK_RESULT_SNIPPET:
                ev["snippet"] = snip[:_TASK_RESULT_SNIPPET]
            self.out.append(ev)
        if len(self.out) > 2 * self.cap:
            del self.out[:-self.cap]


_task_scans: OrderedDict[str, _TaskScan] = OrderedDict()
_task_scans_lock = threading.Lock()


def read_task_events(path: Path, max_keep: int = TASK_EVENTS_MAX
                     ) -> list[dict[str, Any]]:
    """Every task-tool event in the WHOLE log, oldest first, capped to the
    last ``max_keep``.

    Blocking file IO — call it off the event loop (asyncio.to_thread)."""
    key = str(path)
    with _task_scans_lock:
        scan = _task_scans.get(key)
        if scan is None:
            scan = _task_scans[key] = _TaskScan()
            if len(_task_scans) > _TASK_SCANS_MAX:
                _task_scans.popitem(last=False)
        else:
            _task_scans.move_to_end(key)
    with scan.lock:
        try:
            with path.open("rb") as fh:
                fd = fh.fileno()
                st = os.fstat(fd)
                ident = (st.st_dev, st.st_ino)
                if (ident != scan.ident or max_keep > scan.cap
                        or st.st_size < scan.offset
                        or os.pread(fd, len(scan.sig),
                                    scan.offset - len(scan.sig)) != scan.sig):
                    scan.reset(ident, max(max_keep, TASK_EVENTS_MAX))
                if st.st_size > scan.offset:
                    fh.seek(scan.offset)
                    pos = scan.offset
                    for raw in fh:
                        if not raw.endswith(b"\n"):
                            break  # a write in progress: the next call reads it whole
                        pos += len(raw)
                        scan.feed(raw)
                    scan.offset = pos
                    scan.sig = os.pread(fd, min(pos, _TASK_SCAN_SIG),
                                        pos - min(pos, _TASK_SCAN_SIG))
        except OSError as exc:
            logger.error("task_events path=%s read FAILED err=%s", path, exc)
            scan.reset(None, TASK_EVENTS_MAX)
            return []
        return scan.out[-max_keep:]


def _tool_result_body(ev: dict[str, Any]) -> Optional[str]:
    for key in ("snippet", "result", "output"):
        val = ev.get(key)
        if isinstance(val, str):
            return val
    return None


def trim_event_for_wire(ev: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``ev`` with a long tool_result body clipped. Other kinds pass through."""
    if not isinstance(ev, dict) or ev.get("type") != "tool_result":
        return ev
    body = _tool_result_body(ev)
    if body is None or len(body) <= TOOL_RESULT_BODY_MAX:
        return ev
    out = {k: v for k, v in ev.items() if k not in ("result", "output")}
    out["snippet"] = body[:TOOL_RESULT_BODY_MAX]
    out["truncated"] = True
    return out


def trim_events_for_wire(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trim_event_for_wire(e) for e in events]
