"""Bookmarks: "what happened since I left".

The user presses the pane's bookmark button before stepping away. The pulse
stores ``settings.bookmark = {"at", "offset"}`` (the time, and the byte size
of events.jsonl at that moment). When they come back and press it again, the
daemon writes a compact digest of every event appended since that offset,
messages the pulse to summarize only that period, and sets a new bookmark at
"now" — so each press after the first is "summary since the last press".

The digest is a file, not the agent's memory: a handoff while the user was
away empties the context, but the events log covers the whole period.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulsar.daemon import events_io, mailbox, paths
from pulsar.daemon.timeutil import now_iso, parse_iso

logger = logging.getLogger("pulsar.daemon")

# Per-line clips so one huge reply or tool arg can't swamp the digest.
USER_MAX = 1500
ASSISTANT_MAX = 3000
TOOL_MAX = 200
# Whole-digest cap. Over it, the middle is dropped (head + tail kept) and the
# agent is pointed at the raw byte range.
DIGEST_MAX_CHARS = 150_000

# Daemon/runner marker events worth a line in the digest.
MARKER_TYPES = ("runner_error", "failover", "context_reset", "pulse_stopped",
                "handoff", "handoff_done", "fence_breach", "spawned")


def owner_tz(store=None) -> tuple[tzinfo, str]:
    """Resolve the owner's timezone from ``owner.timezone``, else machine local."""
    if store is not None:
        raw = store.get_setting("owner.timezone")
        if isinstance(raw, str) and raw.strip():
            name = raw.strip()
            try:
                return ZoneInfo(name), name
            except ZoneInfoNotFoundError:
                logger.warning("bookmark: unknown owner.timezone %r; using local", name)
    local_now = datetime.now().astimezone()
    tz = local_now.tzinfo or timezone.utc
    label = local_now.tzname() or "local"
    return tz, label


def fmt_time(
    ts: Optional[str],
    with_date: bool = True,
    tz: Optional[tzinfo] = None,
) -> str:
    """'2:15 PM UTC' (optionally 'Sun Nov 8, 2:15 PM UTC'); '?' when unparseable."""
    dt = parse_iso(ts) if ts else None
    if dt is None and ts:
        try:   # runner stamps like 2026-11-08T04:27:41-0800
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            dt = None
    if dt is None:
        return "?"
    local = dt.astimezone(tz) if tz is not None else dt.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    abbr = local.tzname() or ""
    suffix = f" {abbr}" if abbr else ""
    if with_date:
        return f"{local.strftime('%a %b')} {local.day}, {clock}{suffix}"
    return f"{clock}{suffix}"


def ago(ts: str, now: Optional[datetime] = None) -> str:
    dt = parse_iso(ts)
    if dt is None:
        return "?"
    secs = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 48:
        return f"{hours}h {mins}m"
    return f"{hours // 24}d {hours % 24}h"


def _clip(text: Any, n: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + " …[clipped]"


def _one_line(text: Any, n: int) -> str:
    return _clip(" ".join(str(text or "").split()), n)


def digest_line(ev: dict[str, Any], tz: Optional[tzinfo] = None) -> Optional[str]:
    """One event -> one digest entry, or None for noise (usage, metrics,
    reasoning, ok tool results)."""
    t = ev.get("type")
    when = fmt_time(ev.get("ts"), with_date=False, tz=tz)
    if t == "user_message":
        text = ev.get("display_text") or ev.get("text")
        if ev.get("origin") == "peer":
            who = f"message from pulse '{ev.get('from_title') or ev.get('from_pulse_id')}'"
        elif ev.get("origin") == "user":
            who = "the user"
        else:
            who = f"message ({ev.get('origin') or 'system'})"
        return f"\n[{when}] {who}:\n{_clip(text, USER_MAX)}\n"
    if t == "assistant_text":
        return f"\n[{when}] agent:\n{_clip(ev.get('text'), ASSISTANT_MAX)}\n"
    if t == "tool_call":
        desc = ev.get("description")
        if not desc or desc == "None":
            desc = ev.get("args")
        return f"[{when}]   tool {ev.get('name')}: {_one_line(desc, TOOL_MAX)}"
    if t == "tool_result":
        if ev.get("ok") is False or ev.get("ok") == "False":
            return f"[{when}]   ✗ failed: {_one_line(ev.get('snippet'), TOOL_MAX)}"
        return None
    if t == "bookmark":
        return f"[{when}] (bookmark)"
    if t in MARKER_TYPES:
        extra = {k: v for k, v in ev.items() if k not in ("type", "ts")}
        return f"[{when}] [{t}] {_one_line(json.dumps(extra, default=str), TOOL_MAX)}"
    return None


def build_digest(
    events_file: Path,
    start: int,
    end: int,
    tz: Optional[tzinfo] = None,
) -> tuple[str, int]:
    """Digest of the events in byte range [start, end). Returns (text, count
    of events read). Streams line by line — the range can be many MB."""
    out: list[str] = []
    count = 0
    try:
        with events_file.open("rb") as fh:
            fh.seek(start)
            pos = start
            for raw in fh:
                pos += len(raw)
                if pos > end:
                    break
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                count += 1
                line = digest_line(ev, tz=tz)
                if line is not None:
                    out.append(line)
    except OSError as exc:
        logger.error("bookmark digest read %s FAILED err=%s", events_file, exc)
    text = "\n".join(out)
    if len(text) > DIGEST_MAX_CHARS:
        head = DIGEST_MAX_CHARS // 4
        tail = DIGEST_MAX_CHARS - head
        text = (text[:head]
                + "\n\n…[middle of the period omitted — digest too long; read "
                  "the raw log byte range for it]…\n\n"
                + text[-tail:])
    return text, count


def summary_prompt(
    since: str,
    digest_path: Path,
    events_file: Path,
    start: int,
    end: int,
    count: int,
    tz: Optional[tzinfo] = None,
) -> str:
    return (
        f"[Bookmark] The user is back. They set a bookmark at {fmt_time(since, tz=tz)} "
        f"({ago(since)} ago) before stepping away. Give them a summary of "
        "what happened in this pulse SINCE THAT BOOKMARK — only that period, "
        "not earlier work.\n\n"
        "Cover: what got done, what is still running, what failed or is "
        "blocked, messages from other pulses, and anything that needs their "
        "decision. Put what needs them first. Keep it short and give "
        "evidence (commit hashes, paths, URLs) where you have it. If "
        "nothing happened, say so in one line.\n\n"
        "Your context may not cover the whole period (a handoff empties it), "
        f"so read the digest of the period first: {digest_path} "
        f"({count} events). Raw log if you need detail: {events_file}, "
        f"bytes {start}–{end} (e.g. `tail -c +{start + 1} {events_file} | "
        f"head -c {end - start}`)."
    )


def press(store, pulse) -> dict[str, Any]:
    """The bookmark button. No bookmark yet: set one. Otherwise: digest the
    period since it, queue the summary request, and set a new bookmark."""
    events_file = paths.events_path(pulse.id)
    end = events_io.events_size(pulse.id)
    now = now_iso()
    prev = (pulse.settings or {}).get("bookmark")
    summary = None
    if isinstance(prev, dict) and prev.get("at"):
        tz, tz_label = owner_tz(store)
        start = int(prev.get("offset") or 0)
        if start > end:   # log shrank/rotated: digest it all
            start = 0
        text, count = build_digest(events_file, start, end, tz=tz)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ddir = paths.pulse_home(pulse.id) / "bookmarks"
        ddir.mkdir(parents=True, exist_ok=True)
        dpath = ddir / f"digest-{stamp}.md"
        header = (f"# Pulse activity since bookmark\n\n"
                  f"From {fmt_time(prev['at'], tz=tz)} to {fmt_time(now, tz=tz)} "
                  f"({count} events, bytes {start}–{end} of {events_file}).\n"
                  f"Times are in {tz_label}. Tool calls are one line each; "
                  f"successful tool results are omitted.\n")
        dpath.write_text(header + "\n" + (text or "(no activity)") + "\n")
        prompt = summary_prompt(prev["at"], dpath, events_file, start, end, count, tz=tz)
        display = f"🔖 What happened since my bookmark at {fmt_time(prev['at'], tz=tz)}?"
        # write_first + wake: a stopped pulse is respawned to answer — the
        # press IS the user asking for the summary.
        queued = mailbox.deliver(
            store, pulse, prompt, on_archived="raise", write_first=True,
            extra={"display_text": display,
                   **mailbox.origin_keys("user", "bookmark")})
        store.touch_input(pulse.id)
        summary = {"since": prev["at"], "digest": str(dpath),
                   "events": count, "queued": queued}
    bookmark = {"at": now, "offset": end}
    updated = store.update_settings(pulse.id, {"bookmark": bookmark})
    # Pane divider. Written AFTER the offset was taken, so it opens the next
    # period's digest rather than closing this one.
    events_io.emit(pulse.id, {"type": "bookmark", "at": now,
                              "summary_since": summary["since"] if summary else None})
    logger.info("bookmark pulse=%s set at=%s summary=%s", pulse.id, now,
                bool(summary))
    return {"bookmark": bookmark, "summary": summary, "pulse": updated}


def clear(store, pulse):
    """Double-tap on the button: drop the bookmark, no summary."""
    had = bool((pulse.settings or {}).get("bookmark"))
    updated = store.update_settings(pulse.id, {"bookmark": None})
    if had:
        events_io.emit(pulse.id, {"type": "bookmark", "cleared": True})
    logger.info("bookmark pulse=%s cleared had=%s", pulse.id, had)
    return updated
