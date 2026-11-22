"""Handoff seeds: the first message of a successor session. The
forced seed (transcript tail), the carry-over block mined from the event log,
and the reader for a note the pulse wrote itself."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from pulsar.daemon import events_io, paths

logger = logging.getLogger("pulsar.daemon.handoff")


# --------------------------------------------------------------------------- #
# Forced handoff: the old model is dead (cap, provider error, expired token)
# and cannot write its own prose. The successor gets the tail of the pane
# transcript verbatim plus the path of the full events.jsonl so it can grep
# for anything older.
# --------------------------------------------------------------------------- #
# Keep the tail bounded so forced seeds don't consume too much of the
# successor's context; the file path in the header covers older history.
FORCED_SEED_TOKENS = 40_000
FORCED_CHARS_PER_TOKEN = 4
FORCED_ARGS_CHARS = 400  # tool-call args are shown truncated
# Tool results in a rendered tail are capped too so large outputs do not bloat
# seeds or slow cold successor TTFT. 400 chars keeps the shape of a result
# (exit status, first lines); the full text is in events.jsonl.
RESULT_CHARS = 400

FORCED_PREFIX = (
    "[pulsar handoff — FORCED] Your predecessor session died{why} and could "
    "not write a handoff. Below is the tail of its transcript (about {tokens} "
    "tokens, oldest first). The FULL transcript is on disk at:\n  {path}\n"
    "It is JSONL, one event per line: user_message (text), assistant_text "
    "(text), tool_call (name, args), tool_result (snippet), done. Read it "
    "with grep/sed/python when you need something older than this tail. "
    "Reconstruct the state, verify what is actually done before redoing "
    "anything, and continue the task; answer the user's next message as if "
    "there was no seam.\n\n===== transcript tail =====\n"
)


def _render_event(ev: dict) -> Optional[str]:
    t = ev.get("type")
    ts = (ev.get("ts") or "")[11:16]
    if t == "user_message":
        reply = (ev.get("reply_to") or {}).get("text")
        if reply:
            return (f"[{ts}] USER (replying to: {reply[:300]}): "
                    f"{ev.get('text') or ''}")
        return f"[{ts}] USER: {ev.get('text') or ''}"
    if t == "assistant_text":
        return f"[{ts}] ASSISTANT: {ev.get('text') or ''}"
    if t == "tool_call":
        try:
            args = json.dumps(ev.get("args") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(ev.get("args"))
        if len(args) > FORCED_ARGS_CHARS:
            args = args[:FORCED_ARGS_CHARS] + "…"
        return f"[{ts}] TOOL {ev.get('name') or '?'} {args}"
    if t == "tool_result":
        snip = ev.get("snippet") or ""
        if len(snip) > RESULT_CHARS:
            snip = snip[:RESULT_CHARS] + "…"
        return f"[{ts}] RESULT{'' if ev.get('ok', True) else ' (error)'}: {snip}" if snip else None
    if t == "runner_error":
        return f"[{ts}] ERROR: {ev.get('message') or ev.get('text') or json.dumps(ev)[:300]}"
    return None


def _transcript_tail(pulse_id: str, budget_tokens: int,
                     end_offset: Optional[int] = None) -> tuple[list[str], int]:
    """Rendered transcript lines (oldest first) fitting ~budget_tokens, read
    from the bytes before ``end_offset`` (default: end of file). Returns the
    lines and their total chars."""
    f = paths.events_path(pulse_id)
    budget = budget_tokens * FORCED_CHARS_PER_TOKEN
    raw = ""
    if f.exists():
        try:
            with f.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell() if end_offset is None else min(end_offset,
                                                                fh.tell())
                start = max(0, size - budget * 3)  # JSON overhead + dropped lines
                fh.seek(start)
                raw = fh.read(size - start).decode("utf-8", errors="replace")
                if start > 0:
                    raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        except OSError as exc:
            logger.error("handoff.tail pulse=%s read FAILED err=%s", pulse_id, exc)
    lines: list[str] = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = _render_event(ev)
        if r:
            lines.append(r)
    # Trim from the OLDEST until the tail fits the budget.
    total = sum(len(x) + 1 for x in lines)
    while lines and total > budget:
        total -= len(lines[0]) + 1
        lines.pop(0)
    return lines, total


def forced_seed(pulse_id: str, stop_reason: Optional[str] = None,
                budget_tokens: int = FORCED_SEED_TOKENS) -> str:
    """The successor's first message for a forced handoff: header + the
    newest ~budget_tokens of transcript. Reads only the tail of the file."""
    f = paths.events_path(pulse_id)
    lines, total = _transcript_tail(pulse_id, budget_tokens)
    body = "\n".join(lines) if lines else "(no transcript found)"
    why = f" ({stop_reason})" if stop_reason else ""
    return FORCED_PREFIX.format(why=why, tokens=f"{total // FORCED_CHARS_PER_TOKEN:,}",
                                path=str(f)) + body


# --------------------------------------------------------------------------- #
# Carry-over block (handoff review 2026-10-02, proposal 3): facts the daemon
# can pull from the event log without the model having to remember them —
# where the full log is, what the human last said, the open task list, the
# files touched. Appended to EVERY seed. Mechanical, so it can be stale:
# labeled "from the log, verify".
# --------------------------------------------------------------------------- #
CARRY_TOKENS = 3_000
CARRY_SCAN_BYTES = 8 * 1024 * 1024
CARRY_HUMAN_MSGS = 3
CARRY_HUMAN_CHARS = 1_500
CARRY_MAX_FILES = 30

_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit",
               "search_replace", "write_file", "edit_file", "str_replace"}
_DONE_STATUSES = {"completed", "complete", "done", "deleted", "cancelled",
                  "canceled"}


def _session_events(events: list[dict]) -> list[dict]:
    """Events since this session's own seed (the last handoff-seed turn)."""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.get("type") == "user_message" and \
                str(ev.get("msg_file") or "").startswith("handoff-seed"):
            return events[i:]
    return events


def _is_human(ev: dict) -> bool:
    """The user's own messages. Events that carry ``origin`` are human when
    ``origin == "user"``. Older events fall back to the file name: plain
    timestamp-named inbox files (daemon writers use watch-/sched-/handoff-…
    prefixes). Both are minus pulse-to-pulse messages, found by the text."""
    if ev.get("type") != "user_message":
        return False
    text = str(ev.get("text") or "")
    if text.startswith(("[message from", "[pulsar")):
        return False
    origin = ev.get("origin")
    if origin:
        return origin == "user"
    return str(ev.get("msg_file") or "")[:1].isdigit()


def _edited_paths(ev: dict) -> list[str]:
    name, args = ev.get("name"), ev.get("args") or {}
    if not isinstance(args, dict):
        return []
    if name in _EDIT_TOOLS:
        p = args.get("file_path") or args.get("path") or args.get("notebook_path")
        return [str(p)] if p else []
    if name == "file_change":
        return [str(c.get("path")) for c in args.get("changes") or []
                if isinstance(c, dict) and c.get("path")]
    if name == "apply_patch":
        patch = str(args.get("input") or args.get("patch") or "")
        return re.findall(r"(?m)^\*\*\* (?:Add|Update) File: (.+)$", patch)
    return []


def _open_tasks(events: list[dict]) -> list[str]:
    """The task list as the task tools left it: claude TaskCreate/TaskUpdate
    (ids are per-session sequence numbers), todo_write/TodoWrite lists, and
    codex update_plan."""
    tasks: dict[str, dict] = {}
    seq = 0
    for ev in events:
        if ev.get("type") != "tool_call":
            continue
        name, args = ev.get("name"), ev.get("args") or {}
        if not isinstance(args, dict):
            continue
        if name == "TaskCreate":
            seq += 1
            tasks[str(seq)] = {"text": args.get("subject") or "",
                               "status": "pending"}
        elif name == "TaskUpdate":
            t = tasks.setdefault(str(args.get("taskId")),
                                 {"text": "", "status": "pending"})
            if args.get("subject"):
                t["text"] = args["subject"]
            if args.get("status"):
                t["status"] = args["status"]
        elif name in ("todo_write", "TodoWrite", "update_plan"):
            items = args.get("todos") or args.get("plan") or []
            if not args.get("merge"):
                tasks = {}
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                key = str(it.get("id") or f"{name}-{i}")
                t = tasks.setdefault(key, {"text": "", "status": "pending"})
                text = it.get("content") or it.get("step") or it.get("subject")
                if text:
                    t["text"] = text
                if it.get("status"):
                    t["status"] = it["status"]
    return [f"[{t['status']}] {t['text']}" for t in tasks.values()
            if t["text"] and str(t["status"]).lower() not in _DONE_STATUSES]


def carry_over_block(pulse_id: str, budget_tokens: Optional[int] = None) -> str:
    budget = (CARRY_TOKENS if budget_tokens is None else budget_tokens) \
        * FORCED_CHARS_PER_TOKEN
    events = _session_events(events_io.tail_events(pulse_id, CARRY_SCAN_BYTES))
    home = paths.pulse_home(pulse_id)
    head = (
        "\n\n---\nCarry-over from the event log (built by the daemon, not "
        "your predecessor — from the log, verify before relying on it):\n"
        f"- Full transcript: {paths.events_path(pulse_id)} (JSONL: "
        "user_message, assistant_text, tool_call, tool_result, done — "
        "grep it for anything older)\n"
        f"- Earlier handoff notes: {home / 'handoffs'}/<n>.md\n")
    tasks = _open_tasks(events)
    files: list[str] = []
    for ev in events:
        if ev.get("type") == "tool_call":
            for f in _edited_paths(ev):
                if f in files:
                    files.remove(f)
                files.append(f)
    files = files[-CARRY_MAX_FILES:]
    humans = [ev for ev in events if _is_human(ev)][-CARRY_HUMAN_MSGS:]

    def render(n_humans: int) -> str:
        out = head
        if tasks:
            out += ("Open tasks: (re-create these with your task tool first — the "
                    "list is the chat's permanent record and continues, it does "
                    "not restart)\n" + "".join(f"- {t}\n" for t in tasks[-20:]))
        if files:
            out += "Files written/edited this session:\n" + \
                "".join(f"- {f}\n" for f in files)
        hs = humans[len(humans) - n_humans:] if n_humans else []
        if hs:
            out += "Last messages from the user (verbatim, oldest first):\n"
            for ev in hs:
                text = str(ev.get("text") or "")
                if len(text) > CARRY_HUMAN_CHARS:
                    text = text[:CARRY_HUMAN_CHARS] + "…"
                out += f"[{str(ev.get('ts') or '')[:16]}] {text}\n"
        return out

    for n in range(len(humans), -1, -1):
        out = render(n)
        if len(out) <= budget:
            return out
    return out[:budget] + "…\n"


NOTE_MIN_CHARS = 200          # a supplied note shorter than this is refused


def read_note_file(note_path: str) -> str:
    """Validate and read a pulse-supplied handoff note. Raises ValueError
    with a reason the endpoint returns to the caller."""
    p = Path(note_path)
    if not p.is_absolute():
        raise ValueError("note_path must be an absolute path")
    try:
        text = p.read_text(errors="replace").strip()
    except OSError as exc:
        raise ValueError(f"cannot read note_path: {exc}") from exc
    if len(text) < NOTE_MIN_CHARS:
        raise ValueError(f"note is {len(text)} chars — too short to be a "
                         f"handoff (min {NOTE_MIN_CHARS})")
    return text

# The monitor (one tick; the loop is loops.run_every, started by the lifespan table)
