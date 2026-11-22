"""Loop triggers (v25): a loop schedule can fire on its timer, on every handoff,
on every message the user sends, every N replies the pulse makes, and/or every
N tool calls.

Why: pulses drift. Standing rules typed into chat get buried under thousands of
messages and vanish at handoff. A loop whose prompt rides those events keeps
the rules in front of the model. Stored as a csv on the schedule row
("timer,message,replies:5,tools:20"); NULL/empty = timer only, so every pre-v25
loop behaves exactly as before.

Counter triggers read ``replies_total`` / ``tool_calls_total`` off the pulse
heartbeat (runner.py writes them; a session restart resets them, which the
baseline logic treats as a fresh start, never a fire).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from pulsar.daemon import paths
from pulsar.store import Store

EVENT: frozenset[str] = frozenset({"timer", "handoff", "message"})
COUNTER: frozenset[str] = frozenset({"replies", "tools"})
VALID: frozenset[str] = EVENT | COUNTER
DEFAULT: frozenset[str] = frozenset({"timer"})
_COUNTER_RE = re.compile(r"^(replies|tools):(\d+)$")
_BEAT_KEY = {"replies": "replies_total", "tools": "tool_calls_total"}


def parse(raw: Optional[str]) -> set[str]:
    """csv -> set of trigger NAMES ("replies:5" -> "replies"); None/empty/
    garbage-only -> {"timer"}."""
    if not raw:
        return set(DEFAULT)
    got: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item in EVENT:
            got.add(item)
        elif (m := _COUNTER_RE.match(item)) and int(m.group(2)) > 0:
            got.add(m.group(1))
    return got or set(DEFAULT)


def every(raw: Optional[str], name: str) -> Optional[int]:
    """The N of a counter trigger ("replies:5" -> 5), or None if absent."""
    if not raw:
        return None
    for item in raw.split(","):
        m = _COUNTER_RE.match(item.strip())
        if m and m.group(1) == name and int(m.group(2)) > 0:
            return int(m.group(2))
    return None


def normalize(triggers: Optional[Iterable[str]], kind: str) -> Optional[str]:
    """Request list -> stored csv. None stays None (= keep/default). Raises
    ValueError on an unknown name, a bad N, an empty list, or a non-loop."""
    if triggers is None:
        return None
    if kind != "loop":
        raise ValueError("triggers apply to loop schedules only")
    got: dict[str, str] = {}
    for t in triggers:
        t = str(t).strip()
        if t in EVENT:
            got[t] = t
        elif (m := _COUNTER_RE.match(t)) and int(m.group(2)) > 0:
            got[m.group(1)] = f"{m.group(1)}:{int(m.group(2))}"
        else:
            raise ValueError(
                f"unknown trigger {t!r}; valid: timer, handoff, message, "
                f"replies:N, tools:N (N > 0)")
    if not got:
        raise ValueError("a loop needs at least one trigger")
    return ",".join(got[k] for k in sorted(got))


MODES: frozenset[str] = frozenset({"any", "all"})


def mode(raw: Optional[str]) -> str:
    return raw if raw in MODES else "any"


def normalize_mode(raw: Optional[str], kind: str) -> Optional[str]:
    if raw is None:
        return None
    if kind != "loop":
        raise ValueError("trigger_mode applies to loop schedules only")
    m = str(raw).strip().lower()
    if m not in MODES:
        raise ValueError("trigger_mode must be 'any' or 'all'")
    return m


def as_list(raw: Optional[str]) -> list[str]:
    """Stored csv -> wire list, counters kept as 'replies:5'."""
    if not raw:
        return ["timer"]
    out = [t.strip() for t in raw.split(",")
           if t.strip() in EVENT or _COUNTER_RE.match(t.strip())]
    return sorted(out) or ["timer"]


def _loops_with(store: Store, pulse_id: str, trigger: str) -> list:
    return [s for s in store.list_schedules(pulse_id=pulse_id, enabled_only=True)
            if s.kind == "loop" and trigger in parse(s.triggers)]


def label(sched) -> str:
    bits = []
    t = parse(sched.triggers)
    if "timer" in t and sched.interval_minutes:
        bits.append(f"every {sched.interval_minutes} min")
    if "message" in t:
        bits.append("every message")
    if "handoff" in t:
        bits.append("every handoff")
    if "replies" in t:
        bits.append(f"every {every(sched.triggers, 'replies')} replies")
    if "tools" in t:
        bits.append(f"every {every(sched.triggers, 'tools')} tool calls")
    return ", ".join(bits)


def _blocks(loops) -> str:
    return "\n\n".join(f"Standing loop ({label(s)}):\n{s.prompt.strip()}"
                       for s in loops)


def message_block(store: Store, pulse_id: str) -> str:
    """The block appended to the user's message; '' when no loop asks for it."""
    loops = _loops_with(store, pulse_id, "message")
    return ("\n\n---\n" + _blocks(loops)) if loops else ""


def append_to_message(store: Store, pulse_id: str, text: str) -> str:
    block = message_block(store, pulse_id)
    return f"{text}{block}" if block else text


def handoff_block(store: Store, pulse_id: str) -> str:
    """Pasted into the successor seed next to NOTES.md; '' when none."""
    loops = _loops_with(store, pulse_id, "handoff")
    if not loops:
        return ""
    return ("\n\n---\nStanding loops the user set on this pulse — they bind you "
            "as much as they bound your predecessor:\n\n" + _blocks(loops) + "\n")


# --------------------------------------------------------------------------- #
# Timed + counter conditions (scheduler tick)
# --------------------------------------------------------------------------- #
def _beat(sched, beats: dict[str, Optional[dict]]) -> dict:
    if sched.pulse_id not in beats:
        beats[sched.pulse_id] = paths.read_heartbeat(sched.pulse_id)
    return beats[sched.pulse_id] or {}


def _state(sched) -> dict[str, Any]:
    try:
        st = json.loads(sched.trigger_state or "{}")
        return st if isinstance(st, dict) else {}
    except (TypeError, ValueError):
        return {}


def _counter_values(sched, beat: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in parse(sched.triggers) & COUNTER:
        v = beat.get(_BEAT_KEY[name])
        if isinstance(v, int):
            out[name] = v
    return out


def loop_due(store: Store, sched, now, beats: dict[str, Optional[dict]],
             timer_due) -> bool:
    """Combine a loop's armed conditions: timer (``timer_due(sched, now)``),
    replies:N and tools:N (heartbeat counters vs the baselines in
    ``trigger_state``). mode "any": one met condition fires. mode "all":
    every armed condition must be met. Loops with no timed/counter trigger
    (message/handoff only) are never due here.

    A counter seen for the first time, or one that went BACKWARDS (new
    session), is baselined without firing.
    """
    names = parse(sched.triggers)
    conds: dict[str, bool] = {}
    if "timer" in names:
        conds["timer"] = bool(timer_due(sched, now))
    counters = names & COUNTER
    if counters:
        beat = _beat(sched, beats)
        state = _state(sched)
        cur = _counter_values(sched, beat)
        new_state = dict(state)
        changed = False
        for name in counters:
            if name not in cur:
                conds[name] = False  # old runner / no heartbeat: not met
                continue
            base = state.get(name)
            if not isinstance(base, int) or cur[name] < base:
                new_state[name] = cur[name]
                changed = True
                conds[name] = False
                continue
            conds[name] = cur[name] - base >= (every(sched.triggers, name) or 1)
        if changed:
            store.set_trigger_state(sched.id, json.dumps(new_state, sort_keys=True))
    if not conds:
        return False
    return all(conds.values()) if mode(sched.trigger_mode) == "all" \
        else any(conds.values())


def reset_after_fire(store: Store, sched, beats: dict[str, Optional[dict]]) -> None:
    """After ANY fire, every counter baseline moves to the current heartbeat
    values (the timer anchor was already moved by mark_fired)."""
    if not (parse(sched.triggers) & COUNTER):
        return
    cur = _counter_values(sched, _beat(sched, beats))
    if not cur:
        return
    new_state = _state(sched)
    new_state.update(cur)
    store.set_trigger_state(sched.id, json.dumps(new_state, sort_keys=True))
