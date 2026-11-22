"""Handoff prompt texts: the request sent to a pulse,
the successor's seed prefix, the re-ask, the early warning, and the notes-file
helpers the texts embed. Constants and pure functions only.

Per-pulse template overrides (harness-overrides.json — see
pulsar/harness_overrides.py for the placeholder lists) replace the base
handoff request, the successor prefix, and the warning text. An explicit
template wins for EVERY model, interface models included; the interface
TEAM-PACKET defaults apply only when the key is absent. An explicitly
supplied empty string is honoured as empty. Every other behaviour
(addenda, notes helpers) is unchanged.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pulsar.daemon import paths
from pulsar.harness_overrides import read_overrides
from pulsar.interface_profile import (
    TEAM_PACKET_PREFIX,
    TEAM_PACKET_REQUEST,
    is_interface_model,
)
from pulsar.store import Pulse

log = logging.getLogger("pulsar.daemon.handoff.prompts")


HANDOFF_REQUEST = (
    "[pulsar] Your context is nearly full. A fresh session will replace this "
    "one and continue seamlessly. Wrap up ONLY the step you are in the middle "
    "of (or park it safely) — start nothing new — then end your turn with the "
    "handoff for your successor as your final text, with these numbered "
    "sections:\n"
    "1. What we're working on (plain words, brief).\n"
    "2. Current state — what's done, what's in flight.\n"
    "3. What the user wants next / open questions.\n"
    "4. Anything the successor must not relearn (paths, decisions, gotchas).\n"
    "The successor picks up the moment you finish — make the handoff enough "
    "to resume mid-stride."
)

# A model switch reuses the handoff, but "your context is nearly full" is
# false there and confused a tester (shown on a 0-token pulse).
SWITCH_OPENER = ("[pulsar] This pulse is switching to another model. A fresh "
                 "session on the new model will continue seamlessly.")

# A handoff the user asked for from the ⋯ menu: "nearly full" was false
# there too (shown on a pulse at 12K of 225K).
MANUAL_OPENER = ("[pulsar] A fresh session was requested for this pulse. A "
                 "fresh session will replace this one and continue seamlessly.")

# The context-threshold opener, for the `handoff_prompt` override's
# {opener} placeholder (harness-overrides.json).
_CONTEXT_OPENER = ("[pulsar] Your context is nearly full. A fresh session "
                   "will replace this one and continue seamlessly.")


def _safe_format(tpl: str, **kw: object) -> str:
    """str.format that never blows up a handoff on a bad placeholder."""
    try:
        return tpl.format(**kw)
    except Exception as e:
        log.warning("harness override template not formattable (%s) — "
                    "using it as literal text", e)
        return tpl


def _override(pulse_id: str, key: str) -> str | None:
    """A string template override from harness-overrides.json, or None.

    An explicitly supplied empty string is honoured as empty (present !=
    truthy): an override can remove a template outright.
    Only a missing or non-string key means "absent"."""
    tpl = read_overrides(paths.pulse_home(pulse_id)).get(key)
    return tpl if isinstance(tpl, str) else None

# The notes are working state, not a request: "act on it" made a successor
# re-answer the user's last, already-answered message.
SUCCESSOR_PREFIX = (
    "[pulsar handoff] You are continuing a previous session in a fresh "
    "context. Your predecessor's handoff follows. It is working state, not a "
    "new request: resume only work it lists as still in flight, never redo or "
    "re-answer what is already done, and answer the user's next message as "
    "if there was no seam.\n\n"
)

# Added when the old session was idle at the request: nothing was in flight,
# so the successor's first turn must not start anything.
IDLE_SEED_NOTE = (
    "The previous session had finished and was waiting for the user. Reply "
    "with one short line that you are ready, and start nothing until the "
    "user writes.\n\n"
)


# Watchers: a watching-mode pulse's work-in-progress IS its monitoring brief.
# Without this the successor inherits finished work and no idea it is supposed
# to keep waiting on things.
WATCHING_ADDENDUM = (
    "\nThis pulse is in WATCHING mode: its main work is finished and it now "
    "sleeps between watch/schedule wakes to keep an eye on things. Add a "
    "section 5. Standing watch brief — for EACH condition watch and schedule "
    "you own: what it watches, why, and exactly what to do when it fires "
    "(who to message, what to check, what to report). The successor must "
    "RESUME WAITING under that brief — not restart the completed work. "
    "The watches and schedules themselves carry over unchanged (same "
    "pulse id)."
)


# Notes file: one file per pulse that outlives every handoff — core
# constraints, standing decisions, key facts. The dying session curates it;
# the daemon pastes it into the seed so it survives even when the prose
# handoff forgets something.
NOTES_FILE = "NOTES.md"


def notes_path(pulse_id: str) -> Path:
    return paths.pulse_home(pulse_id) / NOTES_FILE


def notes_addendum(pulse_id: str) -> str:
    return (
        f"\nBefore writing the handoff, review your notes file "
        f"{notes_path(pulse_id)} (create it if missing). It persists across "
        "every handoff and is pasted into your successor's seed. Keep only "
        "what must survive many handoffs: core constraints, the user's standing "
        "decisions, key paths and facts. Add what this session learned that "
        "belongs there; delete or condense what is stale or finished. Keep it "
        "short. Do not copy the handoff into it."
    )


# NOTES.md is pasted into EVERY handoff seed. Unbounded growth can eat 10–25%
# of the successor's context before its first turn, so the paste is capped:
# the head carries the map, the file keeps the rest.
NOTES_PASTE_BYTES = 8 * 1024
NOTES_WARN_BYTES = 16 * 1024


def notes_size(pulse_id: str) -> int:
    try:
        return notes_path(pulse_id).stat().st_size
    except OSError:
        return 0


def notes_block(pulse_id: str) -> str:
    try:
        text = notes_path(pulse_id).read_text(errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    total = len(text.encode("utf-8", errors="replace"))
    if total > NOTES_PASTE_BYTES:
        head = text.encode("utf-8", errors="replace")[:NOTES_PASTE_BYTES]
        text = (head.decode("utf-8", errors="ignore").rstrip()
                + f"\n\n[... truncated: the file is {total // 1024} KB, only the "
                f"first {NOTES_PASTE_BYTES // 1024} KB is pasted. Read the file "
                f"for the rest and condense it below {NOTES_PASTE_BYTES // 1024} KB.]")
    return (f"\n\n---\nYour notes file ({notes_path(pulse_id)}), carried "
            f"across handoffs — keep it current:\n\n{text}\n")


def handoff_prompt_for(pulse: Pulse, reason: str = "context_threshold") -> str:
    tpl = _override(pulse.id, "handoff_prompt")
    if tpl is not None:
        # An explicit template wins for every model; the interface TEAM-PACKET
        # below applies only when the key is absent.
        opener = _CONTEXT_OPENER if reason == "context_threshold" \
            else SWITCH_OPENER if reason == "model_switch" else MANUAL_OPENER
        base = _safe_format(tpl, opener=opener)
    else:
        base = TEAM_PACKET_REQUEST if is_interface_model(pulse.model) else HANDOFF_REQUEST
        if reason in ("model_switch", "manual") and base is HANDOFF_REQUEST:
            base = base.replace(
                "[pulsar] Your context is nearly full. A fresh session will "
                "replace this one and continue seamlessly.",
                SWITCH_OPENER if reason == "model_switch" else MANUAL_OPENER, 1)
    if (pulse.settings or {}).get("mode") == "watching":
        base = base + WATCHING_ADDENDUM
    return base + notes_addendum(pulse.id)


def successor_prefix_for(pulse: Pulse, was_idle: bool = False) -> str:
    tpl = _override(pulse.id, "successor_prefix")
    if tpl is not None:  # explicit template wins for every model (finding 6)
        return _safe_format(tpl,
                            idle_note=IDLE_SEED_NOTE if was_idle else "")
    if is_interface_model(pulse.model):
        return TEAM_PACKET_PREFIX
    return SUCCESSOR_PREFIX + (IDLE_SEED_NOTE if was_idle else "")


_NUMBERED = [re.compile(rf"(?m)^[\s#*_>-]*{n}[.):]") for n in (1, 2, 3, 4)]


def has_sections(text: str, team: bool = False) -> bool:
    """Does a reply carry the sections the request asked for?

    Lenient on markup (``1.``, ``**2.**``, ``## 3)``) — the point is to
    catch a one-line wrap-up sentence posing as a handoff, not to grade
    formatting."""
    if team:
        return "TEAM-PACKET" in text and "## Goal" in text
    return all(rx.search(text) for rx in _NUMBERED)


def reask_prompt_for(pulse: Pulse) -> str:
    if is_interface_model(pulse.model):
        want = ("one compact TEAM-PACKET with the headings from the request "
                "(# TEAM-PACKET, ## Goal, ## Latest user intent, ...)")
    else:
        want = ("the handoff note with the four numbered sections: 1. What "
                "we're working on, 2. Current state, 3. What the user wants "
                "next / open questions, 4. Anything the successor must not "
                "relearn")
    return ("[pulsar] Your last reply did not contain the handoff. Your "
            "successor starts from ONLY what you write now. Do no other "
            f"work: reply with {want}. End your turn with it as your final "
            "text.")


from pulsar.launch.context import daemon_url as _daemon_url

DAEMON_URL = "http://127.0.0.1:8726"


def warning_text(pulse: Pulse, ctx: int, thr: int) -> str:
    note = paths.pulse_home(pulse.id) / "handoffs" / "next.md"
    url = _daemon_url()
    tpl = _override(pulse.id, "handoff_warning")
    if tpl is not None:
        return _safe_format(
            tpl, ctx=ctx, thr=thr, note=note,
            notes_path=notes_path(pulse.id), pulse_id=pulse.id,
            daemon_url=url) + notes_size_alarm(pulse.id)
    return (
        f"[pulsar] Context is at {ctx:,} of {thr:,} tokens (your handoff "
        f"threshold). Keep your notes file {notes_path(pulse.id)} current. "
        "At the next natural stopping point — not mid-step — write your "
        f"handoff note to {note} (1. What we're working on, 2. Current "
        "state, 3. What the user wants next / open questions, 4. Anything "
        "the successor must not relearn) and hand off yourself:\n"
        f"  curl -s -X POST {url}/pulses/{pulse.id}/handoff "
        "-H 'Content-Type: application/json' "
        f"-d '{{\"note_path\": \"{note}\"}}'\n"
        "That ends this session; a fresh one continues from your note. If "
        f"you don't, the daemon will ask for a handoff at {thr:,} tokens."
        + notes_size_alarm(pulse.id))


def notes_size_alarm(pulse_id: str) -> str:
    """Rides the context warning (no extra wakeup): a notes file past
    NOTES_WARN_BYTES is pasted truncated at handoff, so the pulse must
    condense it before it hands off."""
    size = notes_size(pulse_id)
    if size <= NOTES_WARN_BYTES:
        return ""
    return (f"\n[pulsar] Your notes file is {size // 1024} KB. Only the first "
            f"{NOTES_PASTE_BYTES // 1024} KB reaches your successor; condense "
            f"it below {NOTES_PASTE_BYTES // 1024} KB before you hand off.")
