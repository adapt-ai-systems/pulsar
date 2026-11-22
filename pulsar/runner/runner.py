"""The Pulsar runner: one dumb, detached babysitter per pulse.

HARD RULE: the runner stays dumb forever. It spawns the
vessel CLI, feeds it messages from the inbox, appends normalized events to
events.jsonl, heartbeats, and answers hello/status/stop on its socket. All
product logic lives in the daemon, which restarts for free.

Steering (restored 2026-07-27): while a turn is running, new
inbox messages — the human's AND the daemon's handoff request — are injected
INTO the live turn between tool calls, not parked behind it. Mechanical, so
still dumb: the runner never reads the message, it just delivers it sooner.

Run as:  python -m pulsar.runner --pulse-id X --home data/pulses/X \
             --model claude-opus-5 --vessel claude-code
The daemon wraps this in `systemd-run --user --scope
-p MemoryMax=... -p TasksMax=... --unit pulsar-pulse-<id>` — the memory fence.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import sys
import time

from pulsar.fsutil import atomic_write_json
from pulsar.harness_overrides import read_overrides
from pulsar.runner.meter import CallMeter
from pathlib import Path

from pulsar.runner.protocol import (
    PROTOCOL_V,
    STATE_BUSY,
    STATE_DEAD_CLI,
    STATE_IDLE,
    STATE_STARTING,
    STATE_STOPPED,
    STATE_STOPPING,
)

GUARD_BIN_DIR = Path(__file__).resolve().parents[2] / "ops" / "guard-bin"

log = logging.getLogger("pulsar.runner")

HEARTBEAT_EVERY_S = 5.0
INBOX_POLL_S = 1.0
# A steer pi ACCEPTED can still sit undelivered forever: pi delivers queued
# steers only before the NEXT LLM call, and a wedged tool call (hung ssh,
# dead tunnel) means that call never comes (seen live 2026-09-18: a
# 30-min wedge ate three steers, each ack looking delivered). When a turn
# stays silent this long after a steer was accepted, the runner forces the
# message through: abort the running bash tool first (turn + context kept,
# steer then delivered), full turn interrupt as the last resort.
STEER_NUDGE_S = float(os.environ.get("PULSAR_STEER_NUDGE_S", "300"))
# Sender keys the daemon stamps into inbox messages (daemon/mailbox.py).
_ORIGIN_KEYS = ("origin", "via", "from_pulse_id", "from_title")


def cgroup_oom_kills() -> int:
    """The kernel's oom_kill counter for the cgroup this runner lives in.

    >0 means the memory fence fired on some process in this pulse's tree.
    Returns 0 (with a log line) when unreadable — never raises.
    """
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        events = Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.events"
        for line in events.read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
        return 0
    except Exception as e:
        log.warning("could not read cgroup memory.events: %s", e)
        return 0


# done{reason: "error"} subtypes the vessel bridges mint for THEIR OWN parse
# failures (vessel/parse.py, claude.py, codex.py) — not a provider error.
LOCAL_ERROR_SUBTYPES = frozenset({"malformed_record", "record_classification",
                                  "stdout_stream", "non_object_event"})


def requeue_message(claimed: Path, dest: Path) -> None:
    """Un-claim a message back into ``inbox/new/`` (at-least-once: a message
    that was claimed — fed, injected, or swept out of a dead runner's
    ``feeding/`` — goes back to the queue unchanged)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    claimed.replace(dest)


class Runner:
    # Turn-progress clock (monotonic): every streamed vessel event refreshes
    # it; the steer nudge reads it to tell a live turn from a silent wedge.
    _turn_progress_ts: float = 0.0

    def __init__(self, pulse_id: str, home: Path, model: str, vessel: str,
                 resume_session: str | None, effort: str | None = None,
                 fast: bool = False):
        self.pulse_id = pulse_id
        self.home = home
        self.model = model
        self.vessel_kind = vessel
        self.resume_session = resume_session
        self.effort = effort
        self.fast = fast
        self.state = STATE_STARTING
        self.stop_reason: str | None = None
        # The provider error that ended the current/last turn (pi 429 usage
        # limit, 400 ...). Shown as the heartbeat reason while no stop_reason
        # is set; cleared when the next turn starts.
        self.turn_error: str | None = None
        # Consecutive turns that ended on a provider/API error, and the inbox
        # file the last of them was fed. Published in the heartbeat; the
        # daemon's failover monitor acts on 2 in a row (daemon/failover.py).
        self.provider_error_turns = 0
        self.provider_error_msg: str | None = None
        self._turn_provider_error = False
        self._turn_interrupted = False   # a turn interrupt was sent this turn
        # Per-pulse harness overrides (harness-overrides.json), re-read at
        # every turn start so a changed file applies on the next turn.
        self._turn_overrides: dict = {}
        self.vessel = None          # set in run()
        self.cli_session_id = resume_session
        # Latest running context total from the vessel's usage events. The
        # runner does NOT act on it (dumb rule) — it just surfaces the number
        # in the heartbeat so the daemon's handoff monitor can decide.
        self.last_ctx_tokens: int | None = None
        # Loop triggers (v25): monotonic per-session counters the daemon's
        # scheduler reads off the heartbeat for 'every N replies/tool calls'.
        self.replies_total = 0
        self.tool_calls_total = 0
        # Epoch seconds the current busy turn started — the UI's "thinking · Ns"
        # source (status honesty). None whenever not busy.
        self.busy_since: float | None = None
        self._stopping = asyncio.Event()
        self._events_fh = None
        self._oom_baseline = cgroup_oom_kills()
        # Streaming mode: assistant_delta events accumulate here and land in
        # the ephemeral partial.json (throttled atomic replace), NEVER in
        # events.jsonl — the durable log stays one line per finished block.
        self._partial_text = ""
        self._partial_written = 0.0
        # Reasoning trace (reasoning_delta events) accumulates here and lands
        # in the ephemeral reasoning.json, read by the pane's reasoning panel.
        # It survives the turn boundary (so "last turn" is still readable) and
        # is cleared when the next turn starts, when the runner starts, and
        # when it exits. The whole-turn trace is never in events.jsonl; each
        # finished thinking block is (thinking cards in the chat, in order
        # with replies and tool calls) — see _flush_reasoning_block. `_reasoning_block` is the not-yet-flushed
        # tail of the trace, also written to reasoning.json as "block" so the
        # pane's live thinking ghost shows only what has no card yet.
        self._reasoning_text = ""
        self._reasoning_block = ""
        # Untruncated copy of the current thinking block for traces.jsonl
        # (the reasoning-trace archive; see _flush_reasoning_block).
        self._trace_block = ""
        self._reasoning_written = 0.0
        # Live code (code_delta events): the argument JSON of the tool call
        # being generated lands in the ephemeral code.json, read by the
        # pane's live code card. Cleared when that tool_call (or the turn)
        # arrives, so the durable card replaces the ghost.
        self._code_text = ""
        self._code_name: str | None = None
        self._code_id: str | None = None
        self._code_written = 0.0
        self._meter = CallMeter()  # per-call speed → 'metrics' events

    # ---------- streaming partials ----------

    PARTIAL_WRITE_EVERY_S = 0.1  # near-real-time stream; was 0.3

    def _on_hidden_delta(self, text: str) -> None:
        """Thinking / tool-call tokens: timed by the speed meter, never shown
        in the partial (pi streams both; see pi.classify_record)."""
        closed = self._meter.delta(text)
        if closed is not None:
            self.emit(closed)

    def _on_delta(self, text: str) -> None:
        closed = self._meter.delta(text)
        if closed is not None:
            self.emit(closed)
        self._partial_text += text
        now = time.monotonic()
        if now - self._partial_written < self.PARTIAL_WRITE_EVERY_S:
            return
        self._partial_written = now
        try:
            atomic_write_json(
                self.home / "partial.json",
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "text": self._partial_text})
        except OSError as e:
            # Display-only surface: log and keep the turn alive.
            log.warning("pulse=%s partial.json write failed: %s",
                        self.pulse_id, e)

    def _clear_partial(self) -> None:
        self._partial_text = ""
        self._partial_written = 0.0
        try:
            (self.home / "partial.json").unlink(missing_ok=True)
        except OSError as e:
            log.warning("pulse=%s partial.json unlink failed: %s",
                        self.pulse_id, e)

    # A tool call's argument JSON past this is not appended: code.json is
    # rewritten every PARTIAL_WRITE_EVERY_S, and a multi-megabyte Write
    # should not be. The card says it stopped ("truncated").
    CODE_MAX_CHARS = 400_000

    def _on_code_delta(self, ev: dict) -> None:
        """A tool call's argument JSON streaming in: metered (generated
        output) and accumulated into code.json for the live code card. A
        chunk carrying a new id starts a new call."""
        text = ev.get("text") or ""
        if text:
            closed = self._meter.delta(text)
            if closed is not None:
                self.emit(closed)
        cid = ev.get("id")
        if cid and cid != self._code_id:
            self._code_text = ""
            self._code_id = cid
            self._code_name = None
        if ev.get("name") and not self._code_name:
            self._code_name = ev.get("name")
        if len(self._code_text) < self.CODE_MAX_CHARS:
            self._code_text += text[:self.CODE_MAX_CHARS - len(self._code_text)]
        self._write_code()

    def _write_code(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._code_written < self.PARTIAL_WRITE_EVERY_S:
            return
        self._code_written = now
        try:
            atomic_write_json(
                self.home / "code.json",
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "id": self._code_id, "name": self._code_name,
                 "text": self._code_text,
                 "truncated": len(self._code_text) >= self.CODE_MAX_CHARS})
        except OSError as e:
            log.warning("pulse=%s code.json write failed: %s",
                        self.pulse_id, e)

    def _clear_code(self) -> None:
        self._code_text = ""
        self._code_name = None
        self._code_id = None
        self._code_written = 0.0
        try:
            (self.home / "code.json").unlink(missing_ok=True)
        except OSError as e:
            log.warning("pulse=%s code.json unlink failed: %s",
                        self.pulse_id, e)

    # Bounded display buffer: a long turn's reasoning is kept as the newest
    # N chars. Generous enough that truncation is rare; the marker says so
    # rather than silently dropping the head.
    REASONING_MAX_CHARS = 200_000
    # Per-block cap for the durable `reasoning` event. events.jsonl is
    # append-only for the pulse's life and every pane tails it, so one
    # thinking block must never dump the whole 200k trace in there; the
    # panel keeps the full live trace.
    REASONING_EVENT_MAX_CHARS = 20_000
    _REASONING_TRUNC = "…[earlier reasoning truncated]\n"
    # A vessel event that ends a thinking block: the model moved on to prose,
    # a tool call, or the turn ended. The block is flushed BEFORE the closer
    # is emitted so the card sits above what it led to.
    REASONING_BLOCK_CLOSERS = frozenset(
        ("assistant_text", "tool_call", "done", "runner_error"))

    @classmethod
    def _tail_trunc(cls, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        body = text[-limit:]
        if not body.startswith(cls._REASONING_TRUNC):
            body = cls._REASONING_TRUNC + body
        return body

    def _on_reasoning_delta(self, text: str) -> None:
        """The model's reasoning trace: timed by the speed meter (it is
        generated output), accumulated into the ephemeral reasoning.json for
        the pane's reasoning panel, and into the current block for the
        durable per-block `reasoning` event (_flush_reasoning_block)."""
        closed = self._meter.delta(text)
        if closed is not None:
            self.emit(closed)
        self._reasoning_text = self._tail_trunc(
            self._reasoning_text + text, self.REASONING_MAX_CHARS)
        self._reasoning_block = self._tail_trunc(
            self._reasoning_block + text, self.REASONING_EVENT_MAX_CHARS)
        self._trace_block = self._tail_trunc(
            self._trace_block + text, self.REASONING_MAX_CHARS)
        self._write_reasoning()

    def _write_reasoning(self, force: bool = False) -> None:
        """Throttled atomic replace of reasoning.json: "text" is the whole
        turn's trace (the panel), "block" the part with no card yet (the
        pane's live thinking ghost)."""
        now = time.monotonic()
        if not force and now - self._reasoning_written < self.PARTIAL_WRITE_EVERY_S:
            return
        self._reasoning_written = now
        try:
            atomic_write_json(
                self.home / "reasoning.json",
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "text": self._reasoning_text,
                 "block": self._reasoning_block})
        except OSError as e:
            log.warning("pulse=%s reasoning.json write failed: %s",
                        self.pulse_id, e)

    def _flush_reasoning_block(self) -> None:
        """A thinking block ended: write it to events.jsonl as one durable
        `reasoning` event (the chat's collapsed thinking card) and drop it
        from the live block. The whole-turn trace in reasoning.json is left
        alone — the panel keeps following it."""
        block = self._reasoning_block
        trace = self._trace_block
        self._reasoning_block = ""
        self._trace_block = ""
        if not block:
            return
        if trace.strip():
            self._append_trace(trace)
        # Whitespace-only thinking (qwen-in-pi pads tool calls with newlines)
        # is dropped, not carded — but still cleared from the ghost.
        if block.strip():
            self.emit({"type": "reasoning", "text": block})
        # Forced so the ghost retires the moment its card exists, not up to a
        # throttle window later.
        self._write_reasoning(force=True)

    def _append_trace(self, text: str) -> None:
        """Reasoning-trace archive: preserve every model's reasoning.
        One line per finished thinking block in traces.jsonl,
        NOT capped at REASONING_EVENT_MAX_CHARS like the chat card, stamped
        with the model and vessel so a corpus builder can pick local-model
        traces without joining the daemon record. Failures are logged only."""
        try:
            with (self.home / "traces.jsonl").open("a") as fh:
                fh.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "model": self.model, "vessel": self.vessel_kind,
                    "chars": len(text), "text": text}) + "\n")
        except OSError as e:
            log.warning("pulse=%s traces.jsonl write failed: %s", self.pulse_id, e)

    def _emit_vessel(self, ev: dict) -> None:
        """Emit a durable vessel event, flushing any open thinking block
        first when the event closes it."""
        if ev.get("type") in self.REASONING_BLOCK_CLOSERS:
            self._flush_reasoning_block()
        self.emit(ev)

    def _clear_reasoning(self) -> None:
        self._reasoning_text = ""
        self._reasoning_block = ""
        self._reasoning_written = 0.0
        try:
            (self.home / "reasoning.json").unlink(missing_ok=True)
        except OSError as e:
            log.warning("pulse=%s reasoning.json unlink failed: %s",
                        self.pulse_id, e)

    # ---------- durable surfaces ----------

    def heartbeat(self) -> None:
        beat = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "pid": os.getpid(),
            "cli_pid": getattr(self.vessel, "pid", None),
            "state": self.state,
            "reason": self.stop_reason or self.turn_error,
            "cli_session_id": self.cli_session_id,
            "last_ctx_tokens": self.last_ctx_tokens,
            "replies_total": self.replies_total,
            "tool_calls_total": self.tool_calls_total,
            "busy_since": self.busy_since,
            "provider_error_turns": self.provider_error_turns,
            "provider_error_msg": self.provider_error_msg,
            "protocol": PROTOCOL_V,
        }
        atomic_write_json(self.home / "heartbeat.json", beat)

    def emit(self, event: dict) -> None:
        # Per-call speed (meter.py): a boundary event closes a call and the
        # metrics line is written just before it.
        metrics = self._meter.observe(event)
        if metrics is not None:
            metrics.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            self._events_fh.write(json.dumps(metrics) + "\n")
        event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self._events_fh.write(json.dumps(event) + "\n")
        self._events_fh.flush()

    def set_state(self, state: str) -> None:
        if state != self.state:
            log.info("pulse=%s state %s -> %s", self.pulse_id, self.state, state)
            if state == STATE_BUSY:
                self.busy_since = time.time()
            else:
                self.busy_since = None
            self.state = state
            self.heartbeat()

    # ---------- socket server ----------

    async def handle_conn(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        peer_op = "?"
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(line)
            peer_op = req.get("op", "?")
            if req.get("v") != PROTOCOL_V:
                log.warning("pulse=%s refused op=%s: protocol %r != %d",
                            self.pulse_id, peer_op, req.get("v"), PROTOCOL_V)
                reply = {"v": PROTOCOL_V, "ok": False,
                         "error": "protocol_version_mismatch",
                         "runner_protocol": PROTOCOL_V}
            elif peer_op in ("hello", "status"):
                reply = {"v": PROTOCOL_V, "ok": True, "pulse_id": self.pulse_id,
                         "pid": os.getpid(),
                         "cli_pid": getattr(self.vessel, "pid", None),
                         "state": self.state,
                         "busy_since": self.busy_since,
                         "cli_session_id": self.cli_session_id}
            elif peer_op == "stop":
                log.info("pulse=%s stop requested via socket", self.pulse_id)
                reply = {"v": PROTOCOL_V, "ok": True}
                self._stopping.set()
            elif peer_op == "interrupt":
                # Abort the in-flight turn only — the pulse stays up. Dumb
                # rule holds: the runner relays the request; the vessel owns
                # whether/how a turn can be aborted.
                if not getattr(self.vessel, "can_interrupt", False):
                    log.warning("pulse=%s interrupt refused: vessel has no "
                                "turn interrupt", self.pulse_id)
                    reply = {"v": PROTOCOL_V, "ok": False,
                             "error": "not_interruptible"}
                elif self.state != STATE_BUSY:
                    log.info("pulse=%s interrupt no-op (state=%s)",
                             self.pulse_id, self.state)
                    reply = {"v": PROTOCOL_V, "ok": True, "noop": True}
                else:
                    log.info("pulse=%s interrupt requested via socket",
                             self.pulse_id)
                    self._turn_interrupted = True
                    await self.vessel.interrupt()
                    reply = {"v": PROTOCOL_V, "ok": True}
            else:
                log.warning("pulse=%s unknown op=%r", self.pulse_id, peer_op)
                reply = {"v": PROTOCOL_V, "ok": False, "error": "unknown_op"}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except Exception:
            log.exception("pulse=%s socket op=%s failed", self.pulse_id, peer_op)
        finally:
            writer.close()

    # ---------- inbox ----------

    @staticmethod
    def _msg_order(path: Path) -> tuple[int, str]:
        # Handoff traffic (request + successor seed) outranks queued user
        # messages — its whole point is to beat the context clock, and the
        # plain lexical sort put "handoff-…" BEHIND every timestamp name
        # (2026-07-27 post-mortem: the request queued behind a test run).
        return (0 if path.name.startswith("handoff") else 1, path.name)

    @staticmethod
    def _is_handoff(path: Path) -> bool:
        """Is this message the daemon's handoff wrap-up request?

        Same ``handoff-`` prefix _msg_order sorts on and _write_message mints
        (daemon/handoff.py). Only the request itself — ``handoff-<ts>-…`` —
        is guarded. The successor seed (``handoff-seed-…``) is an ordinary
        turn: guarding it parked human messages behind long seed turns
        (handoff review 2026-10-02: up to 4.5 min late deliveries).
        """
        name = path.name
        return name.startswith("handoff-") and name[len("handoff-"):][:1].isdigit()

    def _claim(self, path: Path) -> Path | None:
        """Move a queued message into ``inbox/feeding/`` — the mark that it
        has entered a turn. Splitting in-turn messages from queued-unseen
        ones is what lets the daemon's reset retire consumed work instead of
        redelivering it to the fresh session (the 2026-08-15 race: reset
        right after "done" re-fed the whole brief). Returns the feeding-side
        path, or None if the file vanished first (handoff dedupe unlinks
        superseded requests — routine, not fatal)."""
        claimed = self.home / "inbox" / "feeding" / path.name
        try:
            claimed.parent.mkdir(parents=True, exist_ok=True)
            path.replace(claimed)
        except FileNotFoundError:
            return None
        return claimed

    def next_message(self) -> Path | None:
        while True:
            path = min((self.home / "inbox" / "new").glob("*.json"),
                       key=self._msg_order, default=None)
            if path is None:
                return None
            claimed = self._claim(path)
            if claimed is not None:
                return claimed

    def _user_event(self, msg: dict, msg_file: str) -> dict:
        # display_text/attachments (set by the daemon on uploads) keep the
        # chat honest: the bubble shows what the human typed + file chips,
        # while the vessel receives text with the attachment paths block.
        ev: dict = {"type": "user_message",
                    "text": msg.get("display_text", msg.get("text", "")),
                    "msg_file": msg_file}
        if msg.get("attachments"):
            ev["attachments"] = msg["attachments"]
        if msg.get("reply_to"):
            ev["reply_to"] = msg["reply_to"]
        # Origin keys (daemon/mailbox.py): copied as written, when present.
        for k in _ORIGIN_KEYS:
            if k in msg:
                ev[k] = msg[k]
        return ev

    async def _inject_loop(self, pending: set[Path]) -> None:
        """Steer the live turn: inject new inbox messages between tool calls.

        Injection order: stdin write FIRST, then the user_message event — an
        event without a delivered message would be a lie in the chat. If
        inject fails and the vessel can interrupt, abort the turn so the
        message is not stuck behind a long tool loop (Claude injects;
        pi/qwen must not sit on a handoff until they OOM). Otherwise the
        message is requeued for its own turn at the boundary.

        Steer-delivery nudge: an ACCEPTED steer is only queued inside the
        CLI — it reaches the model before the next LLM call, which a
        wedged tool call never lets happen (2026-09-18). If the
        turn goes silent for STEER_NUDGE_S after the first accepted steer,
        abort the running bash tool so the queue flushes; if that fails (or
        the wedge is not bash), abort the whole turn and requeue the
        injected messages so they get their own turn — silent message loss
        is the wrong failure mode.
        """
        fed = frozenset(pending)          # the turn's own message(s)
        steer_ts: float | None = None    # first accepted-but-undelivered steer
        nudged = False                   # abort_bash sent once
        interrupted = False              # turn abort attempted once
        # Per-pulse knobs from harness-overrides.json (read at turn start):
        # steer_nudge_s overrides the window, steer_prefix is prepended to
        # injected text only. Absent/wrong-typed = today's behaviour.
        ov = self._turn_overrides or {}
        # steer_nudge_s must be a finite number >= 0 — 0 is honoured as an
        # immediate nudge, same as the process-wide knob. An ABSENT key
        # selects the default SILENTLY (a warning on every ordinary turn is
        # noise); only a present-but-bad value warns. The type check comes
        # before float() so numeric strings like "0" cannot sneak through
        # (float("0") == 0.0 would nudge immediately); booleans are not
        # numbers; inf/NaN never elapse so a wedged turn would never get its
        # nudge; a huge int can overflow float() itself.
        raw_nudge = ov.get("steer_nudge_s")
        nudge_f: float | None = None
        if isinstance(raw_nudge, (int, float)) \
                and not isinstance(raw_nudge, bool):
            try:
                nudge_f = float(raw_nudge)
            except OverflowError:
                nudge_f = None
        if "steer_nudge_s" not in ov:
            nudge_s = float(STEER_NUDGE_S)  # absent key: today's behaviour
        elif nudge_f is None or not math.isfinite(nudge_f) or nudge_f < 0:
            log.warning("pulse=%s steer_nudge_s %r invalid (need a finite "
                        "number >= 0) — using default %ss",
                        self.pulse_id, raw_nudge, STEER_NUDGE_S)
            nudge_s = float(STEER_NUDGE_S)
        else:
            nudge_s = nudge_f
        raw_prefix = ov.get("steer_prefix")
        steer_prefix = raw_prefix if isinstance(raw_prefix, str) else ""
        while True:
            await asyncio.sleep(INBOX_POLL_S)
            for path in sorted((self.home / "inbox" / "new").glob("*.json"),
                               key=self._msg_order):
                claimed = self._claim(path)
                if claimed is None:
                    continue
                try:
                    msg = json.loads(claimed.read_text())
                    text = msg.get("text", "")
                    await self.vessel.inject(
                        steer_prefix + text if steer_prefix else text)
                except Exception:
                    log.exception("pulse=%s inject %s FAILED — message stays "
                                  "queued for its own turn", self.pulse_id,
                                  path.name)
                    # un-claim: back to new/ so it gets its own turn.
                    with contextlib.suppress(OSError):
                        requeue_message(claimed, path)
                    await self._interrupt_after_failed_inject(path.name)
                    return
                if steer_ts is None:
                    steer_ts = time.monotonic()
                ev = {**self._user_event(msg, path.name), "injected": True}
                if steer_prefix:
                    # The event keeps the original text; the prefix rides
                    # along as its own field.
                    ev["injected_prefix"] = steer_prefix
                self.emit(ev)
                pending.add(claimed)
                log.info("pulse=%s injected %s mid-turn (%d chars)",
                         self.pulse_id, path.name, len(msg.get("text", "")))
            if steer_ts is None or interrupted:
                continue
            # Silence since the steer was accepted: max of the steer clock
            # and the later of runner-side events / the vessel progress
            # clock (tool partials the event schema drops still move it).
            clocks = [self._turn_progress_ts]
            vessel_ts = self.vessel.last_progress_ts() \
                if hasattr(self.vessel, "last_progress_ts") else None
            if vessel_ts is not None:
                clocks.append(vessel_ts)
            if time.monotonic() - max(steer_ts, max(clocks)) < nudge_s:
                continue
            if not nudged and getattr(self.vessel, "can_abort_bash", False):
                nudged = True
                # restart the window: one more silence period before the
                # sledgehammer, in case abort_bash acks but the tool dies
                # slowly
                steer_ts = time.monotonic()
                log.warning("pulse=%s steer undelivered %.0fs and turn "
                            "silent — aborting wedged bash tool so the "
                            "steer gets through", self.pulse_id, nudge_s)
                try:
                    await asyncio.shield(self.vessel.abort_bash())
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("pulse=%s abort_bash FAILED — escalating "
                                  "to turn interrupt", self.pulse_id)
            # Last resort: end the turn. The injected steers were queued in
            # the CLI, not provably delivered — requeue them so they get
            # their own turn instead of a silent boundary ack.
            interrupted = True
            log.warning("pulse=%s steer still undelivered — interrupting "
                        "turn and requeuing %d injected message(s)",
                        self.pulse_id, len(pending - fed))
            for path in pending - fed:
                with contextlib.suppress(OSError):
                    requeue_message(path, self.home / "inbox" / "new" / path.name)
                # Back in new/ now: drop it from the turn's set, or a later
                # vessel.send failure requeues it AGAIN from feeding/, where
                # it no longer is ("requeue ... unreadable [Errno 2]", live
                # 2026-10-09, 4 messages).
                pending.discard(path)
            if not getattr(self.vessel, "can_interrupt", False):
                log.error("pulse=%s vessel cannot interrupt — steer waits "
                          "for the turn boundary", self.pulse_id)
                return
            try:
                self._turn_interrupted = True
                await asyncio.shield(self.vessel.interrupt())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pulse=%s steer-nudge interrupt FAILED — "
                              "message waits for the turn boundary",
                              self.pulse_id)
            return

    async def _interrupt_after_failed_inject(self, msg_name: str) -> None:
        """Abort the live turn when inject cannot land (Claude-like floor).

        No-op when the vessel has no turn interrupt. Shielded so feed_loop's
        finally-cancel cannot abort a half-done kill-and-resume.
        """
        if not getattr(self.vessel, "can_interrupt", False):
            return
        log.info("pulse=%s interrupting turn after inject %s failed",
                 self.pulse_id, msg_name)
        try:
            self._turn_interrupted = True
            await asyncio.shield(self.vessel.interrupt())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pulse=%s interrupt after failed inject also "
                          "FAILED — message waits for the turn boundary",
                          self.pulse_id)

    async def _interrupt_on_message_loop(self, pending: set[Path]) -> None:
        """Steer vessels with no native mid-turn injection (qwen/pi json): a
        message arriving while a turn runs kills-and-resumes the turn so the
        model sees it immediately instead of waiting for the turn boundary.

        One interrupt per turn: the aborted turn closes with
        ``done{subtype: interrupted}``, feed_loop reaches its boundary and
        dispatches the queued message (handoffs first via _msg_order) as its
        own turn against the resumed session. The interrupt is shielded so
        feed_loop's finally-cancel cannot abort a half-done kill-and-resume.

        A wrap-up that is ALREADY the in-flight turn is not aborted — that
        guard lives in feed_loop (``if not self._is_handoff``). A wrap-up
        sitting in new/ MUST interrupt the current work turn; skipping
        handoff-*.json here parks the request behind a tool loop until OOM
        (seen live 2026-09-04 at 246k/175k).
        """
        while True:
            await asyncio.sleep(INBOX_POLL_S)
            fresh = [p for p in (self.home / "inbox" / "new").glob("*.json")
                     if p not in pending]
            if not fresh:
                continue
            log.info("pulse=%s interrupting turn for %d queued message(s): %s",
                     self.pulse_id, len(fresh),
                     ", ".join(sorted(p.name for p in fresh)[:3]))
            try:
                self._turn_interrupted = True
                await asyncio.shield(self.vessel.interrupt())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pulse=%s steer interrupt FAILED — message "
                              "waits for the turn boundary", self.pulse_id)
            return

    def _handle_event(self, ev: dict) -> None:
        """Route one vessel event: deltas feed the live panels, state events
        update the runner, everything else is emitted. Used by the send()
        drain and by _pump_stray_events so both capture the same state."""
        if ev.get("type") == "assistant_delta":
            self._on_delta(ev.get("text") or "")
            return
        if ev.get("type") == "reasoning_delta":
            self._on_reasoning_delta(ev.get("text") or "")
            return
        if ev.get("type") == "hidden_delta":
            self._on_hidden_delta(ev.get("text") or "")
            return
        if ev.get("type") == "code_delta":
            self._on_code_delta(ev)
            return
        if ev.get("type") in ("assistant_text", "done"):
            # The finished block (or turn) supersedes the partial;
            # clear BEFORE emitting so a poller never sees both.
            self._clear_partial()
        if ev.get("type") in ("tool_call", "done", "runner_error") and (
                self._code_text or self._code_id):
            # Same for the live code card: the tool_call card replaces it.
            self._clear_code()
        # Loop-trigger counters (v25) — standalone so the chain below is intact.
        if ev.get("type") == "tool_call":
            self.tool_calls_total += 1
        elif ev.get("type") == "done":
            self.replies_total += 1
        if ev.get("type") == "session":
            self.cli_session_id = ev.get("session_id") or self.cli_session_id
        elif ev.get("type") == "usage" and ev.get("ctx") is not None:
            self.last_ctx_tokens = ev.get("ctx")
        elif (ev.get("type") == "runner_error"
              and ev.get("where") == "provider_error"):
            self.turn_error = ev.get("reason")
            self._turn_provider_error = True
            self.heartbeat()
        elif (ev.get("type") == "done" and ev.get("reason") == "error"
              and not self._turn_interrupted
              and "interrupted" not in str(ev.get("subtype") or "")
              and ev.get("subtype") not in LOCAL_ERROR_SUBTYPES):
            # A CLI-reported error result (claude is_error, codex
            # turn.failed / error, grok RPC failure) — the provider failed
            # the turn. An interrupt Pulsar sent, or a record our own
            # parser could not read, is not a provider error.
            self._turn_provider_error = True
        self._emit_vessel(ev)

    def _pump_stray_events(self) -> None:
        """Emit events that arrive outside a send() drain.

        A message injected in the closing instants of a turn can miss it and
        start a NEW turn whose events stream while the runner sits idle; this
        pump surfaces them instead of letting them rot in the bridge queue.
        """
        poll = getattr(self.vessel, "poll_event", None)
        if poll is None:
            return
        while True:
            ev = poll()
            if ev is None:
                return
            self._handle_event(ev)

    async def feed_loop(self) -> None:
        """Feed inbox messages to the vessel; stream events out. While a turn
        runs, new messages are injected into it (steering) when the vessel
        supports that; otherwise they wait their turn."""
        while not self._stopping.is_set():
            msg_path = self.next_message()
            if msg_path is None:
                self._pump_stray_events()
                self.set_state(STATE_IDLE)
                try:
                    await asyncio.wait_for(self._stopping.wait(), INBOX_POLL_S)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                msg = json.loads(msg_path.read_text())
            except FileNotFoundError:
                # The claim in next_message() absorbs the handoff-dedupe race
                # (2026-08-14: a vanished file crashed the feed task and
                # killed the pulse) — this tolerance stays as the backstop.
                log.info("pulse=%s message %s vanished before read "
                         "(superseded) — rescanning inbox", self.pulse_id,
                         msg_path.name)
                continue
            self.turn_error = None
            self._turn_provider_error = False
            self._turn_interrupted = False
            self._turn_overrides = read_overrides(self.home)
            self.set_state(STATE_BUSY)
            # New turn: drop the previous turn's reasoning trace so the panel
            # never mixes two turns. (It lingers after a turn so "last turn"
            # stays readable.)
            self._clear_reasoning()
            self._clear_code()
            started = time.monotonic()
            log.info("pulse=%s feeding message %s (%d chars)",
                     self.pulse_id, msg_path.name, len(msg.get("text", "")))
            self.emit(self._user_event(msg, msg_path.name))
            # Everything delivered this turn (fed + injected). Acked together
            # at the turn boundary; on error NOTHING acks, so a restored pulse
            # gets every message back.
            pending: set[Path] = {msg_path}
            injector: asyncio.Task | None = None
            # A handoff wrap-up is the one turn nothing may steer. The daemon
            # is blocked in _await_reply on THIS turn's final text; an
            # interrupt closes it with done{interrupted} and no assistant_text,
            # so extract_reply returns None forever and the handoff stalls the
            # full REPLY_TIMEOUT_S before failing — leaving the pulse running
            # on the very session that was already too full (seen live
            # 2026-08-23: a pulse message landed 43s into the
            # wrap-up). Injection is milder but still wrong: the model answers
            # the interloper instead of writing the handoff. Either way the
            # queued message loses nothing — it stays in new/ and the
            # SUCCESSOR picks it up after the respawn.
            if not self._is_handoff(msg_path):
                if getattr(self.vessel, "can_inject", False):
                    injector = asyncio.create_task(self._inject_loop(pending))
                elif getattr(self.vessel, "can_interrupt", False):
                    injector = asyncio.create_task(
                        self._interrupt_on_message_loop(pending))
            try:
                self._turn_progress_ts = time.monotonic()
                held = self._meter.start()
                if held is not None:
                    self.emit(held)
                async for ev in self.vessel.send(msg.get("text", "")):
                    # Every streamed event is turn progress — the steer nudge
                    # measures silence against this clock.
                    self._turn_progress_ts = time.monotonic()
                    self._handle_event(ev)
            except Exception as e:
                if self._stopping.is_set():
                    # A stop (■ / pause / SIGTERM) killed the CLI under this
                    # turn: pulse_stopped already says so, and a runner_error
                    # row would make a normal stop look like a crash.
                    log.info("pulse=%s vessel.send ended by stop: %s",
                             self.pulse_id, e)
                else:
                    log.exception("pulse=%s vessel.send failed after %.1fs",
                                  self.pulse_id, time.monotonic() - started)
                    # Record WHY in the final heartbeat — the daemon's
                    # dead-runner sweep copies it onto the tile as stop_reason.
                    self.stop_reason = f"vessel error: {e}"[:200]
                    self.emit({"type": "runner_error", "where": "vessel.send",
                               "msg_file": msg_path.name})
                # do NOT ack — un-claim back to new/ so the restored pulse
                # gets every message back (a failed turn is not consumed).
                # Except when a requested stop shut the CLI under the turn:
                # then the turn stays in feeding/, where the daemon's handoff
                # and reset decide what the next session still needs, and the
                # next boot requeues the rest exactly like this would.
                for path in pending if not self._stopping.is_set() else ():
                    with contextlib.suppress(OSError):
                        requeue_message(
                            path, self.home / "inbox" / "new" / path.name)
                self._stopping.set()
                break
            finally:
                if injector is not None:
                    injector.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await injector
            for path in pending:
                try:
                    path.replace(self.home / "inbox" / "acked" / path.name)
                except FileNotFoundError:
                    log.info("pulse=%s ack %s: already gone (superseded)",
                             self.pulse_id, path.name)
            log.info("pulse=%s acked %s (+%d injected) in %.1fs",
                     self.pulse_id, msg_path.name, len(pending) - 1,
                     time.monotonic() - started)
            if self._turn_provider_error:
                self.provider_error_turns += 1
                self.provider_error_msg = msg_path.name
                log.warning("pulse=%s turn ended on a provider error (%d in "
                            "a row)", self.pulse_id, self.provider_error_turns)
            else:
                self.provider_error_turns = 0
                self.provider_error_msg = None
            self.heartbeat()

    # ---------- lifecycle ----------

    async def heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            self.heartbeat()
            try:
                await asyncio.wait_for(self._stopping.wait(), HEARTBEAT_EVERY_S)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> int:
        for sub in ("inbox/new", "inbox/feeding", "inbox/acked", "cli"):
            (self.home / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        # At-least-once on crash/restore: a predecessor that died mid-turn
        # left its claimed messages in feeding/ — requeue them. A deliberate
        # reset instead retires feeding/ to acked/ in the daemon BEFORE this
        # respawn, so a finished brief is not redone.
        for stale in (self.home / "inbox" / "feeding").glob("*.json"):
            try:
                requeue_message(stale,
                                self.home / "inbox" / "new" / stale.name)
                log.info("pulse=%s requeued unacked %s from feeding/",
                         self.pulse_id, stale.name)
            except OSError as e:
                log.warning("pulse=%s could not requeue %s from feeding/: %s",
                            self.pulse_id, stale.name, e)
        _touch_private(self.home / "events.jsonl")
        self._events_fh = (self.home / "events.jsonl").open("a")
        # A crashed prior runner can leave a stale partial behind; the UI must
        # never show last week's half-sentence as live streaming. Same for a
        # stale reasoning trace.
        self._clear_partial()
        self._clear_reasoning()
        self._clear_code()
        self.heartbeat()

        # Diet (clean-by-default) is decided by the daemon and read
        # here from disk — the runner passes it through, dumb as ever.
        diet = None
        diet_path = self.home / "diet.json"
        if diet_path.exists():
            try:
                diet = json.loads(diet_path.read_text())
                log.info("pulse=%s diet loaded: %s", self.pulse_id, diet)
            except (OSError, json.JSONDecodeError) as e:
                log.error("pulse=%s diet.json UNREADABLE (%s) — spawning "
                          "full-fat", self.pulse_id, e)

        from pulsar.runner.vessel_bridge import make_vessel
        self.vessel = make_vessel(self.vessel_kind, model=self.model,
                                  workdir=self.home / "cli",
                                  resume=self.resume_session, diet=diet,
                                  effort=self.effort, fast=self.fast)
        try:
            await self.vessel.start()
        except Exception as e:
            log.exception("pulse=%s vessel failed to start", self.pulse_id)
            self.stop_reason = f"vessel failed to start: {e}"[:200]
            self.emit({"type": "runner_error", "where": "vessel.start",
                       "reason": self.stop_reason})
            self.set_state(STATE_DEAD_CLI)
            return 1
        log.info("pulse=%s vessel up cli_pid=%s resume=%s",
                 self.pulse_id, getattr(self.vessel, "pid", None),
                 self.resume_session or "-")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)

        sock_path = self.home / "runner.sock"
        if sock_path.exists():
            log.warning("pulse=%s stale runner.sock removed", self.pulse_id)
            sock_path.unlink()
        server = await asyncio.start_unix_server(self.handle_conn, path=str(sock_path))

        hb = asyncio.create_task(self.heartbeat_loop())
        feed = asyncio.create_task(self.feed_loop())
        cli_watch = asyncio.create_task(self.vessel.wait())
        stop_wait = asyncio.create_task(self._stopping.wait())

        done, _ = await asyncio.wait(
            {hb, feed, cli_watch, stop_wait},
            return_when=asyncio.FIRST_COMPLETED)

        # A supervisor task completing unexpectedly is a runner failure, not a
        # clean stop. In particular, feed_loop can die before it gets to its
        # vessel try-block (bad inbox JSON or an ack filesystem error), and a
        # heartbeat write failure must not leave a live CLI looking healthy.
        task_failure = None
        for task, name in ((feed, "feed"), (hb, "heartbeat")):
            if task not in done or task.cancelled():
                continue
            exc = task.exception()
            if exc is None and self._stopping.is_set():
                continue
            if exc is None:
                exc = RuntimeError(f"{name} task exited unexpectedly")
            task_failure = (name, exc)
            break

        if task_failure is not None:
            name, exc = task_failure
            self.stop_reason = f"{name} task failed: {exc}"[:200]
            log.error("pulse=%s %s", self.pulse_id, self.stop_reason,
                      exc_info=(type(exc), exc, exc.__traceback__))
            self.emit({"type": "runner_error", "where": f"{name}_loop",
                       "reason": self.stop_reason})
            try:
                await self.vessel.stop()
            finally:
                self.state = STATE_DEAD_CLI
        elif cli_watch in done and not self._stopping.is_set():
            rc = cli_watch.result()
            # Compare against the boot-time baseline: with OOMPolicy=continue
            # the pulse survives child OOM kills (runaway ugrep), so a stale
            # nonzero counter must not relabel a later unrelated CLI death.
            oom_kills = cgroup_oom_kills() - self._oom_baseline
            if oom_kills:
                # The runner lives inside the fence cgroup, so the kernel's own
                # counter is the authoritative OOM verdict — no journal parsing.
                self.stop_reason = "hit memory cap"
                log.error("pulse=%s CLI killed by the MEMORY FENCE "
                          "(cgroup oom_kill=%d) rc=%s", self.pulse_id,
                          oom_kills, rc)
                self.emit({"type": "fence_breach", "oom_kills": oom_kills,
                           "rc": rc})
            else:
                self.stop_reason = f"cli died (rc={rc})"
                log.error("pulse=%s CLI exited on its own rc=%s — reporting "
                          "and exiting", self.pulse_id, rc)
            self.emit({"type": "runner_error", "where": "cli_exit", "rc": rc,
                       "reason": self.stop_reason})
            self.set_state(STATE_DEAD_CLI)
        else:
            self.set_state(STATE_STOPPING)
            log.info("pulse=%s stopping: shutting CLI down", self.pulse_id)
            await self.vessel.stop()
            self.set_state(STATE_STOPPED)

        self._stopping.set()
        for t in (hb, feed, cli_watch, stop_wait):
            t.cancel()
        for t in (hb, feed, cli_watch, stop_wait):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        server.close()
        await server.wait_closed()
        # The socket path must not outlive the listener: a respawn would log
        # "stale runner.sock removed" and a client could race the unlink.
        with contextlib.suppress(OSError):
            sock_path.unlink()
        self._clear_partial()
        self._clear_reasoning()
        self._clear_code()
        self.heartbeat()
        log.info("pulse=%s runner exit state=%s", self.pulse_id, self.state)
        return 0 if self.state == STATE_STOPPED else 1


# User-level tool dirs the systemd unit's default PATH lacks. Appended so
# system binaries and the guard shims keep precedence; only dirs that exist
# are added.
USER_TOOL_DIRS = (".local/bin", ".cargo/bin")


def tool_path(path: str, home: Path, daemon_path: str = "") -> str:
    """PATH for every pulse shell: guard shims first, then the daemon's PATH
    (install.sh puts each detected agent tool directory there, and the daemon
    looks for the tools there), the inherited PATH, then the user tool dirs
    that exist and are not already present."""
    first: list[str] = []
    for p in daemon_path.split(os.pathsep):
        if p and p not in first:
            first.append(p)
    parts = [str(GUARD_BIN_DIR)] + first + [
        p for p in path.split(os.pathsep) if p and p not in first]
    for rel in USER_TOOL_DIRS:
        d = str(home / rel)
        if d not in parts and (home / rel).is_dir():
            parts.append(d)
    return os.pathsep.join(parts)


def _touch_private(path: Path) -> None:
    """Create ``path`` with mode 0600 if it does not exist.

    The runner does not change the process umask: the agent tool is a child
    of the runner, and the files it writes in the user's projects must keep
    the user's normal modes. The runner's own files are made private here.
    """
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulse-id", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--vessel", default="claude-code")
    ap.add_argument("--resume", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()

    home = Path(args.home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _touch_private(home / "runner.log")
    # Delete guard for every vessel: ops/guard-bin holds rm/rmdir/find shims
    # that ask pulsar-guard before exec'ing the real binary. Every vessel
    # builds its CLI env from os.environ, so first-on-PATH here reaches each
    # pulse's shell. See pulsar/cli/guard.py.
    # PULSAR_DAEMON_PATH comes from spawn_runner; it is removed here so the
    # agent tool does not get it.
    os.environ["PATH"] = tool_path(os.environ.get("PATH", ""), Path.home(),
                                   os.environ.pop("PULSAR_DAEMON_PATH", ""))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(home / "runner.log"),
                  logging.StreamHandler(sys.stderr)])

    runner = Runner(args.pulse_id, home, args.model, args.vessel, args.resume,
                    effort=args.effort, fast=args.fast)
    rc = asyncio.run(runner.run())
    sys.exit(rc)


if __name__ == "__main__":
    main()
