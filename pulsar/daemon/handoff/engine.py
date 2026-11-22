"""Handoff engine: ask the pulse for its note, capture
the reply from events.jsonl, stop the old runner, seed and start the successor.
The flow is documented on the package (``pulsar.daemon.handoff``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from pulsar.daemon import events_io, lifecycle, loop_triggers, mailbox, paths
from pulsar.daemon.handoff import state
from pulsar.daemon.handoff.prompts import (
    handoff_prompt_for,
    has_sections,
    notes_block,
    reask_prompt_for,
    successor_prefix_for,
)
from pulsar.daemon.handoff.seed import (
    carry_over_block,
    forced_seed,
)
from pulsar.daemon.handoff.state import (
    append_ledger,
    in_flight,
    read_state,
    threshold_for,
    write_state,
)
from pulsar.daemon.timeutil import now_iso as _now_iso
from pulsar.interface_profile import is_interface_model
from pulsar.store import Pulse, Store

logger = logging.getLogger("pulsar.daemon.handoff")


REPLY_POLL_S = 2.0
TAIL_BYTES = 64 * 1024  # bounded events.jsonl window — never whole-file
# Reading forward from the request offset: cap one poll's read so a runaway
# events.jsonl can never balloon the daemon.
READ_CHUNK_BYTES = 4 * 1024 * 1024


def _next_handoff_path(pulse_id: str) -> Path:
    d = paths.pulse_home(pulse_id) / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    ns = [int(f.stem) for f in d.glob("*.md") if f.stem.isdigit()]
    return d / f"{max(ns, default=0) + 1}.md"


def write_message(pulse_id: str, text: str, prefix: str) -> str:
    """Atomic tmp+rename into inbox/new (same contract as app/scheduler)."""
    return mailbox.write(
        pulse_id, text,
        prefix=prefix,
        supersede=prefix.startswith(("handoff", "loopguard")),
        source="handoff",
        extra=mailbox.origin_keys("daemon", "handoff"),
    )


# --------------------------------------------------------------------------- #
# Reply capture (bounded events.jsonl tail)
# --------------------------------------------------------------------------- #
def _retire_received(pulse_id: str, acked: Path, cutoff: int) -> None:
    """Move inbox/feeding/ messages whose user_message (fed or injected) is
    in events.jsonl before byte ``cutoff`` to ``acked``."""
    feeding = paths.pulse_home(pulse_id) / "inbox" / "feeding"
    names = {p.name for p in feeding.glob("*.json")}
    if not names:
        return
    seen: set[str] = set()
    try:
        with paths.events_path(pulse_id).open("rb") as fh:
            end = 0
            for line in fh:
                end += len(line)
                if end > cutoff:
                    break
                if b'"user_message"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if (ev.get("type") == "user_message"
                        and ev.get("msg_file") in names):
                    seen.add(ev["msg_file"])
    except OSError:
        return
    for name in seen:
        with contextlib.suppress(FileNotFoundError):
            (feeding / name).replace(acked / name)
            logger.info("handoff.retire pulse=%s %s already received before "
                        "the note — not redelivered", pulse_id, name)


def _delivered(events: list[dict], msg_file: str) -> bool:
    return any(ev.get("type") == "user_message"
               and ev.get("msg_file") == msg_file for ev in events)


def _pick_note(parts: list[str]) -> str:
    """The note from one turn's text blocks. When the model narrated before
    writing the note, the final block IS the note — take it alone so wrap-up
    chatter stays out of the seed (272 of 1,009 notes were stitched). If the
    sections only exist across blocks, keep them all."""
    blocks = [p for p in parts if p and p.strip()]
    if not blocks:
        return ""
    last = blocks[-1].strip()
    if len(blocks) > 1 and (has_sections(last) or has_sections(last, team=True)):
        return last
    return "\n".join(blocks).strip()


def extract_reply(events: list[dict], msg_file: str) -> Optional[str]:
    return _find_reply(events, msg_file)[0]


def _find_reply(events: list[dict], msg_file: str
                ) -> tuple[Optional[str], Optional[int]]:
    """extract_reply plus the index of the ``done`` that closed the reply.

    The assistant text between our user_message (by msg_file) and its done.

    Returns None while the turn is still incomplete (no done yet). Several
    text blocks: see _pick_note.

    An EMPTY interrupted done does not end the scan. The runner no longer
    steers a handoff turn (runner._is_handoff), but a stop or a crash can
    still close the wrap-up with ``done{interrupted}`` and no text — and the
    model typically writes the handoff as the final text of the very next
    turn. Stopping at that first done threw the note away and left the daemon
    polling a verdict it had already decided, for the whole REPLY_TIMEOUT_S
    (seen live 2026-08-23: the wrap-up was killed at 15:12:47,
    the model produced a complete handoff at 15:14:30, and it was ignored).
    An interrupted done that DID carry text still ends the scan — partial
    text the model wrote for us beats nothing.
    """
    started = False
    parts: list[str] = []
    for i, ev in enumerate(events):
        if ev.get("type") == "user_message" and ev.get("msg_file") == msg_file:
            started = True
            parts = []
            continue
        if not started:
            continue
        if ev.get("type") == "assistant_text":
            parts.append(ev.get("text", ""))
        elif ev.get("type") == "done":
            text = _pick_note(parts)
            if text:
                return text, i
            if _interrupted(ev):
                continue        # keep scanning: the note lands in a later turn
            return None, None
    return None, None


def _interrupted(done_ev: dict) -> bool:
    """Did this done close a turn that was cut short rather than finished?

    Vessels disagree on where they say so — the pi/claude bridges set
    ``reason``, the qwen kill-and-resume path sets ``subtype`` — so check
    both rather than trusting one.
    """
    return "interrupted" in (str(done_ev.get("reason") or "")
                             + str(done_ev.get("subtype") or ""))


def _superseded(pulse_id: str, attempt: str) -> bool:
    """True when another handoff attempt has rewritten the state marker."""
    return (read_state(pulse_id) or {}).get("attempt") != attempt


async def _await_reply(pulse_id: str, msg_file: str,
                       timeout_s: Optional[float] = None,
                       offset: int = 0,
                       timing: Optional[dict] = None,
                       alive: Optional[Callable[[], bool]] = None,
                       attempt: Optional[str] = None,
                       total_timeout_s: Optional[float] = None
                       ) -> Optional[str]:
    """Wait for the reply to ``msg_file``, reading events.jsonl forward from
    ``offset`` (its size when the request was written).

    Two clocks: up to DELIVERY_TIMEOUT_S for the request's user_message to
    appear, then ``timeout_s`` from THAT moment for the reply. One last
    rescan before giving up — the reply can land between the final poll and
    the deadline (scan-after-deadline, see the loop). ``timing`` (optional) receives delivered_at/replied_at
    wall-clock stamps for the ledger. ``alive`` (optional) is checked every
    poll; once it says False the wait ends at once — an archived or deleted
    pulse will never answer, and its wait must not run the full timeout.
    ``total_timeout_s`` caps the WHOLE wait (delivery + reply) measured from
    the start of this call, so a caller with an upstream deadline (model
    switch) never waits longer than it. ``attempt``
    (optional) ends the wait when a newer handoff attempt has rewritten the
    state marker (a forced handoff overtaking this one)."""
    # Read the module constants at CALL time (not def time) so tests can
    # monkeypatch them — a default arg binds too early.
    timeout_s = state.REPLY_TIMEOUT_S if timeout_s is None else timeout_s
    delivery_limit = state.DELIVERY_TIMEOUT_S
    cursor = events_io.EventCursor(pulse_id, offset)
    started = time.monotonic()
    delivered_at: Optional[float] = None
    timing = timing if timing is not None else {}

    def check() -> Optional[str]:
        nonlocal delivered_at
        events = cursor.poll()
        if delivered_at is None and _delivered(events, msg_file):
            delivered_at = time.monotonic()
            timing["delivered_at"] = _now_iso()
            logger.info("handoff.reply pulse=%s request delivered after %.1fs",
                        pulse_id, delivered_at - started)
        reply, done_at = _find_reply(events, msg_file)
        if reply:
            # Where the note ends in events.jsonl: the handoff's cutoff for
            # "the old session already received this" (_retire_received).
            timing["reply_end_offset"] = cursor.ends[done_at]
        return reply

    # Check BEFORE testing the deadline, every pass: the last scan always
    # happens after the deadline, so a reply that landed during the final
    # sleep is still caught (the old loop slept, saw the deadline, and quit
    # without looking).
    while True:
        reply = check()
        if reply:
            timing["replied_at"] = _now_iso()
            logger.info("handoff.reply pulse=%s %d chars in %.1fs",
                        pulse_id, len(reply), time.monotonic() - started)
            return reply
        now = time.monotonic()
        if total_timeout_s is not None and now - started >= total_timeout_s:
            break
        if delivered_at is None:
            if now - started >= delivery_limit:
                break
        elif now - delivered_at >= timeout_s:
            break
        if alive is not None and not alive():
            logger.error("handoff.reply pulse=%s no longer running after "
                         "%.0fs — wait abandoned (msg=%s)", pulse_id,
                         now - started, msg_file)
            return None
        if attempt is not None and _superseded(pulse_id, attempt):
            return None
        await asyncio.sleep(REPLY_POLL_S)
    logger.error("handoff.reply pulse=%s TIMEOUT (%s) after %.0fs (msg=%s)",
                 pulse_id, "reply" if delivered_at else "never delivered",
                 time.monotonic() - started, msg_file)
    return None


async def _ask_for_note(pulse: Pulse, timing: Optional[dict] = None,
                        alive: Optional[Callable[[], bool]] = None,
                        reason: str = "context_threshold",
                        reply_timeout_s: Optional[float] = None,
                        attempt: Optional[str] = None) -> Optional[str]:
    """Send the handoff request and return a usable note, re-asking ONCE
    when the reply lacks the requested sections. A re-ask that fails keeps
    the better of the two replies rather than aborting."""
    pid = pulse.id
    team = is_interface_model(pulse.model)
    timing = timing if timing is not None else {}
    offset = events_io.events_size(pid)
    timing["request_offset"] = offset
    msg_file = write_message(pid, handoff_prompt_for(pulse, reason), "handoff")
    timing["requested_at"] = _now_iso()
    # A caller-capped wait (model switch, under a reverse proxy's upstream
    # timeout) caps delivery + reply TOGETHER from this moment: a dead model
    # never takes delivery, and a slow one cannot stretch past the cap.
    reply = await _await_reply(pid, msg_file, reply_timeout_s, offset=offset,
                               timing=timing, alive=alive, attempt=attempt,
                               total_timeout_s=reply_timeout_s)
    if (reply is None or has_sections(reply, team)
            or reply_timeout_s is not None):  # capped wait: no re-ask
        timing["note_has_sections"] = (
            reply is not None and has_sections(reply, team))
        return reply
    logger.warning("handoff.reply pulse=%s note lacks sections (%d chars) — "
                   "re-asking once", pid, len(reply))
    timing["reasked"] = True
    offset = events_io.events_size(pid)
    msg_file = write_message(pid, reask_prompt_for(pulse), "handoff")
    second_timing: dict = {}
    second = await _await_reply(pid, msg_file, timeout_s=state.REASK_TIMEOUT_S,
                                offset=offset, timing=second_timing,
                                alive=alive, attempt=attempt)
    if second:
        timing["reply_end_offset"] = second_timing["reply_end_offset"]
    if second and has_sections(second, team):
        timing["note_has_sections"] = True
        return second
    timing["note_has_sections"] = False
    logger.error("handoff.reply pulse=%s re-ask gave no sectioned note — "
                 "using the longer reply", pid)
    return max([r for r in (reply, second) if r], key=len)


def _wait_stopped(pulse_id: str,
                  timeout_s: Optional[float] = None) -> None:
    """Wait for the runner's heartbeat to say stopped/dead (bounded, logged).

    Sync (``time.sleep``): it runs in the swap closure's worker thread."""
    timeout_s = state.STOP_SETTLE_S if timeout_s is None else timeout_s
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        beat = paths.read_heartbeat(pulse_id) or {}
        if beat.get("state") in ("stopped", "cli_dead", None):
            return
        time.sleep(0.5)
    logger.warning("handoff.stop pulse=%s heartbeat never settled in %.0fs — "
                   "proceeding (unit was stopped)", pulse_id, timeout_s)


# --------------------------------------------------------------------------- #
# The handoff itself
# --------------------------------------------------------------------------- #


async def run_handoff(store: Store, pulse: Pulse, *,
                      new_model: Optional[str] = None,
                      new_vessel: Optional[str] = None,
                      reason: str = "context_threshold",
                      fallback_reply: Optional[str] = None,
                      reserved: bool = False,
                      force: bool = False,
                      note_text: Optional[str] = None,
                      reply_timeout_s: Optional[float] = None,
                      fallback_to_transcript: bool = False) -> bool:
    """Hand a running pulse off to a fresh CLI session. Returns success.
    Every attempt, whatever its exit, appends one ledger line.

    ``note_text`` is a note the pulse already wrote (POST /handoff with
    note_path, after the early warning): no request, no wait — it seeds the
    successor directly."""
    beat = paths.read_heartbeat(pulse.id) or {}
    rec: dict[str, Any] = {
        "ts": _now_iso(), "pulse_id": pulse.id,
        "n": int((pulse.settings or {}).get("handoff_count") or 0) + 1,
        "reason": reason, "model": pulse.model, "vessel": pulse.vessel,
        "new_model": new_model, "new_vessel": new_vessel,
        "threshold": threshold_for(pulse),
        "ctx_at_request": beat.get("last_ctx_tokens"),
        "forced": force, "note_supplied": note_text is not None,
        "outcome": "raised",
    }
    started = time.monotonic()
    try:
        return await _run_handoff(store, pulse, rec, new_model=new_model,
                                  new_vessel=new_vessel, reason=reason,
                                  fallback_reply=fallback_reply,
                                  reserved=reserved, force=force,
                                  note_text=note_text,
                                  reply_timeout_s=reply_timeout_s,
                                  fallback_to_transcript=fallback_to_transcript)
    finally:
        rec["duration_s"] = round(time.monotonic() - started, 1)
        rec["ended_at"] = _now_iso()
        append_ledger(rec)


async def _run_handoff(store: Store, pulse: Pulse, rec: dict, *,
                       new_model: Optional[str] = None,
                       new_vessel: Optional[str] = None,
                       reason: str = "context_threshold",
                       fallback_reply: Optional[str] = None,
                       reserved: bool = False,
                       force: bool = False,
                       note_text: Optional[str] = None,
                       reply_timeout_s: Optional[float] = None,
                       fallback_to_transcript: bool = False) -> bool:
    """run_handoff's body; fills ``rec`` (the ledger line) as it goes.

    ``new_model``/``new_vessel`` switch the successor (cross-vessel model
    switch rides this same path). ``fallback_reply`` seeds the successor
    when the pulse cannot write its own handoff prose (loop-guard rescues:
    a model stuck in a degenerate loop may never answer the request) —
    without it a reply timeout aborts as before. ``reserved`` means "the
    caller has already reserved the single-flight marker; skip my own
    check." ``force`` skips asking the pulse at all (it is dead, or the
    caller wants a mechanical seed): the successor is seeded with
    ``forced_seed`` — the last ~40k tokens of transcript + the file path —
    and a stopped pulse is respawned without a stop step.
    ``reply_timeout_s`` caps the wait for the pulse's own handoff text and
    ``fallback_to_transcript`` seeds from the transcript (as ``force`` does)
    when that wait runs out — a model switch uses both so a pulse on a dead
    model still moves, and inside a reverse proxy's request timeout. On ANY failure the pulse is left in a sane state (running old
    session, or stopped with a logged reason) and the cooldown stamp
    prevents a tight retry loop.
    """
    pid = pulse.id
    started = time.monotonic()
    model = new_model or pulse.model
    vessel = new_vessel or pulse.vessel
    logger.info("handoff.start pulse=%s reason=%s %s/%s -> %s/%s",
                pid, reason, pulse.model, pulse.vessel, model, vessel)

    # Single-flight. The monitor and a manual cross-vessel switch both call
    # this, and the request wait yields for up to REPLY_TIMEOUT_S — two in
    # flight would double-stop the runner, race for the same handoffs/<n>.md
    # and spawn competing successors. A caller that reserved the marker
    # synchronously before scheduling (manual endpoint) skips this check:
    # it IS the reserving request.
    if not reserved:
        phase = in_flight(pid)
        if phase == "requested" and force:
            # A forced handoff never asks the pulse, so it may overtake one
            # still waiting on a dead model's reply (a switch off a
            # timed-out model otherwise sat "requested" for 10 minutes and
            # refused the forced retry). The waiter sees the new attempt id and
            # bows out.
            logger.warning("handoff.preempt pulse=%s forced %s overtakes an "
                           "unanswered request", pid, reason)
        elif phase is not None:
            logger.error("handoff.refused pulse=%s already in flight "
                         "(phase=%s) — reason=%s dropped", pid, phase, reason)
            rec["outcome"] = "refused"
            return False

    # Cooldown stamp FIRST (attempt-based, like scheduler.mark_fired) so a
    # crash mid-handoff cannot re-fire every tick.
    store.update_settings(pid, {"last_handoff_at": _now_iso()})
    attempt = uuid.uuid4().hex[:12]
    write_state(pid, "requested", reason=reason, attempt=attempt)
    # What the old session has received so far: everything in events.jsonl
    # up to here. A supplied note, a forced seed and a fallback seed are all
    # fixed now; a requested note moves the cutoff to the end of its reply.
    received_cutoff = events_io.events_size(pid)

    reply, was_idle = await _obtain_note(
        store, pulse, rec, reason=reason, fallback_reply=fallback_reply,
        force=force, note_text=note_text, started=started, attempt=attempt,
        reply_timeout_s=reply_timeout_s,
        fallback_to_transcript=fallback_to_transcript)
    if reply is None:
        return False

    # The wait above can take minutes. A pulse archived or deleted in the
    # meantime must not be stopped/respawned. (Stopped is still respawned:
    # the dead-runner sweep can mark it so mid-wait — see the set_state
    # below.)
    now_state = state.store_state(store, pid)
    if now_state is None or now_state == "archived":
        write_state(pid, "failed", reason=f"pulse_{now_state or 'deleted'}")
        logger.error("handoff.abort pulse=%s state is now %s — not "
                     "respawning", pid, now_state or "deleted")
        rec["outcome"] = "pulse_not_running"
        return False

    path = _next_handoff_path(pid)
    path.write_text(reply)
    rec["note_chars"] = len(reply)
    rec["handoff_file"] = path.name
    logger.info("handoff.saved pulse=%s %s (%d chars)", pid, path.name,
                len(reply))
    write_state(pid, "stopping", handoff_file=path.name)

    # The swap: stop the old runner, stage the seed, start the successor. It
    # runs as one sync closure in a worker thread under the pulse lock (never
    # held across the reply wait above): the socket stop, the systemd calls and
    # the settle poll must not block the event loop, and an archive either runs
    # before the closure (it sees "archived" and aborts) or after it (the
    # archive stops the successor normally).
    def _swap_tx() -> bool:
        nonlocal pulse
        with lifecycle.pulse_lock(pid):
            if not _stop_old(store, pulse, rec, force=force):
                return False
            pulse, effort, fast = _prepare_successor(
                store, pulse, rec, reply, model=model, vessel=vessel,
                new_model=new_model, new_vessel=new_vessel, force=force,
                received_cutoff=received_cutoff, was_idle=was_idle)
            return _spawn_successor(store, pulse, rec, path=path, model=model,
                                    vessel=vessel, effort=effort, fast=fast,
                                    reason=reason, started=started)

    return await asyncio.to_thread(_swap_tx)


async def _obtain_note(store: Store, pulse: Pulse, rec: dict, *, reason: str,
                       fallback_reply: Optional[str], force: bool,
                       note_text: Optional[str], started: float,
                       attempt: str, reply_timeout_s: Optional[float],
                       fallback_to_transcript: bool
                       ) -> tuple[Optional[str], bool]:
    """The text that seeds the successor: the supplied note, the forced seed,
    or the pulse's own reply (falling back to ``fallback_reply``). Returns
    ``(text, was_idle)``; the text is None after marking the attempt failed
    when there is none, or when a forced handoff superseded this attempt."""
    pid = pulse.id
    was_idle = False
    if note_text is not None:
        reply = note_text
        rec["note_has_sections"] = has_sections(
            reply, is_interface_model(pulse.model))
        logger.info("handoff.note pulse=%s seeding from supplied note "
                    "(%d chars, no request sent)", pid, len(reply))
    elif force:
        reply = forced_seed(pid, (pulse.settings or {}).get("stop_reason"))
        logger.info("handoff.forced pulse=%s seed=%d chars (no request sent)",
                    pid, len(reply))
    else:
        # Read before the request: answering it makes the pulse busy. Manual
        # only — a threshold handoff between turns may still be mid-project.
        was_idle = reason == "manual" and (
            paths.read_heartbeat(pid) or {}).get("state") == "idle"
        reply = await _ask_for_note(
            pulse, timing=rec, reason=reason, reply_timeout_s=reply_timeout_s,
            attempt=attempt,
            alive=lambda: state.store_state(store, pid) not in (None, "archived"))
        if _superseded(pid, attempt):
            logger.warning("handoff.superseded pulse=%s reason=%s — a forced "
                           "handoff took over; this attempt stops here",
                           pid, reason)
            rec["outcome"] = "superseded"
            return None, was_idle
    if reply is None and fallback_to_transcript:
        reply = forced_seed(pid, (pulse.settings or {}).get("stop_reason"))
        rec["fallback_used"] = True
        # The transcript seed covers everything the old session received
        # during the wait, so the cutoff moves up to now: a message injected
        # while waiting is in the seed and must be retired, not re-fed.
        rec["reply_end_offset"] = events_io.events_size(pid)
        logger.error("handoff.reply pulse=%s no answer — seeding from the "
                     "transcript instead (%d chars, reason=%s)", pid,
                     len(reply), reason)
    if reply is None:
        if fallback_reply:
            logger.error("handoff.reply pulse=%s TIMEOUT — using caller's "
                         "fallback seed (%d chars, reason=%s)", pid,
                         len(fallback_reply), reason)
            reply = fallback_reply
            rec["fallback_used"] = True
        else:
            # Re-stamp the cooldown at the END of the attempt: stamped only at
            # the start, a 600s cooldown expired with the 600s reply wait and
            # the next 30s tick fired again (handoff review 2026-10-02).
            store.update_settings(pid, {"last_handoff_at": _now_iso()})
            write_state(pid, "failed", reason="reply_timeout")
            logger.error("handoff.abort pulse=%s no handoff text (pulse still "
                         "running old session) in %.1fs", pid,
                         time.monotonic() - started)
            rec["outcome"] = "reply_timeout"
            return None, was_idle
    return reply, was_idle


def _stop_old(store: Store, pulse: Pulse, rec: dict, *, force: bool) -> bool:
    """Stop the old runner (under the pulse lock). False when the attempt was
    aborted: the pulse was archived/deleted, or the stop failed."""
    pid = pulse.id
    now_state = state.store_state(store, pid)
    if now_state is None or now_state == "archived":
        write_state(pid, "failed",
                    reason=f"pulse_{now_state or 'deleted'}")
        logger.error("handoff.abort pulse=%s %s before the swap — not "
                     "respawning", pid, now_state or "deleted")
        rec["outcome"] = "pulse_not_running"
        return False
    if pulse.state == "running" or not force:
        try:
            lifecycle.stop_locked(pid)
        except lifecycle.StopFailed:
            store.update_settings(pid, {"last_handoff_at": _now_iso()})
            write_state(pid, "failed", reason="stop_failed")
            logger.error("handoff.abort pulse=%s could not stop old runner", pid)
            rec["outcome"] = "stop_failed"
            return False
        _wait_stopped(pid)
    else:
        logger.info("handoff.forced pulse=%s already stopped — no stop step", pid)

    # Archive can land while the old runner stops, too.
    now_state = state.store_state(store, pid)
    if now_state is None or now_state == "archived":
        write_state(pid, "failed", reason=f"pulse_{now_state or 'deleted'}")
        logger.error("handoff.abort pulse=%s %s during stop — not "
                     "respawning", pid, now_state or "deleted")
        rec["outcome"] = "pulse_not_running"
        return False
    return True


def _prepare_successor(store: Store, pulse: Pulse, rec: dict, reply: str, *,
                       model: str, vessel: str, new_model: Optional[str],
                       new_vessel: Optional[str], force: bool,
                       received_cutoff: int, was_idle: bool = False
                       ) -> tuple[Pulse, Optional[str], bool]:
    """Switch the model if asked, retire stale inbox files, stage the seed.
    Returns the (possibly updated) pulse and the effort/fast for the start."""
    pid = pulse.id
    if new_model or new_vessel:
        pulse = store.set_model(pid, model, vessel)

    home = paths.pulse_home(pid)
    # Reasoning effort and fast mode follow the pulse across handoffs; the
    # ones the target vessel cannot speak are dropped loudly.
    effort, fast = lifecycle.settings_for_target(store, pulse, model, vessel)
    # Retire stale handoff requests/seeds after the old runner has stopped.
    # Otherwise a forced rescue can immediately ask its successor to hand off
    # again. Preserve ordinary queued/claimed work for the runner to recover.
    acked = home / "inbox" / "acked"
    acked.mkdir(parents=True, exist_ok=True)
    for folder in ("new", "feeding"):
        for stale in (home / "inbox" / folder).glob("handoff-*.json"):
            stale.replace(acked / stale.name)
    # A mid-turn handoff leaves the turn's messages in feeding/, and the
    # successor's boot requeue would deliver them AGAIN (live 2026-10-11:
    # 12 briefs re-fed ~1s after the seed). Retire the ones the old session
    # was already fed before its note; the rest (claimed but never fed, or
    # fed after the note) stay for the requeue.
    _retire_received(pid, acked, rec.get("reply_end_offset", received_cutoff))
    # Stage the seed BEFORE launching: the runner can accept queued user work
    # immediately, and that work must never run without the saved context.
    # Seed style: one routine style for every pulse (the note only). Forced and
    # fallback seeds keep their own mechanical paths and are labeled for the ledger.
    if force:
        variant = "forced"
    elif rec.get("fallback_used"):
        variant = "fallback"
    else:
        variant = "note"
    seed = (successor_prefix_for(pulse, was_idle) + reply + notes_block(pid)
            + loop_triggers.handoff_block(store, pid) + carry_over_block(pid))
    rec["seed_variant"] = variant
    rec["seed_chars"] = len(seed)
    write_message(pid, seed, "handoff-seed")
    # The old resume pointer is invalid for this fresh session even if the
    # process fails before it can write its own heartbeat.
    paths.heartbeat_path(pid).unlink(missing_ok=True)
    return pulse, effort, fast


def _spawn_successor(store: Store, pulse: Pulse, rec: dict, *, path: Path,
                     model: str, vessel: str, effort: Optional[str], fast: bool,
                     reason: str, started: float) -> bool:
    """Start the fresh session and record the outcome. False when the respawn
    failed or the pulse was archived during it."""
    pid = pulse.id
    try:
        # resume=None IS the handoff: a fresh CLI session, seeded above.
        lifecycle.start_locked(store, pulse, resume=None, model=model,
                               vessel=vessel, effort=effort, fast=fast)
    except Exception as exc:
        write_state(pid, "failed", reason=f"respawn: {exc}")
        try:
            store.set_state(pid, "stopped")
            store.update_settings(pid, {"stop_reason": "handoff respawn failed"})
        except Exception:
            logger.exception("handoff.abort pulse=%s state cleanup failed", pid)
        logger.error("handoff.abort pulse=%s RESPAWN FAILED err=%s in %.1fs",
                     pid, exc, time.monotonic() - started)
        rec["outcome"] = "respawn_failed"
        return False

    # The successor is live, so say so. Handoff can wait minutes for the reply
    # and then 15s for the old heartbeat to settle, and the dead-runner sweep
    # runs every 10s against exactly that window — without this the store can
    # be left saying stopped while a fresh runner is up, which hides the pulse
    # from schedules, auto-handoff AND the sweep that would have fixed it.
    if state.store_state(store, pid) in (None, "archived"):
        # Archived during the spawn itself: never leave a runner behind it.
        logger.error("handoff.abort pulse=%s archived during respawn — "
                     "stopping the successor", pid)
        try:
            lifecycle.stop_locked(pid)
        except lifecycle.StopFailed:
            logger.error("handoff.abort pulse=%s could not stop the successor",
                         pid)
        write_state(pid, "failed", reason="pulse_archived")
        rec["outcome"] = "pulse_not_running"
        return False
    try:
        store.set_state(pid, "running")
    except Exception:
        logger.exception("handoff pulse=%s could not mark running after "
                         "successor spawn", pid)

    n = int(pulse.settings.get("handoff_count") or 0) + 1
    store.update_settings(pid, {"handoff_count": n, "stop_reason": None})
    write_state(pid, "done", handoff_file=path.name)
    logger.info("handoff.done pulse=%s #%d file=%s reason=%s in %.1fs",
                pid, n, path.name, reason, time.monotonic() - started)
    rec["outcome"] = "done"
    rec["n"] = n
    return True
