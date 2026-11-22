"""The handoff monitor: early warning, the due list, and the tick that fires
background handoffs (loop wrapper: loops.run_every, started from app.py)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pulsar.daemon import paths
from pulsar.daemon.handoff import engine, state
from pulsar.daemon.handoff.prompts import warning_text
from pulsar.daemon.timeutil import now_iso as _now_iso
from pulsar.store import Pulse, Store

logger = logging.getLogger("pulsar.daemon.handoff")

TICK_SECONDS = 30.0


# --------------------------------------------------------------------------- #
# Early warning (handoff review 2026-10-02, proposal 4): at ~80% of the
# threshold tell the pulse once per session to keep NOTES.md current and hand
# off at its next natural stopping point — the pulse picks the seam instead
# of the request landing mid-stride. The forced request at 100% stays as the
# fallback.
# --------------------------------------------------------------------------- #
WARN_FRACTION = 0.8


def _warn_key(pulse: Pulse, beat: dict) -> str:
    """One warning per session: the handoff count plus the CLI session id."""
    return (f"{int((pulse.settings or {}).get('handoff_count') or 0)}:"
            f"{beat.get('cli_session_id') or ''}")


def _eligible(beat: dict) -> Optional[str]:
    """The checks warn_pulses and due_pulses share: None when the heartbeat
    can be acted on, else why not ("no_ctx", "stale", "dead"). The pulse is
    not needed, so it is not a parameter."""
    if not beat.get("last_ctx_tokens"):
        return "no_ctx"
    # A just-dead runner's final heartbeat still carries its high context
    # count. Firing at it would write a durable request and wait ten
    # minutes for a reply nobody can send; the dead-runner sweep is the
    # right handler for this pulse, so leave it alone.
    if not paths.heartbeat_fresh(beat):
        return "stale"
    if beat.get("state") in ("stopped", "cli_dead"):
        return "dead"
    return None


def warn_pulses(store: Store) -> int:
    """Send the early warning to every running pulse in [80%, 100%) of its
    threshold that has not been warned this session. Returns the count."""
    sent = 0
    for pulse in store.list_pulses(state="running"):
        beat = paths.read_heartbeat(pulse.id) or {}
        if _eligible(beat) is not None:
            continue
        ctx = beat["last_ctx_tokens"]
        thr = state.threshold_for(pulse)
        if not (WARN_FRACTION * thr <= ctx < thr):
            continue
        key = _warn_key(pulse, beat)
        if (pulse.settings or {}).get("handoff_warned") == key:
            continue
        if state.in_flight(pulse.id) is not None:
            continue
        engine.write_message(pulse.id, warning_text(pulse, ctx, thr), "handoff-warn")
        store.update_settings(pulse.id, {"handoff_warned": key})
        state.append_ledger({"ts": _now_iso(), "pulse_id": pulse.id,
                             "event": "warning", "ctx": ctx, "threshold": thr,
                             "model": pulse.model, "vessel": pulse.vessel})
        logger.info("handoff.warn pulse=%s ctx=%d thr=%d — early warning sent",
                    pulse.id, ctx, thr)
        sent += 1
    return sent


# --------------------------------------------------------------------------- #
# The monitor (one tick; loop wrapper mirrors scheduler.run_scheduler)
# --------------------------------------------------------------------------- #
def over_threshold(pulse: Pulse, beat: Optional[dict]) -> bool:
    """True when this heartbeat's context is at/over the pulse's handoff
    threshold and no cooldown applies — i.e. the monitor WOULD fire if the
    pulse stayed running. watch_sleep uses it so a woken watcher is not put
    back to sleep seconds before its rotation (Watchers, 2026-09-07)."""
    ctx = (beat or {}).get("last_ctx_tokens")
    if not ctx:
        return False
    if ctx < state.threshold_for(pulse):
        return False
    return not state.in_cooldown(pulse)


def due_pulses(store: Store) -> list[tuple[Pulse, int, int]]:
    """(pulse, ctx, threshold) for every running pulse over its threshold."""
    out = []
    for pulse in store.list_pulses(state="running"):
        beat = paths.read_heartbeat(pulse.id) or {}
        skip = _eligible(beat)
        if skip == "no_ctx":
            continue
        ctx = beat["last_ctx_tokens"]
        if skip == "stale":
            logger.info("handoff.tick pulse=%s ctx=%d but heartbeat stale "
                        "(%.0fs) — skipped", pulse.id, ctx,
                        paths.heartbeat_age(beat) or -1.0)
            continue
        if skip == "dead":
            logger.info("handoff.tick pulse=%s ctx=%d but runner says %s — "
                        "skipped", pulse.id, ctx, beat.get("state"))
            continue
        thr = state.threshold_for(pulse)
        if ctx < thr:
            continue
        if state.in_cooldown(pulse):
            logger.info("handoff.tick pulse=%s ctx=%d >= thr=%d but in "
                        "cooldown — skipped", pulse.id, ctx, thr)
            continue
        out.append((pulse, ctx, thr))
    return out


# Monitor handoffs in flight, by pulse id. Each runs as its own task: run
# serially, one pulse's 600s reply wait held up every other pulse's handoff
# (live 2026-10-13).
_ACTIVE: dict[str, asyncio.Task] = {}


async def _fire(store: Store, pulse: Pulse) -> bool:
    try:
        return await engine.run_handoff(store, pulse)
    except Exception:
        logger.exception("handoff.tick pulse=%s run_handoff RAISED", pulse.id)
        return False
    finally:
        _ACTIVE.pop(pulse.id, None)


async def drain() -> list[bool]:
    """Await every monitor handoff in flight (tests; shutdown)."""
    return list(await asyncio.gather(*list(_ACTIVE.values())))


async def tick(store: Store) -> int:
    """One monitor pass. Starts a background handoff for each due pulse
    and returns how many it started; it does not wait for them."""
    try:
        warn_pulses(store)
    except Exception:
        logger.exception("handoff.tick warn_pulses RAISED — continuing")
    fired = 0
    for pulse, ctx, thr in due_pulses(store):
        if pulse.id in _ACTIVE:
            continue
        # The list is built once per pass; re-check right before firing.
        if state.store_state(store, pulse.id) != "running":
            logger.info("handoff.tick pulse=%s no longer running — skipped",
                        pulse.id)
            continue
        logger.info("handoff.tick pulse=%s ctx=%d crossed thr=%d — firing",
                    pulse.id, ctx, thr)
        _ACTIVE[pulse.id] = asyncio.create_task(_fire(store, pulse))
        fired += 1
    return fired
